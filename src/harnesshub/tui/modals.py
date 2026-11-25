from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from harnesshub.errors import HubError
from harnesshub.installation import Installer
from harnesshub.models import catalog_models
from harnesshub.network import validate_proxy

if TYPE_CHECKING:
    from harnesshub.service import Hub

FALLBACK_MODELS: list[tuple[str, str]] = [
    ("Gemini 4 Argon High · флагман", "gemini-4-argon-high"),
    ("Gemini 4 Hydrogen High · быстрый", "gemini-4-hydrogen-high"),
    ("Claude Opus 5.5 High", "claude-opus-5-5-high"),
    ("Claude Sonnet 5.5 High", "claude-sonnet-5-5-high"),
    ("OpenAI GPT-6.2 Sol", "gpt-6.2-sol"),
    ("OpenAI GPT-6.1 Sol", "gpt-6.1-sol"),
    ("OpenAI GPT-6 Astra", "gpt-6-astra"),
]


def _model_choices_from_hub(hub: Hub) -> list[tuple[str, str]]:
    entries = catalog_models(hub)
    if not entries:
        return list(FALLBACK_MODELS)
    choices: list[tuple[str, str]] = []
    seen_values: set[str] = set()
    for item in entries:
        mid = str(item["model"])
        if mid in seen_values:
            continue
        seen_values.add(mid)
        provs = ",".join(item["providers"])
        label = f"[{provs}] {item['display_name']} ({mid})"
        choices.append((label, mid))
    return choices or list(FALLBACK_MODELS)


class ProfileForm(ModalScreen[dict[str, str] | None]):
    BINDINGS = [("escape", "dismiss(None)", "Отмена")]

    def __init__(self, mode: str, provider: str, current: str = ""):
        super().__init__()
        self.mode, self.provider, self.current = mode, provider, current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                {
                    "new": "Новый профиль",
                    "import": "Импорт каталога",
                    "rename": "Переименовать профиль",
                }[self.mode],
                classes="dialog-title",
            )
            yield Label(f"Harness: {self.provider}", classes="muted")
            yield Label("Имя профиля")
            yield Input(value=self.current, placeholder="work", id="profile-name")
            if self.mode == "import":
                yield Label("Существующий каталог конфигурации")
                yield Input(placeholder="/путь/к/профилю", id="profile-path")
                yield Static(
                    "Каталог останется на месте. Учётные данные не копируются.", classes="muted"
                )
            else:
                yield Static("Отдельная авторизация через родной клиент.", classes="muted")
            with Horizontal(classes="dialog-actions"):
                yield Button("Отмена", id="cancel", compact=True)
                yield Button("Сохранить", id="save", variant="primary", compact=True)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    @on(Input.Submitted)
    def save(self) -> None:
        name = self.query_one("#profile-name", Input).value.strip()
        if not name:
            self.notify("Укажи имя профиля", severity="warning")
            return
        value: dict[str, str] = {"name": name}
        if self.mode == "import":
            value["path"] = self.query_one("#profile-path", Input).value.strip()
            if not value["path"]:
                self.notify("Укажи каталог", severity="warning")
                return
        self.dismiss(value)


class ConfirmRemove(ModalScreen[bool]):
    BINDINGS = [("escape", "dismiss(False)", "Отмена")]

    def __init__(self, profile_name: str):
        super().__init__()
        self.profile_name = profile_name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Убрать профиль из менеджера?", classes="dialog-title")
            yield Static(Text(self.profile_name))
            yield Static("Файлы профиля и авторизация сохранятся.", classes="muted")
            with Horizontal(classes="dialog-actions"):
                yield Button("Отмена", id="cancel", compact=True)
                yield Button("Убрать", id="remove-confirm", variant="error", compact=True)

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "remove-confirm")


class ExistingProfiles(ModalScreen[dict[str, str] | None]):
    BINDINGS = [("escape", "dismiss(None)", "Отмена")]

    def __init__(self, candidates: list[dict[str, str]]):
        super().__init__()
        self.candidates = candidates

    def compose(self) -> ComposeResult:
        with Vertical(id="discovery-dialog"):
            yield Label("Существующие профили AGY / Codex / Claude", classes="dialog-title")
            yield Static(
                "Выбери профиль для регистрации. Его файлы останутся на месте.", classes="muted"
            )
            yield DataTable(id="existing-table", cursor_type="row")
            with Horizontal(classes="dialog-actions"):
                yield Button("Закрыть", id="cancel", compact=True)
                yield Button("Импортировать", id="import-existing", variant="primary", compact=True)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Harness", "Профиль", "Каталог")
        for profile in self.candidates:
            table.add_row(profile["provider"], Text(profile["name"]), Text(profile["path"]))

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#import-existing")
    @on(DataTable.RowSelected)
    def select(self) -> None:
        row = self.query_one(DataTable).cursor_row
        if self.candidates:
            self.dismiss(self.candidates[row])


class ProxyForm(ModalScreen[dict[str, Any] | None]):
    BINDINGS = [("escape", "dismiss(None)", "Отмена")]

    def __init__(self, title: str, policy: dict[str, Any]):
        super().__init__()
        self.title_text, self.policy = title, policy

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Label("Режим подключения")
            yield Select(
                [
                    ("Наследовать", "inherit"),
                    ("Напрямую", "direct"),
                    ("Свой HTTP(S)-прокси", "url"),
                ],
                value=str(self.policy["mode"]),
                allow_blank=False,
                id="proxy-mode",
            )
            yield Label("URL прокси · ввод скрыт")
            yield Input(
                value=str(self.policy.get("url", "")),
                password=True,
                placeholder="http://127.0.0.1:10809",
                id="proxy-url",
            )
            yield Label("Исключения через запятую · пусто = localhost")
            yield Input(value=str(self.policy.get("no_proxy", "")), id="proxy-bypass")
            yield Static(
                "Профиль наследует общий прокси; общий — окружение терминала. "
                "Режим «напрямую» отключает оба уровня.",
                classes="muted",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Отмена", id="cancel", compact=True)
                yield Button("Сохранить", id="save", variant="primary", compact=True)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        mode = str(self.query_one("#proxy-mode", Select).value)
        policy: dict[str, Any] = {"mode": mode}
        if mode == "url":
            policy["url"] = self.query_one("#proxy-url", Input).value.strip()
        if bypass := self.query_one("#proxy-bypass", Input).value.strip():
            policy["no_proxy"] = bypass
        try:
            validate_proxy(policy)
        except HubError as exc:
            self.notify(str(exc), severity="error")
            return
        self.dismiss(policy)


class InstallForm(ModalScreen[dict[str, str | None] | None]):
    BINDINGS = [("escape", "dismiss(None)", "Отмена")]

    def __init__(self, hub: Hub, provider: str, profile: str | None):
        super().__init__()
        self.hub, self.provider, self.profile = hub, provider, profile
        self.plan = Installer(hub).plan(provider)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Установка · {self.plan['name']}", classes="dialog-title")
            yield Static(
                Text(
                    f"Официальный источник\n{self.plan['source']}\n\n"
                    f"Нужно: {', '.join(self.plan['requires'])}"
                )
            )
            yield Label("Прокси для загрузки")
            choices = [("Общие настройки", "global")]
            if self.profile:
                choices.append((f"Прокси профиля {self.profile}", "profile"))
            yield Select(choices, value="global", allow_blank=False, id="install-proxy")
            yield Static(id="install-proxy-summary", classes="muted")
            yield Static(
                "Скачается официальный Windows-установщик. Его запуск — отдельный шаг."
                if self.plan["download_only"]
                else "Оригинальный клиент установится в отдельный каталог менеджера. "
                "Авторизация выполняется после установки.",
                classes="muted",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Отмена", id="cancel", compact=True)
                yield Button(
                    "Скачать" if self.plan["download_only"] else "Установить",
                    id="install-confirm",
                    variant="primary",
                    compact=True,
                )

    def on_mount(self) -> None:
        self.update_proxy_summary()

    @on(Select.Changed, "#install-proxy")
    def update_proxy_summary(self) -> None:
        selected = str(self.query_one("#install-proxy", Select).value)
        plan = Installer(self.hub).plan(
            self.provider, self.profile if selected == "profile" else None
        )
        self.query_one("#install-proxy-summary", Static).update(
            Text(f"{plan['proxy_source']}: {plan['proxy'].get('url', plan['proxy']['mode'])}")
        )

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#install-confirm")
    def install(self) -> None:
        selected = str(self.query_one("#install-proxy", Select).value)
        self.dismiss({"profile": self.profile if selected == "profile" else None})


class HandoffForm(ModalScreen[dict[str, str] | None]):
    BINDINGS = [("escape", "dismiss(None)", "Отмена")]

    def __init__(self, session: dict[str, Any], choices: list[tuple[str, str]] | None = None):
        super().__init__()
        self.session = session
        self.choices = choices or list(FALLBACK_MODELS)

    def compose(self) -> ComposeResult:
        default_val = self.choices[0][1]
        with Vertical(id="dialog"):
            yield Label("Перенос диалога (Cross-Harness Handoff)", classes="dialog-title")
            yield Static(
                Text(
                    f"Источник: {self.session['provider']}/{self.session['profile']} · "
                    f"{self.session['title']}"
                ),
                classes="muted",
            )
            yield Label("Целевая модель (автовыбор лучшего аккаунта по квоте)")
            yield Select(
                self.choices,
                value=default_val,
                allow_blank=False,
                id="handoff-model",
            )
            yield Label("Дополнительное указание новому агенту (необязательно)")
            yield Input(
                placeholder="Продолжи реализацию и запусти тесты",
                id="handoff-note",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Отмена", id="cancel", compact=True)
                yield Button(
                    "Передать и запустить", id="confirm-handoff", variant="primary", compact=True
                )

    def on_mount(self) -> None:
        self.query_one("#handoff-note", Input).focus()

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm-handoff")
    def confirm(self) -> None:
        self.dismiss(
            {
                "session_id": str(self.session["session_id"]),
                "model": str(self.query_one("#handoff-model", Select).value),
                "note": self.query_one("#handoff-note", Input).value.strip(),
            }
        )


class ConsultForm(ModalScreen[dict[str, Any] | None]):
    BINDINGS = [("escape", "dismiss(None)", "Отмена")]

    def __init__(self, default_model: str = "", choices: list[tuple[str, str]] | None = None):
        super().__init__()
        self.base_choices = choices or list(FALLBACK_MODELS)
        valid_values = {v for _, v in self.base_choices}
        self.default_model = (
            default_model if default_model in valid_values else self.base_choices[0][1]
        )

    def compose(self) -> ComposeResult:
        choices: list[tuple[str, str]] = [
            ("Консилиум 3 семейств (Gemini + GPT + Claude)", "__council__"),
            *self.base_choices,
        ]
        with Vertical(id="dialog"):
            yield Label("Второе мнение · Консультация с контекстом", classes="dialog-title")
            yield Static(
                "Автоматически прикрепляет хвост последнего диалога проекта и git diff.",
                classes="muted",
            )
            yield Label("Эксперт или консилиум")
            yield Select(
                choices,
                value=self.default_model,
                allow_blank=False,
                id="consult-target",
            )
            yield Label("Вопрос или архитектурная развилка")
            yield Input(
                placeholder="Проверь текущие изменения на гонки и edge-cases",
                id="consult-question",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Отмена", id="cancel", compact=True)
                yield Button("Спросить", id="confirm-consult", variant="primary", compact=True)

    def on_mount(self) -> None:
        self.query_one("#consult-question", Input).focus()

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm-consult")
    @on(Input.Submitted, "#consult-question")
    def confirm(self) -> None:
        q = self.query_one("#consult-question", Input).value.strip()
        if not q:
            self.notify("Введи вопрос для консультации", severity="warning")
            return
        target = str(self.query_one("#consult-target", Select).value)
        self.dismiss(
            {
                "question": q,
                "council": target == "__council__",
                "model": "" if target == "__council__" else target,
            }
        )


class ResumeProfileForm(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Отмена")]

    def __init__(self, session: dict[str, Any], profiles: list[str], default_profile: str):
        super().__init__()
        self.session = session
        self.profile_names = profiles
        self.default_profile = default_profile

    def compose(self) -> ComposeResult:
        opts = [(p, p) for p in self.profile_names]
        with Vertical(id="dialog"):
            yield Label("Продолжить диалог с другого аккаунта", classes="dialog-title")
            yield Static(
                Text(f"Диалог: {self.session['title']}\nID: {self.session['session_id']}"),
                classes="muted",
            )
            yield Label(f"Профиль ({self.session['provider']})")
            yield Select(
                opts,
                value=self.default_profile
                if self.default_profile in self.profile_names
                else self.profile_names[0],
                allow_blank=False,
                id="resume-profile-select",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Отмена", id="cancel", compact=True)
                yield Button("Продолжить", id="confirm-resume", variant="primary", compact=True)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm-resume")
    def confirm(self) -> None:
        self.dismiss(str(self.query_one("#resume-profile-select", Select).value))
