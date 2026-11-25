from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesshub.errors import HubError
from harnesshub.providers.base import quota_bucket, safe_json_read
from harnesshub.telemetry import (
    account_telemetry,
    agy_base_dir,
    probe_agy_telemetry,
    probe_claude_telemetry,
    probe_codex_telemetry,
    read_agy_profile_models,
    read_agy_quota_state,
)

if TYPE_CHECKING:
    from harnesshub.service import Hub


async def read_usage(
    hub: Hub, provider: str, name: str | None = None, *, live: bool = True
) -> dict[str, Any]:
    if provider == "agy":
        profile = hub.profile("agy", name)
        path = Path(profile["path"])
        base = agy_base_dir(path)
        tok_candidates = (
            path / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
            base / "antigravity-oauth-token",
        )
        has_json_token = False
        for tf in tok_candidates:
            if tf.is_file() and tf.stat().st_size > 0:
                try:
                    raw = tf.read_text(encoding="utf-8", errors="replace").strip()
                    has_json_token = raw.startswith("{")
                except OSError:
                    pass
                break

        if live and has_json_token:
            try:
                tel = await asyncio.to_thread(probe_agy_telemetry, hub, profile["name"])
                return {
                    "provider": provider,
                    "profile": profile["name"],
                    "source": tel.get("source", "live"),
                    "checked_at": tel.get("checked_at") or time.time(),
                    "plan": tel.get("plan") or tel.get("tier"),
                    "plan_detail": tel.get("plan_detail"),
                    "models": [m["id"] for m in tel.get("models") or []],
                    "windows": tel.get("windows") or [],
                    "model_quotas": tel.get("model_quotas") or {},
                    "tokens": tel.get("tokens") or {},
                }
            except HubError:
                pass

        models, tier = read_agy_profile_models(path)
        now = time.time()
        quota = read_agy_quota_state(hub, profile["name"], now)
        cached = safe_json_read(hub.store.root / "quotas" / f"agy-telemetry.{profile['name']}.json")
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("windows"), list)
            and cached["windows"]
        ):
            tel = account_telemetry(hub, profile, live=False, now=now)
            return {
                "provider": provider,
                "profile": profile["name"],
                "source": "cooldown" if quota["in_cooldown"] else "cache",
                "checked_at": cached.get("checked_at") or now,
                "plan": tel.get("plan") or tier,
                "plan_detail": tel.get("plan_detail"),
                "models": [m["id"] for m in tel.get("models") or []] or models,
                "windows": tel.get("windows") or [],
                "model_quotas": tel.get("model_quotas") or {},
                "tokens": tel.get("tokens") or {},
            }

        windows: list[dict[str, Any]] = []
        seen_windows: set[str] = set()
        for bucket, resets_at in sorted(quota["spent_buckets"].items()):
            seen_windows.add(bucket)
            windows.append(
                {
                    "window": bucket,
                    "label": bucket,
                    "remaining_percent": 0.0,
                    "minutes": None,
                    "resets_at": int(resets_at) if resets_at else None,
                }
            )
        if quota["in_cooldown"] and "cooldown" not in seen_windows:
            cd_ts = quota["cooldown_until"]
            windows.append(
                {
                    "window": "cooldown",
                    "label": "429 кулдаун",
                    "remaining_percent": 0.0,
                    "minutes": None,
                    "resets_at": int(cd_ts) if cd_ts else None,
                }
            )
        # Apply profile-level cooldown if models exist and no specific bucket recorded
        if quota["in_cooldown"] and not quota["spent_buckets"]:
            for m in models:
                b = quota_bucket(m)
                if b not in seen_windows:
                    seen_windows.add(b)
        return {
            "provider": provider,
            "profile": profile["name"],
            "source": "cooldown" if windows else "local",
            "checked_at": now,
            "plan": tier,
            "models": models,
            "windows": windows,
        }

    if provider == "claude":
        profile = hub.profile("claude", name)
        path = Path(profile["path"])
        if live and (path / ".credentials.json").is_file() and hub.harness("claude").executable():
            try:
                tel = await asyncio.to_thread(probe_claude_telemetry, hub, profile["name"])
                return {
                    "provider": provider,
                    "profile": profile["name"],
                    "source": "live",
                    "checked_at": tel.get("checked_at") or time.time(),
                    "plan": tel.get("plan") or "claude",
                    "plan_detail": tel.get("plan_detail"),
                    "plan_expires_iso": tel.get("plan_expires_iso"),
                    "windows": tel.get("windows") or [],
                    "tokens": tel.get("tokens") or {},
                }
            except HubError:
                pass

        now = time.time()
        tel = account_telemetry(hub, profile, live=False, now=now)
        return {
            "provider": provider,
            "profile": profile["name"],
            "source": "cooldown" if tel.get("in_cooldown") else "local",
            "checked_at": now,
            "plan": tel.get("plan") or "claude",
            "plan_detail": tel.get("plan_detail"),
            "plan_expires_iso": tel.get("plan_expires_iso"),
            "windows": tel.get("windows") or [],
            "tokens": tel.get("tokens") or {},
        }

    if provider != "codex":
        raise HubError(
            "Опрос квот пока подключён для Codex, AGY и Claude. Для остальных используй "
            "родной /usage или /status; нулевой остаток не подставляется."
        )

    profile = hub.profile(provider, name)
    if live:
        tel = await asyncio.to_thread(probe_codex_telemetry, hub, profile["name"])
    else:
        tel = account_telemetry(hub, profile, live=False)
    return {
        "provider": provider,
        "profile": profile["name"],
        "source": tel.get("source", tel.get("quota_source", "cache")),
        "checked_at": tel.get("checked_at") or time.time(),
        "plan": tel.get("plan"),
        "plan_detail": tel.get("plan_detail"),
        "plan_expires_iso": tel.get("plan_expires_iso"),
        "windows": tel.get("windows") or [],
        "reset_credits": tel.get("reset_credits", 0),
        "tokens": tel.get("tokens") or {},
    }
