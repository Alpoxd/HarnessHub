from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from harnesshub.errors import BusyError, HubError
from harnesshub.locking import FileLock, resource_lock
from harnesshub.network import proxy_environment, public_proxy
from harnesshub.providers.base import is_profile_locked

if TYPE_CHECKING:
    from harnesshub.service import Hub


@dataclass(frozen=True)
class IdeTarget:
    id: str
    name: str
    commands: tuple[str, ...]
    extensions_subdir: str
    default_provider: str | None


IDE_TARGETS: dict[str, IdeTarget] = {
    "agy": IdeTarget(
        id="agy",
        name="Antigravity IDE",
        commands=("antigravity", "antigravity-ide"),
        extensions_subdir=".antigravity/extensions",
        default_provider="agy",
    ),
    "vscode": IdeTarget(
        id="vscode",
        name="Visual Studio Code",
        commands=("code",),
        extensions_subdir=".vscode/extensions",
        default_provider=None,
    ),
    "cursor": IdeTarget(
        id="cursor",
        name="Cursor IDE",
        commands=("cursor",),
        extensions_subdir=".cursor/extensions",
        default_provider="cursor",
    ),
}

IDE_ALIASES: dict[str, str] = {
    "antigravity": "agy",
    "antigravity-ide": "agy",
    "code": "vscode",
    "vs-code": "vscode",
    "claude": "vscode",
    "codex": "vscode",
}

_PROVIDER_ISOLATED_KEYS: dict[str, tuple[str, ...]] = {
    "agy": (
        "HOME",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    ),
    "claude": ("CLAUDE_CONFIG_DIR",),
    "codex": ("CODEX_HOME",),
}


def resolve_ide_target(target: str) -> IdeTarget:
    cleaned = target.strip().lower()
    key = IDE_ALIASES.get(cleaned, cleaned)
    if key not in IDE_TARGETS:
        available = ", ".join(sorted(IDE_TARGETS))
        raise HubError(
            f"Неизвестная IDE: {target}. Доступны: {available} (или claude/codex -> vscode)"
        )
    return IDE_TARGETS[key]


def find_ide_executable(spec: IdeTarget) -> str | None:
    for cmd in spec.commands:
        path = shutil.which(cmd)
        if path:
            return path
    return None


def build_ide_plan(
    hub: Hub,
    target: str,
    *,
    profile: str | None = None,
    agy_profile: str | None = None,
    claude_profile: str | None = None,
    codex_profile: str | None = None,
    cwd: Path | None = None,
    wait: bool = False,
    extra_args: tuple[str, ...] = (),
) -> dict[str, Any]:
    spec = resolve_ide_target(target)
    cleaned_target = target.strip().lower()

    if cleaned_target == "claude" and not claude_profile and profile:
        claude_profile = profile
    elif cleaned_target == "codex" and not codex_profile and profile:
        codex_profile = profile
    elif spec.id == "agy" and not agy_profile and profile:
        agy_profile = profile
    elif profile is not None:
        if spec.id == "cursor":
            raise HubError(f"Изоляция профилей для {spec.name} пока не поддерживается")
        raise HubError("Для VS Code укажи --agy-profile, --claude-profile или --codex-profile")

    selected_profiles: dict[str, dict[str, Any]] = {}
    if agy_profile:
        selected_profiles["agy"] = hub.profile("agy", agy_profile)
    elif spec.id == "agy" and hub.profiles("agy"):
        selected_profiles["agy"] = hub.profile("agy", None)

    if claude_profile:
        selected_profiles["claude"] = hub.profile("claude", claude_profile)
    elif cleaned_target == "claude" and hub.profiles("claude"):
        selected_profiles["claude"] = hub.profile("claude", None)

    if codex_profile:
        selected_profiles["codex"] = hub.profile("codex", codex_profile)
    elif cleaned_target == "codex" and hub.profiles("codex"):
        selected_profiles["codex"] = hub.profile("codex", None)

    env = dict(os.environ)
    real_home = Path(env.get("HOME", str(Path.home())))
    isolated_env: dict[str, str] = {}

    primary_profile: dict[str, Any] | None = None
    for prov in ("agy", "claude", "codex"):
        if prov not in selected_profiles:
            continue
        prof = selected_profiles[prov]
        if primary_profile is None:
            primary_profile = prof
        prov_env = hub.environment(prov, prof)
        for k in _PROVIDER_ISOLATED_KEYS[prov]:
            if k in prov_env:
                env[k] = prov_env[k]
                isolated_env[k] = prov_env[k]
        for k in hub.harness(prov).auth_env:
            env.pop(k, None)
        if prov == "codex":
            env.pop("CODEX_SQLITE_HOME", None)

    policy, proxy_source = (
        hub.proxy_policy(primary_profile["provider"], primary_profile)
        if primary_profile
        else hub.proxy_policy()
    )
    env = proxy_environment(env, policy)
    env["DONT_PROMPT_WSL_INSTALL"] = "1"

    workspace = (cwd or Path.cwd()).expanduser().resolve()
    if primary_profile:
        user_data_dir = Path(primary_profile["path"]) / ".ide-data" / spec.id
    else:
        user_data_dir = hub.store.root / "ide-data" / spec.id

    executable = find_ide_executable(spec)
    command = [executable or spec.commands[0]]
    command.extend(["--user-data-dir", str(user_data_dir)])
    command.append("--new-window")
    if wait:
        command.append("--wait")

    ext_dir = real_home / spec.extensions_subdir
    if ext_dir.is_dir():
        command.extend(["--extensions-dir", str(ext_dir)])

    if policy.get("mode") == "url" and policy.get("url"):
        parsed_proxy = urlsplit(str(policy["url"]))
        if parsed_proxy.username is None and parsed_proxy.password is None:
            command.extend(["--proxy-server", str(policy["url"])])

    command.extend(extra_args)
    command.append(str(workspace))

    return {
        "ide": spec.id,
        "name": spec.name,
        "executable": executable,
        "command": command,
        "cwd": str(workspace),
        "user_data_dir": str(user_data_dir),
        "profiles": {k: v["name"] for k, v in selected_profiles.items()},
        "proxy": public_proxy(policy),
        "proxy_source": proxy_source,
        "isolated_env": isolated_env,
        "_selected_profiles": list(selected_profiles.values()),
        "env": env,
    }


def launch_ide(
    hub: Hub,
    target: str,
    *,
    profile: str | None = None,
    agy_profile: str | None = None,
    claude_profile: str | None = None,
    codex_profile: str | None = None,
    cwd: Path | None = None,
    wait: bool = False,
    dry_run: bool = False,
    extra_args: tuple[str, ...] = (),
) -> dict[str, Any]:
    plan = build_ide_plan(
        hub,
        target,
        profile=profile,
        agy_profile=agy_profile,
        claude_profile=claude_profile,
        codex_profile=codex_profile,
        cwd=cwd,
        wait=wait,
        extra_args=extra_args,
    )
    env = plan.pop("env")
    selected_list: list[dict[str, Any]] = plan.pop("_selected_profiles")
    if dry_run:
        return {**plan, "status": "dry-run"}
    if not plan["executable"]:
        raise HubError(f"{plan['name']} не найден в PATH ({plan['command'][0]})")
    for prof in selected_list:
        if is_profile_locked(hub, prof):
            raise BusyError(
                f"Профиль {prof['provider']}/{prof['name']} занят эксклюзивной операцией"
            )
    Path(plan["user_data_dir"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    if wait:
        with ExitStack() as stack:
            for prof in selected_list:
                stack.enter_context(
                    FileLock(resource_lock(Path(prof["path"]), hub.lock_root), shared=True)
                )
            completed = subprocess.run(plan["command"], env=env, cwd=plan["cwd"], check=False)
        return {**plan, "status": "completed", "returncode": completed.returncode}
    process = subprocess.Popen(
        plan["command"],
        env=env,
        cwd=plan["cwd"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {**plan, "status": "launched", "pid": process.pid}
