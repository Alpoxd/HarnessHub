from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harnesshub.errors import HubError
from harnesshub.locking import FileLock
from harnesshub.network import validate_proxy

NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}\Z")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def validate_name(value: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise HubError("Имя: 1–32 символа A–Z, a–z, 0–9, _ или -; первый символ — буква/цифра")
    return value


def empty_state() -> dict:
    return {
        "version": 2,
        "profiles": [],
        "defaults": {},
        "custom": [],
        "settings": {},
        "network": {"global": {"mode": "inherit"}},
        "installations": {},
    }


def validate_state(state: dict) -> None:
    try:
        if type(state["version"]) is not int or state["version"] not in {1, 2}:
            raise HubError("Неизвестная версия state.json; обнови HarnessHub")
        if not isinstance(state["profiles"], list) or not isinstance(state["custom"], list):
            raise ValueError
        if not isinstance(state["defaults"], dict) or not isinstance(state["settings"], dict):
            raise ValueError
        identities, locations = set(), set()
        for profile in state["profiles"]:
            provider, name = validate_name(profile["provider"]), validate_name(profile["name"])
            path = Path(profile["path"])
            if not path.is_absolute() or type(profile["external"]) is not bool:
                raise ValueError
            if type(profile.get("autoheal", False)) is not bool:
                raise ValueError
            if "automode" in profile and type(profile["automode"]) is not bool:
                raise ValueError
            if "proxy" in profile:
                validate_proxy(profile["proxy"])
            identity, location = (provider, name), os.path.normcase(str(path.resolve()))
            if identity in identities or location in locations:
                raise ValueError
            identities.add(identity)
            locations.add(location)
        if any((p, n) not in identities for p, n in state["defaults"].items()):
            raise ValueError
        from harnesshub.registry import BUILTINS

        provider_ids = {p.id for p in BUILTINS}
        for item in state["custom"]:
            validate_name(item["id"])
            if item["id"] in provider_ids or not isinstance(item["name"], str) or not item["name"]:
                raise ValueError
            provider_ids.add(item["id"])
            if not isinstance(item["command"], str) or not item["command"].strip():
                raise ValueError
            env = item.get("config_env")
            if env is not None and (not isinstance(env, str) or not ENV_NAME.fullmatch(env)):
                raise ValueError
            args = item.get("login_args", [])
            if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
                raise ValueError
        if any(p["provider"] not in provider_ids for p in state["profiles"]):
            raise ValueError
        str_settings = {"agy_helper", "agy_binary", "agy_shared_home"}
        bool_settings = {"agy_automode", "automode", "agy_shared_history", "agy_locale_override"}
        for k, v in state["settings"].items():
            if k in str_settings:
                if not isinstance(v, str):
                    raise ValueError
                if k == "agy_shared_home" and (not v.strip() or not Path(v).is_absolute()):
                    raise ValueError
            elif k in bool_settings:
                if type(v) is not bool:
                    raise ValueError
            else:
                raise ValueError
        if state["version"] == 2:
            if not isinstance(state["network"], dict) or set(state["network"]) != {"global"}:
                raise ValueError
            validate_proxy(state["network"]["global"])
            if not isinstance(state["installations"], dict):
                raise ValueError
            for provider, install in state["installations"].items():
                if provider not in provider_ids or not isinstance(install, dict):
                    raise ValueError
                directory = Path(install["directory"])
                executable = Path(install["executable"])
                if (
                    not directory.is_absolute()
                    or not executable.is_absolute()
                    or not executable.resolve().is_relative_to(directory.resolve())
                ):
                    raise ValueError
                launcher = install["launcher"]
                if (
                    not isinstance(launcher, list)
                    or not launcher
                    or any(not isinstance(arg, str) or not arg for arg in launcher)
                    or launcher[-1] != str(executable)
                ):
                    raise ValueError
                if any(
                    not isinstance(install[key], str)
                    for key in ("source", "method", "installed_at")
                ):
                    raise ValueError
                runtime = install.get("env", {})
                allowed = {"JUNIE_DATA"} if provider == "junie" else set()
                if not isinstance(runtime, dict) or set(runtime) - allowed:
                    raise ValueError
                if any(
                    not isinstance(value, str)
                    or not Path(value).is_absolute()
                    or not Path(value).resolve().is_relative_to(directory.resolve())
                    for value in runtime.values()
                ):
                    raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HubError("Некорректный state.json; файл сохранён без изменений") from exc


class StateStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.path = self.root / "state.json"

    def read(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty_state()
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise HubError("Не удалось прочитать state.json; файл сохранён без изменений") from exc
        validate_state(state)
        if state["version"] == 1:
            state = copy.deepcopy(state)
            state["version"] = 2
            state["network"] = {"global": {"mode": "inherit"}}
            state["installations"] = {}
        return state

    def update(self, change: Callable[[dict], Any]) -> Any:
        with FileLock(self.root / "state.lock"):
            state = copy.deepcopy(self.read())
            result = change(state)
            validate_state(state)
            fd, name = tempfile.mkstemp(prefix=".state-", dir=self.root)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(state, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return result
