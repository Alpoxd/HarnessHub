from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

from harnesshub.errors import HubError

PROXY_KEYS = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
LOOPBACK = "localhost,127.0.0.1,::1"


def validate_proxy(policy: dict) -> None:
    if not isinstance(policy, dict) or set(policy) - {"mode", "url", "no_proxy"}:
        raise HubError("Некорректная настройка прокси")
    if policy.get("mode") not in {"inherit", "direct", "url"}:
        raise HubError("Режим прокси: inherit, direct или url")
    bypass = policy.get("no_proxy")
    if bypass is not None and (not isinstance(bypass, str) or any(ord(c) < 32 for c in bypass)):
        raise HubError("Некорректный список исключений прокси")
    if policy["mode"] != "url":
        if "url" in policy:
            raise HubError("URL нужен только для режима url")
        return
    value = policy.get("url")
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise HubError("Укажи URL прокси в формате http://host:port или https://host:port")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        if any(ord(c) < 32 or ord(c) == 127 for c in unquote(value)):
            raise ValueError
    except ValueError as exc:
        raise HubError("Некорректный URL HTTP(S)-прокси; проверь адрес и порт") from exc


def redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username is None and parsed.password is None:
        return value
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme, f"***:***@{host}", parsed.path, "", ""))


def public_proxy(policy: dict) -> dict:
    return {**policy, **({"url": redact_url(policy["url"])} if "url" in policy else {})}


def redact_output(value: str, env: dict[str, str] | None = None) -> str:
    for key, url in (env or {}).items():
        if key.lower() not in PROXY_KEYS or key.lower() == "no_proxy":
            continue
        try:
            parsed = urlsplit(url)
            if parsed.username is not None or parsed.password is not None:
                value = value.replace(url, redact_url(url))
            for credential in (parsed.username, parsed.password):
                if credential:
                    value = value.replace(credential, "***").replace(unquote(credential), "***")
        except ValueError:
            continue
    return re.sub(r"(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1***:***@", value)


def resolve_proxy(global_policy: dict, profile_policy: dict | None = None) -> tuple[dict, str]:
    profile_policy = profile_policy or {"mode": "inherit"}
    if profile_policy["mode"] != "inherit":
        return dict(profile_policy), "profile"
    effective = dict(global_policy)
    if "no_proxy" in profile_policy:
        effective["no_proxy"] = profile_policy["no_proxy"]
    return effective, "environment" if effective["mode"] == "inherit" else "global"


def proxy_environment(env: dict[str, str], policy: dict) -> dict[str, str]:
    result = dict(env)
    if policy["mode"] == "inherit":
        if "no_proxy" in policy:
            for key in list(result):
                if key.lower() == "no_proxy":
                    result.pop(key)
            result["NO_PROXY"] = result["no_proxy"] = policy["no_proxy"]
        return result
    for key in list(result):
        if key.lower() in PROXY_KEYS:
            result.pop(key)
    proxy = policy.get("url", "")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        result[name] = result[name.lower()] = proxy
    bypass = "*" if policy["mode"] == "direct" else policy.get("no_proxy", LOOPBACK)
    result["NO_PROXY"] = result["no_proxy"] = bypass
    return result
