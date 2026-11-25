from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesshub.catalog_loader import (
    AGY_MODEL_LABELS,
    CLAUDE_MODEL_LABELS,
    CODEX_MODEL_LABELS,
    DEFAULT_AGY_MODELS,
    DEFAULT_CLAUDE_MODELS,
    DEFAULT_CODEX_MODELS,
)
from harnesshub.errors import HubError
from harnesshub.providers import ADAPTERS
from harnesshub.providers.agy import (
    agy_acp_probe as _agy_acp_probe,
)
from harnesshub.providers.agy import (
    agy_base_dir,
    agy_models_file,
    build_agy_telemetry,
    probe_agy_telemetry,
    read_agy_profile_models,
    read_agy_quota_state,
)
from harnesshub.providers.agy import (
    agy_cloudcode_post as _agy_cloudcode_post,
)
from harnesshub.providers.base import (
    AccountTelemetry,
    ai_ask_state_dirs,
    classify_agy_tier,
    empty_tokens,
    is_profile_locked,
    model_family,
    profile_lock_state,
    quota_bucket,
    read_label_file,
)
from harnesshub.providers.claude import (
    build_claude_telemetry,
    probe_claude_telemetry,
    read_claude_quota_state,
)
from harnesshub.providers.codex import build_codex_telemetry, probe_codex_telemetry

if TYPE_CHECKING:
    from harnesshub.service import Hub

__all__ = [
    "AGY_MODEL_LABELS",
    "CLAUDE_MODEL_LABELS",
    "CODEX_MODEL_LABELS",
    "DEFAULT_AGY_MODELS",
    "DEFAULT_CLAUDE_MODELS",
    "DEFAULT_CODEX_MODELS",
    "_agy_acp_probe",
    "_agy_cloudcode_post",
    "account_telemetry",
    "agy_base_dir",
    "agy_models_file",
    "ai_ask_state_dirs",
    "classify_agy_tier",
    "is_profile_locked",
    "model_family",
    "probe_agy_telemetry",
    "probe_all_profiles",
    "probe_claude_telemetry",
    "probe_codex_telemetry",
    "probe_profile_telemetry",
    "profile_lock_state",
    "quota_bucket",
    "read_agy_profile_models",
    "read_agy_quota_state",
    "read_claude_quota_state",
]


def _build_generic_telemetry(hub: Hub, profile: dict[str, Any]) -> dict[str, Any]:
    path = Path(profile["path"])
    locked, active = profile_lock_state(hub, profile)
    return AccountTelemetry(
        provider=profile["provider"],
        profile=profile["name"],
        path=str(path),
        locked=locked,
        active=active,
        logged_in=path.is_dir(),
        account_label=read_label_file(path),
        plan=None,
        plan_detail=profile["provider"],
        plan_expires_at=None,
        plan_expires_iso=None,
        plan_expired=False,
        tier=profile["provider"],
        quota_source="unavailable",
        checked_at=None,
        in_cooldown=False,
        spent_buckets={},
        remaining_percent=None,
        resets_at=None,
        windows=[],
        model_quotas={},
        reset_credits=0,
        models=[],
        locked_models=[],
        models_source="none",
        models_cached=False,
        tokens=empty_tokens(),
    ).to_dict()


def account_telemetry(
    hub: Hub, profile: dict[str, Any], *, live: bool = False, now: float | None = None
) -> dict[str, Any]:
    now = time.time() if now is None else now
    provider = profile["provider"]
    if live:
        try:
            return probe_profile_telemetry(hub, provider, profile["name"])
        except HubError:
            pass
    adapter = ADAPTERS.get(provider)
    if adapter is not None:
        return adapter.build_telemetry(hub, profile, now)
    if provider == "codex":
        return build_codex_telemetry(hub, profile, now)
    if provider == "agy":
        return build_agy_telemetry(hub, profile, now)
    if provider == "claude":
        return build_claude_telemetry(hub, profile, now)
    return _build_generic_telemetry(hub, profile)


def probe_profile_telemetry(hub: Hub, provider: str, profile_name: str) -> dict[str, Any]:
    adapter = ADAPTERS.get(provider)
    if adapter is not None:
        return adapter.probe_telemetry(hub, profile_name)
    return account_telemetry(hub, hub.profile(provider, profile_name), live=False)


def probe_all_profiles(hub: Hub, provider: str | None = None) -> list[dict[str, Any]]:
    profiles = hub.profiles(provider)
    if not profiles:
        return []

    def _run_one(prof: dict[str, Any]) -> dict[str, Any]:
        prov = prof["provider"]
        name = prof["name"]
        try:
            return probe_profile_telemetry(hub, prov, name)
        except HubError as exc:
            tel = account_telemetry(hub, prof, live=False)
            tel["probe_error"] = str(exc)
            return tel

    if len(profiles) == 1:
        return [_run_one(profiles[0])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(profiles))) as pool:
        futures = [pool.submit(_run_one, prof) for prof in profiles]
        return [f.result() for f in futures]
