from __future__ import annotations

import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from harnesshub.errors import HubError


@dataclass(frozen=True)
class Harness:
    id: str
    name: str
    commands: tuple[str, ...]
    config_env: str | None
    login_args: tuple[str, ...]
    docs: str
    note: str = ""
    platforms: tuple[str, ...] = ("linux", "darwin", "win32")
    auth_env: tuple[str, ...] = ()
    launcher: tuple[str, ...] = ()
    discover_on_path: bool = True
    runtime_env: tuple[tuple[str, str], ...] = ()

    def executable(self) -> str | None:
        if self.launcher:
            if shutil.which(self.launcher[0]) and Path(self.commands[0]).is_file():
                return self.commands[0]
            return None
        if not self.discover_on_path:
            return None
        for cmd in self.commands:
            path = shutil.which(cmd)
            if path is None:
                continue
            if self.id == "kimi":
                try:
                    with Path(path).open("rb") as stream:
                        prefix = stream.read(4096)
                    if b"from kimi_cli" not in prefix and b"import kimi_cli" not in prefix:
                        continue
                except OSError:
                    continue
            return path
        return None

    def profile_supported(self) -> bool:
        return self.config_env is not None and sys.platform in self.platforms

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "executable": self.executable(),
            "profiles_supported": self.profile_supported(),
        }


BUILTINS = (
    Harness(
        "claude",
        "Claude Code",
        ("claude",),
        "CLAUDE_CONFIG_DIR",
        ("auth", "login", "--claudeai"),
        "https://code.claude.com/docs/en/authentication",
        auth_env=(
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_SECURESTORAGE_CONFIG_DIR",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "ANTHROPIC_PROFILE",
            "ANTHROPIC_FEDERATION_RULE_ID",
            "ANTHROPIC_ORGANIZATION_ID",
        ),
    ),
    Harness(
        "codex",
        "OpenAI Codex",
        ("codex",),
        "CODEX_HOME",
        ("login",),
        "https://developers.openai.com/codex/auth/",
        auth_env=(
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "CODEX_API_KEY",
            "CODEX_ACCESS_TOKEN",
            "OPENAI_FEDERATION_RULE_ID",
            "OPENAI_IDENTITY_TOKEN_FILE",
            "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
        ),
    ),
    Harness(
        "agy",
        "Antigravity",
        ("agy",),
        "HOME",
        (),
        "https://antigravity.google/docs/getting-started?tab=cli",
        "Изоляция аккаунтов через HOME/USERPROFILE; встроенный автохил только на Linux x64.",
        platforms=("linux", "darwin", "win32"),
        auth_env=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    ),
    Harness(
        "qoder",
        "Qoder CLI",
        ("qoder", "qodercli"),
        "QODER_CONFIG_DIR",
        (),
        "https://docs.qoder.com/cli/authentication",
        "Вход: /login. Разделение двух аккаунтов требует живой проверки.",
        auth_env=("QODER_PERSONAL_ACCESS_TOKEN",),
    ),
    Harness(
        "kimi",
        "Kimi CLI · legacy",
        ("kimi",),
        "KIMI_SHARE_DIR",
        ("login",),
        "https://www.kimi.com/code/docs/en/kimi-cli.html",
        auth_env=("KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL_NAME", "MOONSHOT_API_KEY"),
        note="Существующий kimi-cli. Для свежей установки выбери Kimi Code.",
    ),
    Harness(
        "kimi-code",
        "Kimi Code",
        ("kimi",),
        "KIMI_CODE_HOME",
        (),
        "https://moonshotai.github.io/kimi-code/en/guides/getting-started",
        "Новый официальный клиент; для входа введи /login.",
        auth_env=("KIMI_API_KEY", "KIMI_BASE_URL", "MOONSHOT_API_KEY"),
        discover_on_path=False,
    ),
    Harness(
        "copilot",
        "GitHub Copilot",
        ("copilot",),
        None,
        ("login",),
        "https://docs.github.com/en/copilot/how-tos/copilot-cli",
        "Запуск родного клиента; аккаунты переключаются через /user.",
        auth_env=(
            "COPILOT_PROVIDER_BASE_URL",
            "COPILOT_PROVIDER_API_KEY",
            "COPILOT_GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        ),
    ),
    Harness(
        "cursor",
        "Cursor Agent",
        ("cursor-agent", "agent"),
        None,
        ("login",),
        "https://cursor.com/docs/cli/overview",
        "Запуск родного клиента.",
        auth_env=("CURSOR_API_KEY",),
    ),
    Harness(
        "gemini",
        "Gemini CLI",
        ("gemini",),
        None,
        (),
        "https://geminicli.com/docs/get-started/authentication/",
        "Вход через Google; доступ зависит от аккаунта и плана.",
        auth_env=("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"),
    ),
    Harness(
        "kiro",
        "Kiro CLI",
        ("kiro-cli",),
        None,
        ("login",),
        "https://kiro.dev/docs/reference/cli-commands/",
        "Запуск родного клиента.",
    ),
    Harness(
        "junie",
        "JetBrains Junie",
        ("junie",),
        None,
        (),
        "https://junie.jetbrains.com/docs/junie-cli.html",
        "Вход через JetBrains account.",
        auth_env=(
            "JUNIE_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
        ),
    ),
)


def registry(custom: list[dict] | None = None) -> dict[str, Harness]:
    result = {h.id: h for h in BUILTINS}
    for item in custom or []:
        key = item["id"]
        if key in result:
            raise HubError(f"Harness {key!r} уже существует")
        result[key] = Harness(
            id=key,
            name=item["name"],
            commands=(item["command"],),
            config_env=item.get("config_env"),
            login_args=tuple(item.get("login_args", [])),
            docs=item.get("docs", ""),
            note="Пользовательский адаптер; изоляцию проверяет владелец.",
        )
    return result


def default_config_path(provider: str, home: Path) -> Path | None:
    relative = {
        "claude": ".claude",
        "codex": ".codex",
        "agy": ".gemini/antigravity-cli",
        "qoder": ".qoder",
        "kimi": ".kimi",
        "kimi-code": ".kimi-code",
    }
    return home / relative[provider] if provider in relative else None
