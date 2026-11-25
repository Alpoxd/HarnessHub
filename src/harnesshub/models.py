from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesshub.catalog_loader import (
    AGY_MODEL_LABELS,
    CLAUDE_MODEL_LABELS,
    CLAUDE_SUBSTITUTE_CHAINS,
    CODEX_MODEL_LABELS,
    DEFAULT_AGY_MODELS,
    DEFAULT_CLAUDE_MODELS,
    DEFAULT_CODEX_MODELS,
    MODEL_ALIASES,
    catalog_for_root,
)
from harnesshub.providers.base import atomic_write_text
from harnesshub.telemetry import (
    account_telemetry,
    agy_models_file,
    classify_agy_tier,
    is_profile_locked,
    model_family,
    probe_agy_telemetry,
    probe_all_profiles,
    probe_claude_telemetry,
    probe_codex_telemetry,
    probe_profile_telemetry,
    profile_lock_state,
    quota_bucket,
    read_agy_profile_models,
    read_agy_quota_state,
    read_claude_quota_state,
)

if TYPE_CHECKING:
    from harnesshub.service import Hub

__all__ = [
    "AGY_MODEL_LABELS",
    "CLAUDE_MODEL_LABELS",
    "CODEX_MODEL_LABELS",
    "DEFAULT_AGY_MODELS",
    "DEFAULT_CLAUDE_MODELS",
    "DEFAULT_CODEX_MODELS",
    "MODEL_ALIASES",
    "agy_models_file",
    "catalog_models",
    "classify_agy_tier",
    "group_catalog_models",
    "is_profile_locked",
    "is_tier1_agy_model",
    "mark_quota_cooldown",
    "model_base_and_effort",
    "model_cli_args",
    "model_family",
    "normalize_model",
    "probe_agy_models",
    "probe_agy_telemetry",
    "probe_all_profiles",
    "probe_claude_telemetry",
    "probe_codex_telemetry",
    "probe_profile_telemetry",
    "profile_capabilities",
    "profile_lock_state",
    "quota_bucket",
    "rank_candidates",
    "read_agy_profile_models",
    "read_agy_quota_state",
    "read_claude_quota_state",
    "substitute_chain",
]


def normalize_model(model: str, *, root: Path | None = None) -> str:
    cleaned = model.strip().lower()
    aliases = catalog_for_root(root).get("aliases", MODEL_ALIASES) if root else MODEL_ALIASES
    return str(aliases.get(cleaned, cleaned))


def is_tier1_agy_model(model: str, *, root: Path | None = None) -> bool:
    slug = normalize_model(model, root=root)
    return slug.startswith(
        (
            "gemini-4-",
            "gemini-pro-orchestrator",
            "claude-opus-5-5",
            "claude-sonnet-5-5",
        )
    )


def substitute_chain(model: str, provider: str, *, root: Path | None = None) -> tuple[str, ...]:
    slug = normalize_model(model, root=root)
    level = "low" if slug.endswith("-low") else "medium" if slug.endswith("-medium") else "high"
    if provider == "codex":
        return (slug,)
    if provider == "claude":
        chains = (
            catalog_for_root(root).get("substitutes", {}).get("claude", CLAUDE_SUBSTITUTE_CHAINS)
            if root
            else CLAUDE_SUBSTITUTE_CHAINS
        )
        raw = chains.get(slug)
        return tuple(str(x) for x in raw) if raw else (slug,)
    if provider == "agy":
        if slug == "claude-opus-5-5":
            slug = "claude-opus-5-5-high"
        elif slug == "claude-sonnet-5-5":
            slug = "claude-sonnet-5-5-high"
        elif slug == "claude-opus-4-6":
            slug = "claude-opus-4-6-thinking"
        if slug.startswith(("gemini-4-neon", "gemini-4-carbon", "gemini-4-argon")):
            return (slug, f"gemini-4-hydrogen-{level}", f"gemini-3.8-flash-{level}")
        if slug.startswith("gemini-pro-orchestrator"):
            return (
                slug,
                "gemini-pro-orchestrator-argon",
                "gemini-pro-orchestrator",
                "gemini-4-hydrogen-high",
                "gemini-3.8-flash-high",
            )
        if slug.startswith("gemini-4-hydrogen"):
            return (slug, f"gemini-3.8-flash-{level}")
        if slug.startswith("claude-opus-5-5"):
            return (slug, "claude-opus-4-6-thinking")
        if slug.startswith("claude-sonnet-5-5"):
            return (slug, "claude-sonnet-4-6")
    return (slug,)


def probe_agy_models(hub: Hub, profile_name: str, timeout: float = 20.0) -> dict[str, Any]:
    tel = probe_agy_telemetry(hub, profile_name, timeout=timeout)
    return {
        "provider": "agy",
        "profile": profile_name,
        "tier": tel["tier"],
        "models": [m["id"] for m in tel["models"]],
        "path": str(Path(tel["path"]) / ".models"),
    }


def mark_quota_cooldown(
    hub: Hub,
    provider: str,
    profile_name: str,
    model: str | None = None,
    duration_seconds: int = 3600,
) -> dict[str, Any]:
    quota_dir = hub.store.root / "quotas"
    resets_at = int(time.time() + max(1, duration_seconds))
    if provider == "agy":
        norm_model = normalize_model(model or "argon", root=hub.store.root)
        bucket = quota_bucket(norm_model)
        target = quota_dir / f"agy-quota.{profile_name}.{bucket}"
    elif provider == "claude":
        target = quota_dir / f"claude-cooldown.{profile_name}"
    else:
        target = quota_dir / f"{provider}-cooldown.{profile_name}"
    atomic_write_text(target, f"{resets_at}\n", mode=0o600)
    return {
        "provider": provider,
        "profile": profile_name,
        "model": model,
        "resets_at": resets_at,
    }


def profile_capabilities(
    hub: Hub, profile: dict[str, Any], now: float | None = None
) -> dict[str, Any]:
    tel = account_telemetry(hub, profile, live=False, now=now)
    model_ids = [m["id"] for m in tel["models"]]
    model_meta = {m["id"]: m for m in tel["models"]}
    quota_known = tel["quota_source"] in ("live", "cache", "cooldown")
    rem_display = tel["remaining_percent"]
    rem_effective: float | None = 0.0 if tel["in_cooldown"] else rem_display
    return {
        "provider": tel["provider"],
        "profile": tel["profile"],
        "path": tel["path"],
        "locked": tel["locked"],
        "active": tel.get("active", False),
        "logged_in": tel["logged_in"],
        "account_label": tel["account_label"],
        "plan": tel["plan"],
        "plan_detail": tel["plan_detail"],
        "plan_expires_iso": tel["plan_expires_iso"],
        "plan_expired": tel["plan_expired"],
        "tier": tel["tier"],
        "models": model_ids,
        "model_meta": model_meta,
        "locked_models": tel["locked_models"],
        "models_cached": tel["models_cached"],
        "models_source": tel["models_source"],
        "in_cooldown": tel["in_cooldown"],
        "spent_buckets": tel["spent_buckets"],
        "windows": tel["windows"],
        "model_quotas": tel["model_quotas"],
        "quota_known": quota_known,
        "quota_source": tel["quota_source"],
        "display_remaining_percent": rem_display,
        "remaining_percent": rem_effective,
        "resets_at": tel["resets_at"],
        "tokens": tel["tokens"],
    }


def _model_quota_for(
    cap: dict[str, Any], model_id: str
) -> tuple[float | None, float | None, bool, bool]:
    bucket = quota_bucket(model_id)
    spent_buckets = cap["spent_buckets"]
    bucket_spent = bucket in spent_buckets or cap["in_cooldown"]
    if bucket_spent:
        return 0.0, spent_buckets.get(bucket) or cap["resets_at"], True, True

    mq = cap["model_quotas"].get(model_id)
    if isinstance(mq, dict) and mq.get("remaining_percent") is not None:
        rem = float(mq["remaining_percent"])
        return rem, mq.get("resets_at") or cap["resets_at"], True, rem <= 0

    if cap["provider"] == "agy" and model_id.startswith("gemini-"):
        gem_win = next((w for w in cap["windows"] if w.get("window") == "gemini"), None)
        if gem_win and gem_win.get("remaining_percent") is not None:
            rem = float(gem_win["remaining_percent"])
            return rem, gem_win.get("resets_at") or cap["resets_at"], True, rem <= 0

    if cap["display_remaining_percent"] is not None:
        rem = float(cap["display_remaining_percent"])
        return rem, cap["resets_at"], cap["quota_known"], rem <= 0

    return None, cap["resets_at"], False, False


_EFFORT_SUFFIXES: tuple[str, ...] = ("-xhigh", "-high", "-medium", "-low", "-max")
_EFFORT_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
_EFFORT_PREF: tuple[str, ...] = ("high", "xhigh", "medium", "low", "max")


def model_base_and_effort(model_id: str) -> tuple[str, str | None]:
    lowered = model_id.strip().lower()
    for suffix in _EFFORT_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 2:
            return model_id[: -len(suffix)], suffix.lstrip("-")
    return model_id, None


def _clean_base_display_name(display_name: str, base_id: str) -> str:
    for tag in (" (XHigh)", " (High)", " (Medium)", " (Low)", " (Max)"):
        if display_name.endswith(tag):
            return display_name[: -len(tag)]
    return display_name or base_id


def group_catalog_models(
    entries: list[dict[str, Any]], preferred_effort: str = "high"
) -> list[dict[str, Any]]:
    """Collapse effort-variant rows (e.g. -low/-medium/-high/-xhigh) into one row per base model."""
    buckets: dict[tuple[str, str], list[tuple[str | None, dict[str, Any]]]] = {}
    order: list[tuple[str, str]] = []
    for item in entries:
        mid = str(item["model"])
        base_id, eff = model_base_and_effort(mid)
        provs_key = ",".join(item.get("providers") or [])
        gkey = (provs_key, base_id)
        if gkey not in buckets:
            buckets[gkey] = []
            order.append(gkey)
        buckets[gkey].append((eff, item))

    grouped: list[dict[str, Any]] = []
    for gkey in order:
        variants = buckets[gkey]
        eff_map: dict[str, dict[str, Any]] = {}
        plain: dict[str, Any] | None = None
        for eff, item in variants:
            if eff is None:
                plain = item
            else:
                eff_map[eff] = item

        # Keep single non-variant or single-effort models cleanly while exposing available efforts
        if len(variants) == 1 and plain is not None:
            single = dict(plain)
            single["base_model"] = single["model"]
            single["efforts"] = []
            single["selected_effort"] = None
            single["variants"] = {None: single["model"]}
            grouped.append(single)
            continue

        chosen_eff: str | None = None
        chosen_item: dict[str, Any]
        if preferred_effort in eff_map:
            chosen_eff = preferred_effort
            chosen_item = eff_map[preferred_effort]
        else:
            picked = next((e for e in _EFFORT_PREF if e in eff_map), None)
            if picked is not None:
                chosen_eff = picked
                chosen_item = eff_map[picked]
            else:
                chosen_item = plain or variants[0][1]

        available_efforts = sorted(eff_map.keys(), key=lambda e: _EFFORT_ORDER.get(e, 99))
        total_tokens = sum(int(item.get("total_tokens") or 0) for _, item in variants)
        base_id = gkey[1]
        row = dict(chosen_item)
        row["base_model"] = base_id
        row["display_name"] = _clean_base_display_name(
            str(chosen_item.get("display_name") or ""), base_id
        )
        row["efforts"] = available_efforts
        row["selected_effort"] = chosen_eff
        row["variants"] = {eff: str(item["model"]) for eff, item in variants}
        row["total_tokens"] = total_tokens
        grouped.append(row)
    return grouped


def catalog_models(
    hub: Hub,
    provider: str | None = None,
    *,
    family: str | None = None,
    query: str = "",
) -> list[dict[str, Any]]:
    profiles = hub.profiles(provider)
    now = time.time()
    capabilities = [profile_capabilities(hub, p, now) for p in profiles]
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    best_scores: dict[tuple[str, str], tuple[int, float]] = {}
    seen_token_sources: set[tuple[str, str, str]] = set()
    q_norm = query.strip().lower()
    fam_norm = family.strip().lower() if family else None

    for cap in capabilities:
        prov = cap["provider"]
        tok_dict = cap.get("tokens") or {}
        tok_by_model = tok_dict.get("by_model") or {}
        tok_source = str(tok_dict.get("stats_source_key") or cap["path"])
        for m in cap["models"]:
            meta = cap["model_meta"].get(m) or {}
            fam = meta.get("family") or model_family(m)
            if fam_norm and fam != fam_norm:
                continue
            display_name = meta.get("name") or m
            if q_norm and q_norm not in m.lower() and q_norm not in display_name.lower():
                continue

            rem, resets_at, q_known, depleted = _model_quota_for(cap, m)
            key = (prov, m) if provider else ("", m)
            entry = by_key.get(key)
            if entry is None:
                entry = {
                    "model": m,
                    "display_name": display_name,
                    "family": fam,
                    "providers": [],
                    "context_window": meta.get("context_window"),
                    "total_profiles": 0,
                    "ready_profiles": 0,
                    "best_profile": None,
                    "best_remaining_percent": -1.0,
                    "quota_known": False,
                    "best_resets_at": None,
                    "total_tokens": 0,
                }
                by_key[key] = entry
                best_scores[key] = (-2, -1.0)

            if prov not in entry["providers"]:
                entry["providers"].append(prov)
            if entry["context_window"] is None and meta.get("context_window") is not None:
                entry["context_window"] = meta.get("context_window")
            entry["total_profiles"] += 1
            tok_dedup_key = (prov, tok_source, m)
            if tok_dedup_key not in seen_token_sources:
                seen_token_sources.add(tok_dedup_key)
                entry["total_tokens"] += int(tok_by_model.get(m) or 0)

            ready = (
                not cap["locked"] and cap["logged_in"] and not depleted and (rem is None or rem > 0)
            )
            if ready:
                entry["ready_profiles"] += 1
            if not cap["locked"] and cap["logged_in"]:
                if q_known and rem is not None:
                    score = (1, rem)
                elif not depleted:
                    score = (0, -1.0)
                else:
                    score = (-1, 0.0)
                if score > best_scores[key]:
                    best_scores[key] = score
                    entry["best_remaining_percent"] = (
                        round(rem, 1) if (q_known and rem is not None) else -1.0
                    )
                    entry["best_profile"] = f"{prov}/{cap['profile']}"
                    entry["quota_known"] = q_known
                    entry["best_resets_at"] = resets_at

    return sorted(
        by_key.values(),
        key=lambda item: (
            item["providers"][0] if item["providers"] else "",
            item["family"],
            item["model"],
        ),
    )


def rank_candidates(
    hub: Hub,
    model: str,
    provider: str | None = None,
    *,
    allow_substitutes: bool = True,
) -> list[dict[str, Any]]:
    requested = normalize_model(model, root=hub.store.root)
    req_family = model_family(requested)
    profiles = hub.profiles(provider)
    now = time.time()
    candidates: list[dict[str, Any]] = []

    for prof in profiles:
        prov = prof["provider"]
        if provider is None:
            if requested.startswith("gpt-oss"):
                if prov != "agy":
                    continue
            elif req_family == "gpt" and prov != "codex":
                continue
            elif req_family == "gemini" and prov not in ("agy", "gemini"):
                continue
            elif req_family == "claude" and prov not in ("claude", "agy"):
                continue

        cap = profile_capabilities(hub, prof, now)
        chain = (
            substitute_chain(requested, prov, root=hub.store.root)
            if allow_substitutes
            else (requested,)
        )
        supported = set(cap["models"])

        matched_model: str | None = None
        substitute_rank = 99
        for idx, candidate_model in enumerate(chain):
            if candidate_model in supported:
                matched_model = candidate_model
                substitute_rank = idx
                break
        if matched_model is None:
            continue

        rem, resets_at, q_known, depleted = _model_quota_for(cap, matched_model)
        quota_ready = not depleted and (rem is None or rem > 0)

        spare_penalty = (
            1
            if (
                prov == "agy"
                and not is_tier1_agy_model(matched_model, root=hub.store.root)
                and "gemini-4-hydrogen-high" in supported
            )
            else 0
        )

        candidates.append(
            {
                "provider": prov,
                "profile": prof["name"],
                "path": prof["path"],
                "default": bool(prof.get("default")),
                "requested_model": requested,
                "resolved_model": matched_model,
                "exact_match": substitute_rank == 0,
                "substitute_rank": substitute_rank,
                "tier": cap["tier"],
                "plan": cap["plan"],
                "plan_detail": cap["plan_detail"],
                "locked": cap["locked"],
                "active": cap["active"],
                "logged_in": cap["logged_in"],
                "quota_ready": quota_ready,
                "quota_known": q_known,
                "remaining_percent": round(rem, 1) if (q_known and rem is not None) else None,
                "resets_at": resets_at,
                "spare_penalty": spare_penalty,
                "tokens_total": (cap.get("tokens") or {}).get("total_tokens"),
            }
        )

    candidates.sort(
        key=lambda c: (
            0 if not c["locked"] else 1,
            0 if c["logged_in"] else 1,
            0 if c["quota_ready"] else 1,
            c["substitute_rank"],
            0 if c["quota_known"] else 1,
            -int((c["remaining_percent"] or 0.0) // 10) if c["quota_known"] else 0,
            0 if not c["active"] else 1,
            -(c["remaining_percent"] if c["remaining_percent"] is not None else 0.0),
            c["spare_penalty"],
            0 if c["default"] else 1,
            c["provider"],
            c["profile"],
        )
    )
    return candidates


def model_cli_args(provider: str, model: str) -> tuple[str, ...]:
    if not model:
        return ()
    if provider == "codex":
        return ("-m", model)
    if provider in ("agy", "claude", "gemini"):
        return ("--model", model)
    return ("--model", model)
