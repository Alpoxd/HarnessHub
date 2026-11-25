from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from platformdirs import user_data_path
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from harnesshub import __version__
from harnesshub.agy import AgyMaintenance
from harnesshub.consult import consult_peers
from harnesshub.errors import HubError
from harnesshub.ide import launch_ide
from harnesshub.installation import Installer
from harnesshub.mcp import serve_mcp
from harnesshub.models import (
    catalog_models,
    mark_quota_cooldown,
    probe_agy_models,
    probe_all_profiles,
    probe_profile_telemetry,
    rank_candidates,
)
from harnesshub.service import Hub
from harnesshub.sessions import list_sessions, read_session
from harnesshub.telemetry import account_telemetry
from harnesshub.usage import read_usage


def _fmt_cli_tokens(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _fmt_cli_turns(value: int | None) -> str:
    return "—" if value is None else str(value)


def _fmt_cli_reset(ts: float | int | None) -> str:
    from datetime import datetime

    if not ts or ts <= 0:
        return "—"
    dt = datetime.fromtimestamp(float(ts))
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%d.%m %H:%M")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harnesshub", description="Менеджер подписочных AI harness")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--data-dir", type=Path, default=user_data_path("harnesshub", appauthor=False))
    p.add_argument("--json", action="store_true", help="Машиночитаемый вывод")
    sub = p.add_subparsers(dest="action")
    sub.add_parser("tui", help="Интерактивный терминальный интерфейс")
    sub.add_parser("list", help="Каталог harness и установленные клиенты")
    sub.add_parser("doctor", help="Проверка конфигурации без запуска клиентов")
    install = sub.add_parser(
        "install", help="Установить оригинальный harness из официального источника"
    )
    install.add_argument("provider")
    install.add_argument(
        "--dry-run", action="store_true", help="Показать источник и план без загрузки"
    )
    install.add_argument("-p", "--profile", help="Использовать прокси конкретного профиля")
    proxy = sub.add_parser("proxy", help="Общий прокси и переопределения профилей")
    scope = proxy.add_subparsers(dest="proxy_scope", required=True)
    for name in ("global", "profile"):
        item = scope.add_parser(name)
        if name == "profile":
            item.add_argument("provider")
            item.add_argument("name")
        item.add_argument(
            "operation", choices=("show", "set", "direct", "inherit"), nargs="?", default="show"
        )
        item.add_argument("url", nargs="?")
        item.add_argument("--no-proxy", help="Исключения через запятую; по умолчанию localhost")
    profiles = sub.add_parser("profiles", help="Управление профилями").add_subparsers(
        dest="profile_action", required=True
    )
    listing = profiles.add_parser("list")
    listing.add_argument("provider", nargs="?")
    profiles.add_parser("discover", help="Найти существующие профили AGY/Codex/Claude без импорта")
    for action in ("new", "import", "use", "rename", "remove"):
        item = profiles.add_parser(action)
        item.add_argument("provider")
        item.add_argument("name")
        if action == "import":
            item.add_argument("path", type=Path)
        if action == "rename":
            item.add_argument("new_name")
    for action in ("run", "login"):
        item = sub.add_parser(action)
        item.add_argument("provider")
        selection = item.add_mutually_exclusive_group()
        selection.add_argument("-p", "--profile")
        selection.add_argument("--native", action="store_true", help="Родная конфигурация клиента")
        item.add_argument("-C", "--cwd", type=Path)
        if action == "run":
            perm = item.add_mutually_exclusive_group()
            perm.add_argument(
                "--yolo",
                dest="automode",
                action="store_const",
                const=True,
                default=None,
                help="Автомод: запуск без подтверждения разрешений",
            )
            perm.add_argument(
                "--safe",
                dest="automode",
                action="store_const",
                const=False,
                help="Безопасный режим: запрашивать подтверждения",
            )

    usage_p = sub.add_parser("usage", help="Квоты, тарифный план и статистика токенов")
    usage_p.add_argument("provider", nargs="?")
    usage_p.add_argument("-p", "--profile")
    usage_p.add_argument("--all", action="store_true", help="Опросить все аккаунты")

    accounts_p = sub.add_parser(
        "accounts", help="Сводка аккаунтов по харнессам: планы, квоты, модели, токены"
    )
    accounts_p.add_argument("provider", nargs="?", help="Фильтр по harness")
    accounts_p.add_argument("--live", action="store_true", help="Выполнить живой опрос клиентов")
    agy = sub.add_parser("agy", help="Патчинг и автохил").add_subparsers(
        dest="agy_action", required=True
    )
    for action in ("status", "heal", "restore"):
        agy.add_parser(action)
    configure = agy.add_parser("configure")
    configure.add_argument("--binary")
    configure.add_argument("--helper")
    autoheal = agy.add_parser("autoheal")
    autoheal.add_argument("name")
    autoheal.add_argument("mode", choices=("on", "off"))
    agy_auto = agy.add_parser(
        "automode", help="Режим пропуска подтверждений AGY (--dangerously-skip-permissions)"
    )
    agy_auto.add_argument(
        "mode", choices=("status", "on", "off", "inherit"), nargs="?", default="status"
    )
    agy_auto.add_argument("-p", "--profile")
    agy_share = agy.add_parser(
        "share-history", help="Общая история диалогов AGY между профилями без потери токенов"
    )
    agy_share.add_argument("mode", choices=("status", "on", "off"), nargs="?", default="status")
    agy_share.add_argument("--home", type=Path, help="Канонический домашний каталог (~/.gemini)")
    agy_share.add_argument(
        "--copy-local",
        action="store_true",
        help="При выключении скопировать историю в локальные каталоги профилей",
    )
    agy_sync = agy.add_parser(
        "sync", help="Синхронизировать settings.json, GEMINI.md и скиллы в профили AGY"
    )
    agy_sync.add_argument("--home", type=Path, help="Источник настроек (~/.gemini)")
    custom = (
        sub.add_parser("harness", help="Расширение каталога")
        .add_subparsers(dest="harness_action", required=True)
        .add_parser("add")
    )
    custom.add_argument("id")
    custom.add_argument("--name", required=True)
    custom.add_argument("--command", required=True)
    custom.add_argument("--config-env")
    custom.add_argument("--login-arg", action="append", default=[])

    models = sub.add_parser("models", help="Реестр моделей и ранжирование аккаунтов по квотам")
    models_sub = models.add_subparsers(dest="models_action")
    m_list = models_sub.add_parser("list", help="Реестр моделей по зарегистрированным профилям")
    m_list.add_argument("-p", "--provider", help="Фильтр по harness")
    m_list.add_argument("--family", help="Фильтр по семейству (gpt, gemini, claude, other)")
    m_list.add_argument("-q", "--query", default="", help="Поиск по имени или ID модели")
    m_rank = models_sub.add_parser("rank", help="Отсортировать аккаунты по квоте для модели")
    m_rank.add_argument("model", help="Модель или алиас (argon, hydro, astra, sol, opus, sonnet)")
    m_rank.add_argument("-p", "--provider", help="Фильтр по harness")
    m_probe = models_sub.add_parser("probe", help="Живой опрос моделей и квот профиля")
    m_probe.add_argument("profile", nargs="?", help="Имя профиля (без --all)")
    m_probe.add_argument("-p", "--provider", help="Harness (agy, codex, claude)")
    m_probe.add_argument("--all", action="store_true", help="Опросить все профили")
    m_cooldown = models_sub.add_parser("cooldown", help="Пометить квоту модели/профиля исчерпанной")
    m_cooldown.add_argument("provider")
    m_cooldown.add_argument("profile")
    m_cooldown.add_argument("-m", "--model")
    m_cooldown.add_argument("--seconds", type=int, default=3600)

    smart = sub.add_parser(
        "smart", help="Model-First запуск: выбрать лучший свободный аккаунт по квоте для модели"
    )
    smart.add_argument("model", help="Модель или алиас (argon, hydro, orch, opus, astra, sol)")
    smart.add_argument("-p", "--provider", help="Ограничить выбор конкретным harness")
    smart.add_argument("-C", "--cwd", type=Path)
    smart_perm = smart.add_mutually_exclusive_group()
    smart_perm.add_argument(
        "--yolo",
        dest="automode",
        action="store_const",
        const=True,
        default=None,
        help="Автомод: запуск без подтверждения разрешений",
    )
    smart_perm.add_argument(
        "--safe",
        dest="automode",
        action="store_const",
        const=False,
        help="Безопасный режим: запрашивать подтверждения",
    )
    smart.add_argument(
        "--dry-run", action="store_true", help="Показать выбранный профиль без запуска"
    )

    sessions = sub.add_parser(
        "sessions", help="Единая история диалогов, продолжение и кросс-харнессный handoff"
    )
    sess_sub = sessions.add_subparsers(dest="sessions_action")
    s_list = sess_sub.add_parser("list", help="Список диалогов по всем профилям")
    s_list.add_argument("-p", "--provider", help="Фильтр по harness")
    s_list.add_argument("--profile", help="Фильтр по профилю")
    s_list.add_argument("-C", "--cwd", type=Path, help="Фильтр по каталогу проекта")
    s_list.add_argument("-q", "--query", default="", help="Поиск по теме, модели, проекту или ID")
    s_list.add_argument("--all", action="store_true", help="Показать диалоги всех проектов")
    s_list.add_argument("-n", "--limit", type=int, default=30)
    s_show = sess_sub.add_parser("show", help="Прочитать сообщения диалога")
    s_show.add_argument("session_id")
    s_resume = sess_sub.add_parser("resume", help="Продолжить диалог в родном harness и профиле")
    s_resume.add_argument("session_id")
    s_resume.add_argument("-m", "--model", help="Переопределить модель при продолжении")
    s_resume.add_argument(
        "-p",
        "--profile",
        dest="target_profile",
        help="Продолжить диалог под указанным профилем (удобно при общей истории AGY)",
    )
    s_resume.add_argument("-C", "--cwd", type=Path)
    res_perm = s_resume.add_mutually_exclusive_group()
    res_perm.add_argument(
        "--yolo",
        dest="automode",
        action="store_const",
        const=True,
        default=None,
        help="Автомод: запуск без подтверждения разрешений",
    )
    res_perm.add_argument(
        "--safe",
        dest="automode",
        action="store_const",
        const=False,
        help="Безопасный режим: запрашивать подтверждения",
    )
    s_handoff = sess_sub.add_parser(
        "handoff", help="Передать контекст диалога в другой harness/аккаунт/модель"
    )
    s_handoff.add_argument("session_id")
    s_handoff.add_argument("-m", "--model", help="Целевая модель (автоподбор лучшего аккаунта)")
    s_handoff.add_argument(
        "--to", dest="target_provider", help="Целевой harness (agy, codex, claude)"
    )
    s_handoff.add_argument("-p", "--profile", dest="target_profile", help="Целевой профиль")
    s_handoff.add_argument("--note", default="", help="Дополнительное указание новому агенту")
    s_handoff.add_argument("-C", "--cwd", type=Path)
    ho_perm = s_handoff.add_mutually_exclusive_group()
    ho_perm.add_argument(
        "--yolo",
        dest="automode",
        action="store_const",
        const=True,
        default=None,
        help="Автомод: запуск без подтверждения разрешений",
    )
    ho_perm.add_argument(
        "--safe",
        dest="automode",
        action="store_const",
        const=False,
        help="Безопасный режим: запрашивать подтверждения",
    )
    s_handoff.add_argument(
        "--dry-run", action="store_true", help="Показать handoff-план и бриф без запуска"
    )

    consult = sub.add_parser(
        "consult", help="Второе/третье мнение от модели другого семейства с контекстом задачи"
    )
    consult.add_argument("question", nargs="?", default="", help="Вопрос для консультации")
    consult.add_argument(
        "-m",
        "--model",
        action="append",
        dest="models",
        help="Целевая модель (можно указать несколько раз для параллельного опроса)",
    )
    consult.add_argument(
        "--council",
        action="store_true",
        help="Консилиум трёх семейств (Gemini + GPT + Claude) параллельно",
    )
    consult.add_argument(
        "--from-family",
        choices=("gemini", "gpt", "claude"),
        help="Исключить текущее семейство и выбрать другое",
    )
    consult.add_argument("--session", help="ID диалога для подгрузки контекста")
    consult.add_argument(
        "--no-session", action="store_true", help="Не прикреплять контекст последнего диалога"
    )
    consult.add_argument("--no-git", action="store_true", help="Не прикреплять git status/diff")
    consult.add_argument("-C", "--cwd", type=Path)
    consult.add_argument("-t", "--timeout", type=float, default=180.0)
    consult.add_argument("--dry-run", action="store_true", help="Показать план без запуска CLI")

    ide = sub.add_parser(
        "ide", help="Запуск IDE (Antigravity IDE, VS Code, Cursor) в изолированном профиле"
    )
    ide.add_argument("target", help="IDE или harness (agy, vscode, cursor, claude, codex)")
    ide.add_argument("-p", "--profile", help="Имя профиля")
    ide.add_argument("--agy-profile", help="Профиль AGY для окружения IDE")
    ide.add_argument("--claude-profile", help="Профиль Claude Code для окружения IDE")
    ide.add_argument("--codex-profile", help="Профиль Codex для окружения IDE")
    ide.add_argument("-C", "--cwd", type=Path)
    ide.add_argument("--wait", action="store_true", help="Ждать закрытия окна IDE")
    ide.add_argument("--dry-run", action="store_true", help="Показать команду и пути без запуска")

    sub.add_parser("mcp", help="Запустить встроенный MCP-сервер (stdio) для вызовов из IDE и CLI")
    return p


def _infer_view(value: Any) -> str | None:
    if isinstance(value, list):
        if not value:
            return None
        first = value[0]
        if isinstance(first, dict):
            if "commands" in first:
                return "list"
            if "quota_source" in first and "plan_detail" in first:
                return "accounts"
            if "resolved_model" in first:
                return "models_rank"
            if "ready_profiles" in first:
                return "models_list"
            if "session_id" in first and "turns" in first:
                return "sessions_list"
            if "provider" in first and "path" in first:
                return "profiles_discover" if "external" not in first else "profiles_list"
    elif isinstance(value, dict):
        if "opinions" in value and "question" in value:
            return "consult"
        if "session_id" in value and "messages" in value:
            return "sessions_show"
        if "windows" in value and "provider" in value:
            return "usage"
        if "data_dir" in value and "agy_builtin" in value:
            return "doctor"
        if "configured" in value and "effective" in value:
            return "proxy"
        if "status" in value and "arch" in value and "path" in value:
            return "agy_status"
    return None


def _empty_message(view: str | None) -> str | None:
    return {
        "profiles_list": "Профили пока не зарегистрированы.",
        "profiles_discover": "Новых профилей AGY / Codex / Claude не найдено.",
        "accounts": "Зарегистрированных аккаунтов пока нет.",
        "models_list": "Модели по выбранным фильтрам не найдены.",
        "models_rank": "Подходящих аккаунтов для этой модели не найдено.",
        "sessions_list": "Диалоги по выбранным фильтрам не найдены.",
    }.get(view or "")


def display(value: Any, *, as_json: bool, view: str | None = None) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    console = Console()
    resolved = view or _infer_view(value)
    if isinstance(value, list) and not value:
        msg = _empty_message(resolved)
        if msg:
            console.print(msg)
            return

    if resolved == "list" and isinstance(value, list):
        table = Table(title="HarnessHub · подписочные harness", expand=True)
        for column in ("ID", "Harness", "Клиент", "Профили"):
            table.add_column(column)
        for row in value:
            table.add_row(
                row["id"],
                row["name"],
                "установлен" if row["executable"] else "не найден",
                "доступны" if row["profiles_supported"] else "родной клиент",
            )
        console.print(table)
    elif resolved == "accounts" and isinstance(value, list):
        table = Table(title="Аккаунты харнессов · планы, квоты, токены", expand=True)
        for column in (
            "Harness/Профиль",
            "Аккаунт",
            "План · Срок",
            "Квота",
            "Сброс",
            "Моделей",
            "Токены",
            "Статус",
        ):
            table.add_column(column)
        for row in value:
            from harnesshub.tui.formatters import fmt_cli_quota_summary

            q_str = fmt_cli_quota_summary(row)
            if row.get("locked"):
                status = "занят"
            elif not row.get("logged_in"):
                status = "нет входа"
            elif row.get("in_cooldown"):
                status = "кулдаун"
            elif row.get("plan_expired"):
                status = "истёк"
            elif row.get("active"):
                status = "активен"
            else:
                status = "готов"
            toks = row.get("tokens") or {}
            tok_cell = (
                "общие" if toks.get("shared_history") else _fmt_cli_tokens(toks.get("total_tokens"))
            )
            table.add_row(
                f"{row['provider']}/{row['profile']}",
                str(row.get("account_label") or "—"),
                str(row.get("plan_detail") or "—"),
                q_str,
                _fmt_cli_reset(row.get("resets_at")),
                str(len(row.get("models") or [])),
                tok_cell,
                status,
            )
        console.print(table)
    elif resolved == "profiles_discover" and isinstance(value, list):
        table = Table(title="Найденные профили для импорта", expand=True)
        for column in ("Harness", "Профиль", "Каталог"):
            table.add_column(column)
        for row in value:
            table.add_row(row["provider"], row["name"], row["path"])
        console.print(table)
    elif resolved == "profiles_list" and isinstance(value, list):
        table = Table(title="Профили", expand=True)
        for column in ("Harness", "Профиль", "По умолчанию", "Каталог"):
            table.add_column(column)
        for row in value:
            table.add_row(
                row["provider"], row["name"], "да" if row.get("default") else "", row["path"]
            )
        console.print(table)
    elif resolved == "models_rank" and isinstance(value, list):
        table = Table(title="Ранжирование аккаунтов по модели и квотам", expand=True)
        for column in ("#", "Harness/Профиль", "План", "Модель", "Квота", "Сброс", "Статус"):
            table.add_column(column)
        for idx, row in enumerate(value, 1):
            if row.get("locked"):
                status = "занят"
            elif not row.get("logged_in", True):
                status = "нет входа"
            elif not row.get("quota_ready"):
                status = "лимит 0%"
            elif row.get("active"):
                status = "активен"
            else:
                status = "готово"
            rem = row.get("remaining_percent")
            q_str = f"{rem:g}%" if (row.get("quota_known") and rem is not None) else "—"
            table.add_row(
                str(idx),
                f"{row['provider']}/{row['profile']}",
                str(row.get("plan_detail") or row["tier"]),
                row["resolved_model"],
                q_str,
                _fmt_cli_reset(row.get("resets_at")),
                status,
            )
        console.print(table)
    elif resolved == "models_list" and isinstance(value, list):
        table = Table(title="Реестр моделей", expand=True)
        for column in (
            "Harness",
            "Модель",
            "Название",
            "Слоты",
            "Лучшая квота",
            "Лучший слот",
            "Токены",
        ):
            table.add_column(column)
        for row in value:
            rem = row.get("best_remaining_percent", -1)
            q_str = f"{rem:g}%" if (row.get("quota_known") and rem >= 0) else "—"
            table.add_row(
                ", ".join(row["providers"]),
                row["model"],
                str(row.get("display_name") or row["model"]),
                f"{row['ready_profiles']}/{row['total_profiles']}",
                q_str,
                str(row["best_profile"] or "—"),
                _fmt_cli_tokens(row.get("total_tokens") or None),
            )
        console.print(table)
    elif resolved == "sessions_list" and isinstance(value, list):
        table = Table(title="Истории чата", expand=True)
        columns = (
            "ID",
            "Harness/Профиль",
            "Модель",
            "Ходов",
            "Токены",
            "Проект",
            "Обновлён",
            "Тема",
        )
        for column in columns:
            table.add_column(column)
        for row in value:
            table.add_row(
                row["session_id"][:12],
                f"{row['provider']}/{row['profile']}",
                row["model"],
                _fmt_cli_turns(row.get("turns")),
                _fmt_cli_tokens(row.get("tokens_used")),
                str(row.get("project_name") or "—"),
                row["updated_at_iso"],
                row["title"],
            )
        console.print(table)
    elif resolved == "consult" and isinstance(value, dict):
        console.print(f"[bold]Вопрос:[/bold] {escape(str(value.get('question', '')))}")
        for op in value.get("opinions") or []:
            title = (
                f"{op.get('provider')}/{op.get('profile')} · {op.get('model')} ({op.get('status')})"
            )
            if op.get("status") == "dry-run":
                body = "Команда: " + " ".join(str(a) for a in (op.get("command") or [])[:6]) + " …"
            else:
                body = str(op.get("response") or op.get("error") or "")
            console.print(Panel(escape(body), title=escape(title), expand=True))
    elif resolved == "sessions_show" and isinstance(value, dict):
        header = (
            f"{value.get('provider')}/{value.get('profile')} · {value.get('model')}\n"
            f"ID: {value.get('session_id')}\n"
            f"Проект: {value.get('cwd') or '—'}\n"
            f"Обновлён: {value.get('updated_at_iso') or '—'}"
        )
        console.print(Panel(escape(header), title="Диалог", expand=True))
        for chat_msg in value.get("messages") or []:
            role = "Пользователь" if chat_msg.get("role") == "user" else "Ассистент"
            console.print(f"\n[bold]{role}:[/bold] {escape(str(chat_msg.get('text') or ''))}")
    elif resolved == "usage" and isinstance(value, dict):
        windows = value.get("windows") or []
        console.print(
            f"[bold]{escape(str(value.get('provider')))}/{escape(str(value.get('profile')))}[/bold]"
            f" · план: {escape(str(value.get('plan_detail') or value.get('plan') or '—'))}"
            f" · источник: {escape(str(value.get('source', '—')))}"
        )
        if not windows:
            console.print("Данные о квотах отсутствуют (нет активного кулдауна или кэша).")
            return
        table = Table(title="Окна квот", expand=True)
        for column in ("Окно", "Остаток", "Сброс"):
            table.add_column(column)
        for win in windows:
            rem = win.get("remaining_percent")
            table.add_row(
                str(win.get("label") or win.get("window")),
                f"{rem:g}%" if rem is not None else "—",
                _fmt_cli_reset(win.get("resets_at")),
            )
        console.print(table)
    elif resolved in ("doctor", "proxy", "agy_status", "agy_kv") and isinstance(value, dict):
        titles = {
            "doctor": "Диагностика HarnessHub",
            "proxy": "Прокси",
            "agy_status": "Статус AGY",
            "agy_kv": "Настройки AGY",
        }
        table = Table(title=titles[resolved], expand=True)
        table.add_column("Параметр")
        table.add_column("Значение")
        for k, v in value.items():
            table.add_row(
                str(k), json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            )
        console.print(table)
    else:
        console.print_json(json.dumps(value, ensure_ascii=False))


def _resolve_view(args: argparse.Namespace) -> str | None:
    action = args.action
    if action == "list":
        return "list"
    if action == "doctor":
        return "doctor"
    if action == "proxy":
        return "proxy"
    if action == "accounts":
        return "accounts"
    if action == "usage":
        return "accounts" if (args.all or not args.provider) else "usage"
    if action == "profiles":
        if args.profile_action == "list":
            return "profiles_list"
        if args.profile_action == "discover":
            return "profiles_discover"
    if action == "agy" and args.agy_action == "status":
        return "agy_status"
    if action == "agy" and args.agy_action in ("automode", "share-history", "sync"):
        return "agy_kv"
    if action == "models":
        sub_act = args.models_action or "list"
        if sub_act == "list":
            return "models_list"
        if sub_act == "rank":
            return "models_rank"
    if action == "sessions":
        sub_act = args.sessions_action or "list"
        if sub_act == "list":
            return "sessions_list"
        if sub_act == "show":
            return "sessions_show"
    if action == "consult":
        return "consult"
    return None


def execute(args: argparse.Namespace, hub: Hub, trailing: tuple[str, ...]) -> Any:
    action = args.action
    if action == "install":
        installer = Installer(hub)
        return (
            installer.plan(args.provider, args.profile)
            if args.dry_run
            else (installer.install(args.provider, args.profile))
        )
    if action == "proxy":
        provider = args.provider if args.proxy_scope == "profile" else None
        name = args.name if provider is not None else None
        if args.operation == "show":
            if args.url is not None or args.no_proxy is not None:
                raise HubError("Для изменения прокси используй set, direct или inherit")
            return hub.proxy_info(provider, name)
        policy = {"mode": "url" if args.operation == "set" else args.operation}
        if args.operation == "set":
            policy["url"] = args.url
        elif args.url is not None:
            raise HubError("URL нужен только для команды set")
        if args.no_proxy is not None:
            policy["no_proxy"] = args.no_proxy
        return hub.set_proxy(policy, provider, name)
    if action == "list":
        return [spec.as_dict() for spec in hub.harnesses().values()]
    if action == "doctor":
        return {
            "data_dir": str(hub.store.root),
            "state": "ok",
            "installed": [h.id for h in hub.harnesses().values() if h.executable()],
            "profiles": len(hub.profiles()),
            "models_catalog": len(catalog_models(hub)),
            "agy_builtin": "Linux ELF64 x64, две проверяемые сигнатуры",
            "agy_automode": hub.automode_enabled("agy"),
            "agy_shared_history": bool(
                hub.store.read()["settings"].get("agy_shared_history", False)
            ),
            "credentials": "Управляются родными клиентами",
        }
    if action == "profiles":
        operation = args.profile_action
        if operation == "list":
            if args.provider:
                hub.harness(args.provider)
            return hub.profiles(args.provider)
        if operation == "discover":
            return hub.existing_profiles()
        if operation == "new":
            return hub.create(args.provider, args.name)
        if operation == "import":
            return hub.import_profile(args.provider, args.name, args.path)
        if operation == "use":
            hub.set_default(args.provider, args.name)
        elif operation == "rename":
            hub.rename(args.provider, args.name, args.new_name)
        else:
            hub.remove(args.provider, args.name)
        return {"status": "ok", "action": operation}
    if action == "accounts":
        if args.provider:
            hub.harness(args.provider)
        if args.live:
            return probe_all_profiles(hub, args.provider)
        return [account_telemetry(hub, p, live=False) for p in hub.profiles(args.provider)]
    if action == "usage":
        if args.all or not args.provider:
            if args.provider:
                hub.harness(args.provider)
            return probe_all_profiles(hub, args.provider)
        return asyncio.run(read_usage(hub, args.provider, args.profile))
    if action == "agy":
        maintenance = AgyMaintenance(hub)
        if args.agy_action == "configure":
            maintenance.configure(args.binary, args.helper)
            return {"status": "ok"}
        if args.agy_action == "autoheal":
            maintenance.autoheal(args.name, args.mode == "on")
            return {"status": "ok", "autoheal": args.mode == "on"}
        if args.agy_action == "automode":
            if args.mode == "status":
                return hub.automode_info("agy", args.profile)
            if args.mode == "inherit" and not args.profile:
                return maintenance.automode(None)
            enabled: bool | None = None if args.mode == "inherit" else (args.mode == "on")
            return maintenance.automode(enabled, args.profile)
        if args.agy_action == "share-history":
            if args.mode == "status":
                return maintenance.history_status(home=args.home)
            return maintenance.share_history(
                args.mode == "on", home=args.home, copy_local=args.copy_local
            )
        if args.agy_action == "sync":
            return maintenance.sync_profiles(home=args.home)
        return getattr(
            maintenance, {"heal": "heal", "status": "status", "restore": "restore"}[args.agy_action]
        )()
    if action == "harness":
        hub.register_harness(args.id, args.name, args.command, args.config_env, args.login_arg)
        return {"status": "ok", "id": args.id}
    if action == "models":
        sub_act = args.models_action or "list"
        if sub_act == "list":
            return catalog_models(
                hub,
                getattr(args, "provider", None),
                family=getattr(args, "family", None),
                query=getattr(args, "query", ""),
            )
        if sub_act == "rank":
            return rank_candidates(hub, args.model, args.provider)
        if sub_act == "probe":
            if getattr(args, "all", False) or not args.profile:
                return probe_all_profiles(hub, getattr(args, "provider", None))
            target_provider = getattr(args, "provider", None)
            if not target_provider:
                matches = [p["provider"] for p in hub.profiles() if p["name"] == args.profile]
                if len(matches) > 1:
                    raise HubError(
                        f"Профиль {args.profile!r} найден у нескольких harness "
                        f"({', '.join(matches)}); укажи -p/--provider"
                    )
                target_provider = matches[0] if len(matches) == 1 else "agy"
            if target_provider == "agy":
                return probe_agy_models(hub, args.profile)
            return probe_profile_telemetry(hub, target_provider, args.profile)
        if sub_act == "cooldown":
            return mark_quota_cooldown(hub, args.provider, args.profile, args.model, args.seconds)
    if action == "smart":
        return hub.select_smart_candidate(args.model, args.provider)
    if action == "sessions":
        sub_act = args.sessions_action or "list"
        if sub_act == "list":
            all_projects = getattr(args, "all", False)
            cwd = None if all_projects else (getattr(args, "cwd", None) or Path.cwd())
            return list_sessions(
                hub,
                cwd=cwd,
                provider=getattr(args, "provider", None),
                profile=getattr(args, "profile", None),
                query=getattr(args, "query", ""),
                limit=getattr(args, "limit", 30),
            )
        if sub_act == "show":
            return read_session(hub, args.session_id)
        if sub_act == "handoff":
            return hub.plan_handoff(
                args.session_id,
                target_model=args.model,
                target_provider=args.target_provider,
                target_profile=args.target_profile,
                note=args.note,
                cwd=args.cwd,
            )
    if action == "consult":
        question = args.question
        if not question and not sys.stdin.isatty():
            question = sys.stdin.read()
        return consult_peers(
            hub,
            question,
            models=args.models,
            from_family=args.from_family,
            council=args.council,
            cwd=args.cwd,
            session_id=args.session,
            include_session=not args.no_session,
            include_git=not args.no_git,
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    if action == "ide":
        return launch_ide(
            hub,
            args.target,
            profile=args.profile,
            agy_profile=args.agy_profile,
            claude_profile=args.claude_profile,
            codex_profile=args.codex_profile,
            cwd=args.cwd,
            wait=args.wait,
            dry_run=args.dry_run,
            extra_args=trailing,
        )
    raise HubError("Неизвестная команда")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    trailing: tuple[str, ...] = ()
    if "--" in argv:
        index = argv.index("--")
        trailing, argv = tuple(argv[index + 1 :]), argv[:index]
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    allowed_trailing = ("run", "login", "smart", "ide", "sessions")
    if trailing and args.action not in allowed_trailing:
        argument_parser.error("Аргументы после -- разрешены только для команд запуска")
    hub = Hub(args.data_dir)
    try:
        if args.action == "mcp":
            return serve_mcp(hub)
        if args.action in (None, "tui"):
            if args.json:
                raise HubError(
                    "Для JSON используй list, doctor, accounts, profiles, "
                    "models, sessions или usage"
                )
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                display([h.as_dict() for h in hub.harnesses().values()], as_json=False, view="list")
                return 0
            from harnesshub.tui import run_tui

            return run_tui(hub)
        if args.action in ("run", "login"):
            if args.json:
                raise HubError("JSON не используется при интерактивном запуске клиента")
            return hub.launch(
                args.provider,
                args.profile,
                native=args.native,
                login=args.action == "login",
                automode=getattr(args, "automode", None),
                args=trailing,
                cwd=args.cwd,
            )
        if args.action == "smart" and not args.dry_run:
            if args.json:
                raise HubError("Для JSON со smart добавь --dry-run")
            return hub.launch_smart(
                args.model,
                args.provider,
                automode=getattr(args, "automode", None),
                args=trailing,
                cwd=args.cwd,
            )
        if args.action == "sessions":
            sub_act = args.sessions_action or "list"
            if sub_act == "resume":
                if args.json:
                    raise HubError("JSON не используется при интерактивном resume")
                return hub.resume_session(
                    args.session_id,
                    model=args.model,
                    target_profile=getattr(args, "target_profile", None),
                    automode=getattr(args, "automode", None),
                    args=trailing,
                    cwd=args.cwd,
                )
            if sub_act == "handoff" and not args.dry_run:
                if args.json:
                    raise HubError("Для JSON с handoff добавь --dry-run")
                return hub.handoff_session(
                    args.session_id,
                    target_model=args.model,
                    target_provider=args.target_provider,
                    target_profile=args.target_profile,
                    note=args.note,
                    automode=getattr(args, "automode", None),
                    args=trailing,
                    cwd=args.cwd,
                )
        result = execute(args, hub, trailing)
        display(result, as_json=args.json, view=_resolve_view(args))
        return 0
    except (HubError, OSError, subprocess.SubprocessError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            Console(stderr=True).print(f"Ошибка: {exc}", markup=False)
        return 1
    except KeyboardInterrupt:
        return 130
