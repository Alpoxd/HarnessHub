from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harnesshub.errors import BusyError, HubError
from harnesshub.locking import FileLock, resource_lock
from harnesshub.network import proxy_environment, public_proxy, resolve_proxy, validate_proxy
from harnesshub.registry import Harness, default_config_path, registry
from harnesshub.store import StateStore, validate_name


class Hub:
    def __init__(self, root: Path, lock_root: Path | None = None):
        self.store = StateStore(root)
        self.lock_root = lock_root

    def harnesses(self) -> dict[str, Harness]:
        state = self.store.read()
        harnesses = registry(state["custom"])
        for provider, install in state["installations"].items():
            directory = Path(install["directory"]).resolve()
            if not directory.is_relative_to(self.store.root / "tools" / provider):
                raise HubError("Каталог установленного harness выходит за пределы tools")
            harnesses[provider] = replace(
                harnesses[provider],
                commands=(install["executable"],),
                launcher=tuple(install["launcher"]),
                discover_on_path=True,
                runtime_env=tuple(install.get("env", {}).items()),
            )
        if configured := state["settings"].get("agy_binary"):
            harnesses["agy"] = replace(harnesses["agy"], commands=(configured,), launcher=())
        return harnesses

    def harness(self, provider: str) -> Harness:
        try:
            return self.harnesses()[provider]
        except KeyError as exc:
            raise HubError(f"Неизвестный harness: {provider}") from exc

    def profiles(self, provider: str | None = None) -> list[dict[str, Any]]:
        state = self.store.read()
        return [
            {
                **p,
                **({"proxy": public_proxy(p["proxy"])} if "proxy" in p else {}),
                "default": state["defaults"].get(p["provider"]) == p["name"],
            }
            for p in state["profiles"]
            if provider is None or p["provider"] == provider
        ]

    def profile(self, provider: str, name: str | None = None) -> dict[str, Any]:
        self.harness(provider)
        state = self.store.read()
        name = name or state["defaults"].get(provider)
        for p in state["profiles"]:
            if p["provider"] == provider and p["name"] == name:
                return dict(p)
        raise HubError(f"Профиль {provider}/{name or 'по умолчанию'} не найден")

    def _require_profiles(self, provider: str) -> Harness:
        spec = self.harness(provider)
        if not spec.profile_supported():
            raise HubError(
                f"Изоляция {spec.name} на {sys.platform} пока не поддерживается; "
                "используй run --native"
            )
        return spec

    @staticmethod
    def _register(state: dict[str, Any], profile: dict[str, Any]) -> None:
        key = (profile["provider"], profile["name"])
        location = os.path.normcase(str(Path(profile["path"]).resolve()))
        for item in state["profiles"]:
            if (item["provider"], item["name"]) == key:
                raise HubError("Профиль с таким именем уже зарегистрирован")
            if os.path.normcase(str(Path(item["path"]).resolve())) == location:
                raise HubError("Этот каталог уже зарегистрирован под другим именем")
        state["profiles"].append(profile)
        state["defaults"].setdefault(profile["provider"], profile["name"])

    def create(self, provider: str, name: str) -> dict[str, Any]:
        self._require_profiles(provider)
        validate_name(name)
        path = self.store.root / "profiles" / provider / name
        profile = {
            "provider": provider,
            "name": name,
            "path": str(path),
            "external": False,
            "autoheal": False,
            "created_at": datetime.now(UTC).isoformat(),
        }

        def change(state: dict[str, Any]) -> None:
            self._register(state, profile)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                path.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise HubError(f"Каталог уже существует: {path}; используй import") from exc
            if provider == "codex":
                config = path / "config.toml"
                config.write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
                config.chmod(0o600)
            elif provider == "agy":
                (path / ".gemini" / "antigravity-cli").mkdir(mode=0o700, parents=True)
                if sys.platform == "win32":
                    (path / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
                    (path / "AppData" / "Local").mkdir(parents=True, exist_ok=True)

        self.store.update(change)
        if provider == "agy" and self.store.read()["settings"].get("agy_shared_history", False):
            from harnesshub.agy_workspace import ensure_agy_profile_shared_history

            ensure_agy_profile_shared_history(self, profile)
        return profile

    def import_profile(self, provider: str, name: str, path: Path) -> dict[str, Any]:
        self._require_profiles(provider)
        validate_name(name)
        path = path.expanduser().resolve(strict=True)
        if not path.is_dir():
            raise HubError("Для импорта нужен каталог профиля")
        if path == self.store.root or path.is_relative_to(self.store.root):
            raise HubError("Каталог данных менеджера нельзя использовать как профиль")
        profile = {
            "provider": provider,
            "name": name,
            "path": str(path),
            "external": True,
            "autoheal": False,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.store.update(lambda state: self._register(state, profile))
        if provider == "agy" and self.store.read()["settings"].get("agy_shared_history", False):
            from harnesshub.agy_workspace import ensure_agy_profile_shared_history

            ensure_agy_profile_shared_history(self, profile)
        return profile

    def set_default(self, provider: str, name: str) -> None:
        self.profile(provider, name)

        def change(state: dict[str, Any]) -> None:
            if not any(p["provider"] == provider and p["name"] == name for p in state["profiles"]):
                raise HubError("Профиль был удалён другим процессом")
            state["defaults"][provider] = name

        self.store.update(change)

    def rename(self, provider: str, name: str, new_name: str) -> None:
        validate_name(new_name)

        def change(state: dict[str, Any]) -> None:
            if any(p["provider"] == provider and p["name"] == new_name for p in state["profiles"]):
                raise HubError("Профиль с новым именем уже существует")
            for p in state["profiles"]:
                if p["provider"] == provider and p["name"] == name:
                    p["name"] = new_name
                    if state["defaults"].get(provider) == name:
                        state["defaults"][provider] = new_name
                    return
            raise HubError("Профиль не найден")

        self.store.update(change)

    def remove(self, provider: str, name: str) -> None:
        profile = self.profile(provider, name)
        with FileLock(resource_lock(Path(profile["path"]), self.lock_root), shared=False):

            def change(state: dict[str, Any]) -> None:
                state["profiles"] = [
                    p for p in state["profiles"] if (p["provider"], p["name"]) != (provider, name)
                ]
                if state["defaults"].get(provider) == name:
                    state["defaults"].pop(provider)

            self.store.update(change)

    def environment(self, provider: str, profile: dict[str, Any] | None = None) -> dict[str, str]:
        spec = self.harness(provider)
        env = dict(os.environ)
        for key in spec.auth_env:
            env.pop(key, None)
        env.update(dict(spec.runtime_env))
        if profile is not None:
            self._require_profiles(provider)
            if spec.config_env is None:
                raise HubError(f"Изоляция {spec.name} не поддерживается")
            path = Path(profile["path"])
            if not path.is_dir():
                raise HubError(f"Каталог профиля отсутствует: {path}")
            env[spec.config_env] = str(path)
            if provider == "codex":
                env.pop("CODEX_SQLITE_HOME", None)
            elif provider == "agy":
                from harnesshub.agy_workspace import resolve_real_home

                settings = self.store.read()["settings"]
                if settings.get("agy_locale_override", True):
                    env["LANG"] = "en_US.UTF-8"
                    env["LC_ALL"] = "C.UTF-8"
                    env["LANGUAGE"] = "en_US"
                if sys.platform == "win32":
                    env["USERPROFILE"] = str(path)
                    env["APPDATA"] = str(path / "AppData" / "Roaming")
                    env["LOCALAPPDATA"] = str(path / "AppData" / "Local")
                    drive, homepath = os.path.splitdrive(str(path))
                    if drive:
                        env["HOMEDRIVE"] = drive
                        env["HOMEPATH"] = homepath or "\\"
                else:
                    real_home = resolve_real_home(self)
                    resolved_path = path.resolve()
                    token_file = path / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                    if resolved_path != real_home or (
                        token_file.is_file() and token_file.stat().st_size > 0
                    ):
                        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/bus"
                    env["XDG_CONFIG_HOME"] = str(path / ".config")
                    env["XDG_CACHE_HOME"] = str(path / ".cache")
                    env["XDG_DATA_HOME"] = str(path / ".local" / "share")
        policy, _ = self.proxy_policy(provider, profile)
        return proxy_environment(env, policy)

    def proxy_policy(
        self, provider: str | None = None, profile: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str]:
        state = self.store.read()
        return resolve_proxy(state["network"]["global"], profile.get("proxy") if profile else None)

    def proxy_info(self, provider: str | None = None, name: str | None = None) -> dict[str, Any]:
        profile = self.profile(provider, name) if provider is not None else None
        policy, source = self.proxy_policy(provider, profile)
        configured = (
            profile.get("proxy", {"mode": "inherit"})
            if profile
            else (self.store.read()["network"]["global"])
        )
        return {
            "configured": public_proxy(configured),
            "effective": public_proxy(policy),
            "source": source,
        }

    def set_proxy(
        self, policy: dict[str, Any], provider: str | None = None, name: str | None = None
    ) -> dict[str, Any]:
        validate_proxy(policy)
        profile = self.profile(provider, name) if provider is not None else None

        def change(state: dict[str, Any]) -> None:
            if profile is None:
                state["network"]["global"] = dict(policy)
                return
            for existing in state["profiles"]:
                if (existing["provider"], existing["name"]) == (provider, profile["name"]):
                    existing["proxy"] = dict(policy)
                    return
            raise HubError("Профиль был удалён другим процессом")

        self.store.update(change)
        return self.proxy_info(provider, profile["name"] if profile else None)

    def automode_enabled(
        self,
        provider: str,
        profile: dict[str, Any] | None = None,
        *,
        override: bool | None = None,
    ) -> bool:
        if override is not None:
            return bool(override)
        state = self.store.read()
        fresh = profile
        if profile is not None:
            pname = profile.get("name")
            for existing in state["profiles"]:
                if existing["provider"] == provider and existing["name"] == pname:
                    fresh = existing
                    break
        if fresh is not None and "automode" in fresh:
            return bool(fresh["automode"])
        settings = state["settings"]
        if provider == "agy" and "agy_automode" in settings:
            return bool(settings["agy_automode"])
        return bool(settings.get("automode", False))

    def automode_info(self, provider: str | None = None, name: str | None = None) -> dict[str, Any]:
        settings = self.store.read()["settings"]
        profile = (
            self.profile(provider, name) if (provider is not None and name is not None) else None
        )
        if profile is not None:
            assert provider is not None
            if "automode" in profile:
                configured: str | bool = bool(profile["automode"])
                source = "profile"
            else:
                configured = "inherit"
                source = (
                    "agy_automode"
                    if (provider == "agy" and "agy_automode" in settings)
                    else "global"
                )
            return {
                "provider": provider,
                "profile": profile["name"],
                "configured": configured,
                "effective": self.automode_enabled(provider, profile),
                "source": source,
            }
        target_prov = provider or "agy"
        if target_prov == "agy":
            if "agy_automode" in settings:
                configured = bool(settings["agy_automode"])
                source = "agy_automode"
            elif "automode" in settings:
                configured = bool(settings["automode"])
                source = "global"
            else:
                configured = False
                source = "default"
        else:
            configured = bool(settings.get("automode", False))
            source = "global" if "automode" in settings else "default"
        return {
            "provider": target_prov,
            "profile": None,
            "configured": configured,
            "effective": self.automode_enabled(target_prov),
            "source": source,
        }

    def set_automode(
        self,
        enabled: bool | None,
        provider: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        if name is not None and provider is None:
            provider = "agy"
        if provider is not None:
            self.harness(provider)
        profile = (
            self.profile(provider, name) if (provider is not None and name is not None) else None
        )

        def change(state: dict[str, Any]) -> None:
            if profile is not None:
                for existing in state["profiles"]:
                    if (existing["provider"], existing["name"]) == (provider, profile["name"]):
                        if enabled is None:
                            existing.pop("automode", None)
                        else:
                            existing["automode"] = bool(enabled)
                        return
                raise HubError("Профиль был удалён другим процессом")
            if provider == "agy":
                if enabled is None:
                    state["settings"].pop("agy_automode", None)
                else:
                    state["settings"]["agy_automode"] = bool(enabled)
            else:
                if enabled is None:
                    state["settings"].pop("automode", None)
                else:
                    state["settings"]["automode"] = bool(enabled)

        self.store.update(change)
        return self.automode_info(provider, profile["name"] if profile else None)

    def command(
        self,
        provider: str,
        profile: dict[str, Any] | None,
        login: bool = False,
        args: tuple[str, ...] = (),
        cwd: Path | None = None,
        *,
        automode: bool | None = None,
    ) -> list[str]:
        spec = self.harness(provider)
        executable = spec.executable()
        if executable is None:
            raise HubError(f"{spec.name} не найден в PATH. Установка: {spec.docs}")
        command = list(spec.launcher) if spec.launcher else [executable]
        if provider == "codex" and profile is not None:
            command.extend(["-c", 'cli_auth_credentials_store="file"'])
        if login:
            command.extend(spec.login_args)
        else:
            if provider == "agy" and profile is not None:
                command.extend(["--add-dir", str((cwd or Path.cwd()).resolve())])
            if self.automode_enabled(provider, profile, override=automode):
                if provider == "agy":
                    has_mode = any(a == "--mode" or a.startswith("--mode=") for a in args)
                    if "--dangerously-skip-permissions" not in args and not has_mode:
                        command.append("--dangerously-skip-permissions")
                elif provider == "claude":
                    has_perm = any(
                        a == "--permission-mode" or a.startswith("--permission-mode=") for a in args
                    )
                    if "--dangerously-skip-permissions" not in args and not has_perm:
                        command.append("--dangerously-skip-permissions")
                elif provider == "codex":
                    has_sandbox = any(
                        a
                        in (
                            "--yolo",
                            "--dangerously-bypass-approvals-and-sandbox",
                            "-s",
                            "--sandbox",
                            "-a",
                            "--ask-for-approval",
                        )
                        or a.startswith(("--sandbox=", "--ask-for-approval="))
                        for a in args
                    )
                    if not has_sandbox:
                        command.append("--yolo")
        command.extend(args)
        return command

    def launch(
        self,
        provider: str,
        name: str | None = None,
        *,
        native: bool = False,
        login: bool = False,
        automode: bool | None = None,
        args: tuple[str, ...] = (),
        cwd: Path | None = None,
    ) -> int:
        if native and name is not None:
            raise HubError("--native и выбранный профиль нельзя использовать вместе")
        profile = None if native else self.profile(provider, name)
        if (
            provider == "agy"
            and profile is not None
            and not login
            and self.store.read()["settings"].get("agy_shared_history", False)
        ):
            from harnesshub.agy_workspace import ensure_agy_profile_shared_history

            ensure_agy_profile_shared_history(self, profile)
        env = self.environment(provider, profile)
        cwd = (cwd or Path.cwd()).expanduser().resolve(strict=True)
        if not cwd.is_dir():
            raise HubError("Рабочий проект должен быть каталогом")
        cmd_kwargs = {"automode": automode} if automode is not None else {}
        command = self.command(provider, profile, login, args, cwd, **cmd_kwargs)
        if profile:
            identity = Path(profile["path"])
        else:
            spec = self.harness(provider)
            relocated = env.get(spec.config_env) if spec.config_env else None
            home = Path(env.get("USERPROFILE") or env.get("HOME") or str(Path.home()))
            if relocated:
                identity = Path(relocated).expanduser()
                if not identity.is_absolute():
                    identity = cwd / identity
            else:
                identity = default_config_path(provider, home) or home / f".{provider}"
        with FileLock(resource_lock(identity, self.lock_root), shared=not login):
            if profile and profile.get("autoheal") and provider == "agy":
                from harnesshub.agy import AgyMaintenance

                policy, _ = self.proxy_policy(provider, profile)
                AgyMaintenance(self).heal(
                    env=proxy_environment(dict(os.environ), policy),
                    fast_check=True,
                )
            from harnesshub.proc import wrap_windows_command

            process = subprocess.Popen(wrap_windows_command(command), env=env, cwd=cwd)
            while True:
                try:
                    code = process.wait()
                    return code if code >= 0 else 128 - code
                except KeyboardInterrupt:
                    # The foreground child receives terminal signals directly.
                    continue

    def existing_profiles(self, home: Path | None = None) -> list[dict[str, str]]:
        from harnesshub.agy_workspace import resolve_real_home

        home = home or resolve_real_home(self)
        registered = {os.path.normcase(str(Path(p["path"]).resolve())) for p in self.profiles()}
        candidates = []
        roots = {
            "agy": Path(os.environ.get("AGY_PROFILES", home / "agy-profiles")),
            "codex": Path(os.environ.get("CODEX_PROFILES", home / "codex-profiles")),
            "claude": Path(os.environ.get("CLAUDE_PROFILES", home / "claude-profiles")),
        }
        for provider, root in roots.items():
            if not self.harness(provider).profile_supported() or not root.is_dir():
                continue
            for path in sorted(root.iterdir()):
                if path.is_dir() and not path.name.startswith("."):
                    candidates.append(
                        {"provider": provider, "name": path.name, "path": str(path.resolve())}
                    )
        if (
            self.harness("agy").profile_supported()
            and (home / ".gemini" / "antigravity-cli").is_dir()
        ):
            candidates.append({"provider": "agy", "name": "main", "path": str(home.resolve())})
        if (home / ".codex").is_dir():
            candidates.append(
                {"provider": "codex", "name": "main", "path": str((home / ".codex").resolve())}
            )
        if (home / ".claude").is_dir():
            candidates.append(
                {"provider": "claude", "name": "main", "path": str((home / ".claude").resolve())}
            )
        return [c for c in candidates if os.path.normcase(c["path"]) not in registered]

    def select_smart_candidate(
        self,
        model: str,
        provider: str | None = None,
    ) -> dict[str, Any]:
        from harnesshub.models import rank_candidates

        candidates = rank_candidates(self, model, provider)
        if not candidates:
            raise HubError(
                f"Нет зарегистрированных профилей для модели {model!r}. "
                "Создай или импортируй профиль."
            )
        unlocked = [c for c in candidates if not c["locked"]]
        if not unlocked:
            raise BusyError(
                f"Все профили для модели {model!r} сейчас заняты эксклюзивной операцией"
            )
        return unlocked[0]

    def launch_smart(
        self,
        model: str,
        provider: str | None = None,
        *,
        automode: bool | None = None,
        args: tuple[str, ...] = (),
        cwd: Path | None = None,
    ) -> int:
        from harnesshub.models import model_cli_args

        chosen = self.select_smart_candidate(model, provider)
        m_args = model_cli_args(chosen["provider"], chosen["resolved_model"])
        return self.launch(
            chosen["provider"],
            chosen["profile"],
            automode=automode,
            args=(*m_args, *args),
            cwd=cwd,
        )

    def resume_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        target_profile: str | None = None,
        automode: bool | None = None,
        args: tuple[str, ...] = (),
        cwd: Path | None = None,
    ) -> int:
        from harnesshub.sessions import read_session, resume_cli_args

        session = read_session(self, session_id)
        chosen_profile = (
            str(self.profile(session["provider"], target_profile)["name"])
            if target_profile is not None
            else str(session["profile"])
        )
        effective_cwd = cwd
        if effective_cwd is None:
            saved_cwd = Path(session["cwd"]) if session.get("cwd") else None
            effective_cwd = saved_cwd if (saved_cwd and saved_cwd.is_dir()) else Path.cwd()
        r_args = resume_cli_args(session["provider"], session["session_id"], model)
        return self.launch(
            session["provider"],
            chosen_profile,
            automode=automode,
            args=(*r_args, *args),
            cwd=effective_cwd,
        )

    def plan_handoff(
        self,
        session_id: str,
        *,
        target_model: str | None = None,
        target_provider: str | None = None,
        target_profile: str | None = None,
        note: str = "",
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        from harnesshub.models import normalize_model
        from harnesshub.sessions import build_handoff_prompt, handoff_cli_args

        session, prompt = build_handoff_prompt(self, session_id, note=note, cwd=cwd)
        resolved_model: str | None = None

        if target_model and not target_profile:
            chosen = self.select_smart_candidate(target_model, target_provider)
            resolved_provider = str(chosen["provider"])
            target_profile = str(chosen["profile"])
            resolved_model = str(chosen["resolved_model"])
        else:
            resolved_provider = target_provider or str(session["provider"])
            prof = self.profile(resolved_provider, target_profile)
            target_profile = str(prof["name"])
            if target_model:
                resolved_model = normalize_model(target_model, root=self.store.root)

        effective_cwd = cwd
        if effective_cwd is None:
            saved_cwd = Path(session["cwd"]) if session.get("cwd") else None
            effective_cwd = saved_cwd if (saved_cwd and saved_cwd.is_dir()) else Path.cwd()

        cli_args = handoff_cli_args(resolved_provider, prompt, resolved_model)
        return {
            "source_session": {
                "session_id": session["session_id"],
                "provider": session["provider"],
                "profile": session["profile"],
                "model": session["model"],
            },
            "target_provider": resolved_provider,
            "target_profile": target_profile,
            "target_model": resolved_model,
            "cwd": str(effective_cwd),
            "prompt": prompt,
            "cli_args": list(cli_args),
        }

    def handoff_session(
        self,
        session_id: str,
        *,
        target_model: str | None = None,
        target_provider: str | None = None,
        target_profile: str | None = None,
        note: str = "",
        automode: bool | None = None,
        args: tuple[str, ...] = (),
        cwd: Path | None = None,
    ) -> int:
        plan = self.plan_handoff(
            session_id,
            target_model=target_model,
            target_provider=target_provider,
            target_profile=target_profile,
            note=note,
            cwd=cwd,
        )
        return self.launch(
            plan["target_provider"],
            plan["target_profile"],
            automode=automode,
            args=(*plan["cli_args"], *args),
            cwd=Path(plan["cwd"]),
        )

    def register_harness(
        self,
        provider: str,
        name: str,
        command: str,
        config_env: str | None = None,
        login_args: list[str] | None = None,
    ) -> None:
        validate_name(provider)
        if provider in self.harnesses():
            raise HubError("Harness с таким ID уже существует")
        item = {
            "id": provider,
            "name": name,
            "command": command,
            "config_env": config_env,
            "login_args": login_args or [],
        }
        self.store.update(lambda state: state["custom"].append(item))
