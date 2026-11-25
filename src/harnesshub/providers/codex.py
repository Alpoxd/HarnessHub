from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesshub import __version__
from harnesshub.catalog_loader import CODEX_MODEL_LABELS, DEFAULT_CODEX_MODELS
from harnesshub.errors import HubError
from harnesshub.providers.base import (
    MAX_FULL_TRANSCRIPT_BYTES,
    SYNTHETIC_CODEX_PREFIXES,
    AccountTelemetry,
    BoundedCache,
    SessionSummary,
    ai_ask_state_dirs,
    atomic_write_json,
    decode_jwt_claims,
    empty_tokens,
    epoch_to_date,
    epoch_to_iso,
    extract_content_text,
    fast_head_lines_large,
    iso_to_epoch,
    iso_to_epoch_or_zero,
    model_family,
    profile_lock_state,
    project_name,
    read_head_and_count,
    read_label_file,
    safe_json_read,
    shorten,
)
from harnesshub.rpc import JsonRpcStdioClient

if TYPE_CHECKING:
    from harnesshub.service import Hub

_CODEX_TOKEN_STATS_CACHE: BoundedCache[str, tuple[int, int, dict[str, Any]]] = BoundedCache(256)


def _codex_window_label(window_key: str, minutes: int | None) -> str:
    if isinstance(minutes, int) and minutes > 0:
        if minutes <= 360:
            return "5ч"
        if 9000 <= minutes <= 12000:
            return "7д"
        if minutes >= 40000:
            return "30д"
        if minutes >= 1440:
            return f"{round(minutes / 1440)}д"
        if minutes >= 60:
            return f"{round(minutes / 60)}ч"
        return f"{minutes}м"
    if window_key == "primary":
        return "5ч"
    if window_key == "secondary":
        return "7д"
    return window_key


def parse_codex_auth_metadata(profile_path: Path) -> dict[str, Any]:
    auth_file = profile_path / "auth.json"
    if not auth_file.is_file():
        return {"logged_in": False, "auth_mtime": None}
    try:
        st = auth_file.stat()
    except OSError:
        return {"logged_in": False, "auth_mtime": None}
    if st.st_size <= 0:
        return {"logged_in": False, "auth_mtime": None}

    raw = safe_json_read(auth_file)
    email: str | None = None
    jwt_plan: str | None = None
    active_until: float | None = None
    active_start: float | None = None
    if isinstance(raw, dict):
        tokens = raw.get("tokens")
        if isinstance(tokens, dict):
            claims = decode_jwt_claims(tokens.get("id_token") or "")
            if isinstance(claims.get("email"), str) and claims["email"]:
                email = claims["email"]
            oai = claims.get("https://api.openai.com/auth")
            if isinstance(oai, dict):
                if isinstance(oai.get("chatgpt_plan_type"), str):
                    jwt_plan = oai["chatgpt_plan_type"]
                active_until = iso_to_epoch(oai.get("chatgpt_subscription_active_until"))
                active_start = iso_to_epoch(oai.get("chatgpt_subscription_active_start"))
    return {
        "logged_in": True,
        "auth_mtime": st.st_mtime,
        "email": email,
        "jwt_plan": jwt_plan,
        "active_until": active_until,
        "active_start": active_start,
    }


def read_codex_sqlite_tokens(profile_path: Path) -> dict[str, Any]:
    db_path = profile_path / "state_5.sqlite"
    if not db_path.is_file():
        return {}
    try:
        st = db_path.stat()
    except OSError:
        return {}
    key = str(db_path)
    cached = _CODEX_TOKEN_STATS_CACHE.get(key)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return dict(cached[2])

    stats: dict[str, Any] = {}
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        cols = {row[1] for row in con.execute("PRAGMA table_info(threads)").fetchall()}
        if not cols:
            return {}
        has_tokens = "tokens_used" in cols
        has_source = "thread_source" in cols
        has_model = "model" in cols
        sum_expr = "COALESCE(SUM(tokens_used), 0)" if has_tokens else "0"
        cnt_expr = (
            "SUM(CASE WHEN COALESCE(thread_source, '') != 'subagent' THEN 1 ELSE 0 END)"
            if has_source
            else "COUNT(*)"
        )
        row = con.execute(f"SELECT {sum_expr}, {cnt_expr} FROM threads").fetchone()
        total = int(row[0] or 0) if row else 0
        sessions_count = int(row[1] or 0) if row else 0
        by_model: dict[str, int] = {}
        if has_tokens and has_model:
            for m, t in con.execute(
                "SELECT model, SUM(tokens_used) FROM threads "
                "WHERE model IS NOT NULL AND tokens_used > 0 "
                "GROUP BY model ORDER BY 2 DESC"
            ):
                if isinstance(m, str) and m and t:
                    by_model[m] = int(t)
        stats = {
            "total_tokens": total,
            "sessions_count": sessions_count,
            "by_model": by_model,
        }
    except sqlite3.Error:
        stats = {}
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
    _CODEX_TOKEN_STATS_CACHE.set(key, (st.st_mtime_ns, st.st_size, dict(stats)))
    return stats


def read_codex_models_from_cache(profile_path: Path) -> tuple[list[dict[str, Any]], str]:
    cache_file = profile_path / "models_cache.json"
    payload = safe_json_read(cache_file)
    if isinstance(payload, dict) and isinstance(payload.get("models"), list):
        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in payload["models"]:
            if not isinstance(item, dict):
                continue
            slug = item.get("slug")
            if not isinstance(slug, str) or not slug or slug in seen:
                continue
            if slug == "gpt-reserve":
                continue
            visibility = item.get("visibility")
            is_hidden = visibility == "hide"
            if is_hidden and slug != "codex-auto-review":
                continue
            seen.add(slug)
            models.append(
                {
                    "id": slug,
                    "name": item.get("display_name") or CODEX_MODEL_LABELS.get(slug, slug),
                    "provider": "codex",
                    "family": model_family(slug),
                    "context_window": item.get("context_window"),
                    "effort": item.get("default_reasoning_level"),
                    "hidden": is_hidden,
                }
            )
        if models:
            return models, "cache"

    return (
        [
            {
                "id": slug,
                "name": CODEX_MODEL_LABELS.get(slug, slug),
                "provider": "codex",
                "family": model_family(slug),
                "context_window": None,
                "effort": None,
                "hidden": slug == "codex-auto-review",
            }
            for slug in DEFAULT_CODEX_MODELS
        ],
        "default",
    )


def normalize_codex_windows(
    data: dict[str, Any],
) -> tuple[list[dict[str, Any]], float | None, float | None]:
    windows: list[dict[str, Any]] = []
    if isinstance(data.get("windows"), list):
        for w in data["windows"]:
            if not isinstance(w, dict) or "remaining_percent" not in w:
                continue
            rem = round(max(0.0, min(100.0, float(w["remaining_percent"]))), 1)
            used = w.get("used_percent")
            if used is None:
                used = round(max(0.0, min(100.0, 100.0 - rem)), 1)
            mins = w.get("minutes")
            wname = str(w.get("window") or "primary")
            windows.append(
                {
                    "window": wname,
                    "label": w.get("label") or _codex_window_label(wname, mins),
                    "remaining_percent": rem,
                    "used_percent": round(float(used), 1),
                    "minutes": mins,
                    "resets_at": w.get("resets_at"),
                }
            )
    else:
        for key, used_k, mins_k, reset_k in (
            ("primary", "primary_used_percent", "primary_window_mins", "primary_resets_at"),
            (
                "secondary",
                "secondary_used_percent",
                "secondary_window_mins",
                "secondary_resets_at",
            ),
        ):
            if used_k not in data or data[used_k] is None:
                continue
            used = float(data[used_k])
            rem = round(max(0.0, min(100.0, 100.0 - used)), 1)
            mins = data.get(mins_k)
            windows.append(
                {
                    "window": key,
                    "label": _codex_window_label(key, mins),
                    "remaining_percent": rem,
                    "used_percent": round(used, 1),
                    "minutes": mins,
                    "resets_at": data.get(reset_k),
                }
            )

    if windows:
        rem_min = min(float(w["remaining_percent"]) for w in windows)
        min_w = min(windows, key=lambda w: float(w["remaining_percent"]))
        if min_w.get("resets_at"):
            best_resets: float | None = float(min_w["resets_at"])
        else:
            best_resets = next((float(w["resets_at"]) for w in windows if w.get("resets_at")), None)
        return windows, rem_min, best_resets
    if data.get("remaining_percent") is not None:
        rem = round(float(data["remaining_percent"]), 1)
        resets = float(data["resets_at"]) if data.get("resets_at") else None
        return [], rem, resets
    return [], None, None


def read_codex_quota_caches(
    hub: Hub, profile_name: str, auth_mtime: float | None
) -> tuple[dict[str, Any] | None, str]:
    dirs = [hub.store.root / "quotas", *ai_ask_state_dirs(hub)]
    candidates: list[Path] = []
    for d in dirs:
        candidates.append(d / f"codex-telemetry.{profile_name}.json")
        candidates.append(d / f"codex-usage.{profile_name}.json")
        candidates.append(d / f"codex-cache.{profile_name}.json")
    for cache_file in candidates:
        raw = safe_json_read(cache_file)
        if not isinstance(raw, dict):
            continue
        cached_mtime = raw.get("auth_mtime")
        if (
            auth_mtime is not None
            and isinstance(cached_mtime, (int, float))
            and abs(float(cached_mtime) - auth_mtime) > 1.0
        ):
            continue
        raw_inner = raw.get("data")
        data: dict[str, Any] = raw_inner if isinstance(raw_inner, dict) else raw
        if data.get("ok") is False and not data.get("logged_in", True):
            continue
        if any(
            k in data
            for k in ("windows", "primary_used_percent", "remaining_percent", "live_models")
        ):
            return data, "cache"
    return None, "none"


def build_codex_telemetry(hub: Hub, profile: dict[str, Any], now: float) -> dict[str, Any]:
    name = profile["name"]
    path = Path(profile["path"])
    locked, active = profile_lock_state(hub, profile)
    auth = parse_codex_auth_metadata(path)
    logged_in = bool(auth["logged_in"])
    cached_data, cache_status = read_codex_quota_caches(hub, name, auth.get("auth_mtime"))

    live_models: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    rem: float | None = None
    resets_at: float | None = None
    checked_at: float | None = None
    live_plan: str | None = None
    live_email: str | None = None
    reset_credits = 0
    usage_summary: dict[str, Any] = {}

    if cached_data:
        windows, rem, resets_at = normalize_codex_windows(cached_data)
        checked_at = cached_data.get("checked_at")
        live_plan = cached_data.get("plan")
        live_email = cached_data.get("email")
        reset_credits = int(cached_data.get("reset_credits") or 0)
        if isinstance(cached_data.get("usage_summary"), dict):
            usage_summary = cached_data["usage_summary"]
        if isinstance(cached_data.get("live_models"), list) and cached_data["live_models"]:
            live_models = cached_data["live_models"]

    if live_models:
        models = live_models
        models_source = "live"
    else:
        models, models_source = read_codex_models_from_cache(path)

    jwt_plan = auth.get("jwt_plan")
    active_until = auth.get("active_until")
    plan = live_plan or jwt_plan
    plan_expired = bool(active_until is not None and active_until < now)
    plan_expires_iso = epoch_to_date(active_until)
    if plan and jwt_plan and plan == "free" and jwt_plan != "free" and plan_expired:
        plan_detail = f"FREE ({jwt_plan.upper()} истёк {plan_expires_iso})"
    elif plan and plan_expires_iso:
        suffix = "истёк" if plan_expired else "до"
        plan_detail = f"{plan.upper()} · {suffix} {plan_expires_iso}"
    elif plan:
        plan_detail = str(plan).upper()
    else:
        plan_detail = "—" if not logged_in else "Codex"

    account_label = live_email or auth.get("email") or read_label_file(path)
    if not logged_in:
        rem = None
        quota_source = "unavailable"
    elif windows or rem is not None:
        quota_source = "cache" if cache_status == "cache" else "local"
    else:
        quota_source = "unavailable"

    in_cooldown = rem is not None and rem <= 0
    tokens = empty_tokens()
    sqlite_stats = read_codex_sqlite_tokens(path)
    if sqlite_stats:
        tokens["total_tokens"] = sqlite_stats.get("total_tokens")
        tokens["sessions_count"] = sqlite_stats.get("sessions_count")
        tokens["by_model"] = sqlite_stats.get("by_model") or {}
    if usage_summary:
        if usage_summary.get("lifetime_tokens") is not None:
            tokens["total_tokens"] = max(
                int(tokens["total_tokens"] or 0),
                int(usage_summary["lifetime_tokens"]),
            )
        if usage_summary.get("peak_daily_tokens") is not None:
            tokens["peak_daily_tokens"] = int(usage_summary["peak_daily_tokens"])
        if usage_summary.get("recent_7d_tokens") is not None:
            tokens["recent_7d_tokens"] = int(usage_summary["recent_7d_tokens"])

    return AccountTelemetry(
        provider="codex",
        profile=name,
        path=str(path),
        locked=locked,
        active=active,
        logged_in=logged_in,
        account_label=account_label,
        plan=plan,
        plan_detail=plan_detail,
        plan_expires_at=active_until,
        plan_expires_iso=plan_expires_iso,
        plan_expired=plan_expired,
        tier=plan or "codex",
        quota_source=quota_source,
        checked_at=checked_at,
        in_cooldown=in_cooldown,
        spent_buckets={"codex": resets_at} if in_cooldown and resets_at else {},
        remaining_percent=rem,
        resets_at=resets_at,
        windows=windows,
        model_quotas={},
        reset_credits=reset_credits,
        models=models,
        locked_models=[],
        models_source=models_source,
        models_cached=(path / "models_cache.json").is_file() or bool(live_models),
        tokens=tokens,
    ).to_dict()


def probe_codex_telemetry(hub: Hub, profile_name: str, timeout: float = 12.0) -> dict[str, Any]:
    profile = hub.profile("codex", profile_name)
    env = hub.environment("codex", profile)
    command = hub.command("codex", profile) + ["app-server", "--stdio"]
    try:
        with JsonRpcStdioClient(command, env=env) as client:
            client.request(
                "initialize",
                {"clientInfo": {"name": "harnesshub", "version": __version__}},
                timeout=timeout,
                timeout_error="Опрос квот Codex не ответил вовремя",
                rpc_error_prefix="Codex не поддержал запрос initialize; проверь вход и версию CLI",
            )
            client.notify("initialized")
            acc_res = client.request(
                "account/read",
                {},
                timeout=timeout,
                timeout_error="Опрос квот Codex не ответил вовремя",
                rpc_error_prefix=(
                    "Codex не поддержал запрос account/read; проверь вход и версию CLI"
                ),
            )
            account = acc_res.get("account")
            if not account:
                raise HubError("Codex: в этом профиле ещё не выполнен вход")
            rl_res = client.request(
                "account/rateLimits/read",
                {},
                timeout=timeout,
                timeout_error="Опрос квот Codex не ответил вовремя",
                rpc_error_prefix=(
                    "Codex не поддержал запрос account/rateLimits/read; проверь вход и версию CLI"
                ),
            )
            usage_res = client.request(
                "account/usage/read",
                {},
                timeout=timeout,
                optional=True,
            )
            models_res = client.request(
                "model/list",
                {},
                timeout=timeout,
                optional=True,
            )
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise HubError("Codex app-server прервал соединение") from exc
    except OSError as exc:
        raise HubError("Не удалось запустить Codex app-server") from exc

    limits = rl_res.get("rateLimits") or {}
    windows: list[dict[str, Any]] = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        if not isinstance(used, (int, float)):
            continue
        mins = window.get("windowDurationMins")
        windows.append(
            {
                "window": key,
                "label": _codex_window_label(key, mins),
                "remaining_percent": round(max(0.0, min(100.0, 100.0 - float(used))), 1),
                "used_percent": round(float(used), 1),
                "minutes": mins,
                "resets_at": window.get("resetsAt"),
            }
        )

    usage_summary: dict[str, Any] = {}
    if isinstance(usage_res, dict) and isinstance(usage_res.get("summary"), dict):
        s = usage_res["summary"]
        buckets = usage_res.get("dailyUsageBuckets") or []
        recent_7d = sum(int(b.get("tokens") or 0) for b in buckets[-7:] if isinstance(b, dict))
        usage_summary = {
            "lifetime_tokens": s.get("lifetimeTokens"),
            "peak_daily_tokens": s.get("peakDailyTokens"),
            "recent_7d_tokens": recent_7d if buckets else None,
        }

    live_models: list[dict[str, Any]] = []
    if isinstance(models_res, dict) and isinstance(models_res.get("data"), list):
        seen: set[str] = set()
        for item in models_res["data"]:
            if not isinstance(item, dict):
                continue
            slug = item.get("id") or item.get("slug")
            if not isinstance(slug, str) or not slug or slug in seen or slug == "gpt-reserve":
                continue
            is_hidden = bool(item.get("hidden"))
            if is_hidden and slug != "codex-auto-review":
                continue
            seen.add(slug)
            live_models.append(
                {
                    "id": slug,
                    "name": item.get("displayName")
                    or item.get("display_name")
                    or CODEX_MODEL_LABELS.get(slug, slug),
                    "provider": "codex",
                    "family": model_family(slug),
                    "context_window": item.get("contextWindow"),
                    "effort": item.get("defaultReasoningEffort"),
                    "hidden": is_hidden,
                }
            )

    reset_credits = int((rl_res.get("rateLimitResetCredits") or {}).get("availableCount") or 0)
    plan = limits.get("planType") or account.get("planType")
    auth = parse_codex_auth_metadata(Path(profile["path"]))
    now = time.time()
    payload = {
        "provider": "codex",
        "profile": profile["name"],
        "source": "live",
        "checked_at": now,
        "auth_mtime": auth.get("auth_mtime"),
        "plan": plan,
        "email": account.get("email") or auth.get("email"),
        "windows": windows,
        "reset_credits": reset_credits,
        "usage_summary": usage_summary,
        "live_models": live_models,
    }
    try:
        quota_dir = hub.store.root / "quotas"
        atomic_write_json(quota_dir / f"codex-usage.{profile['name']}.json", payload)
        atomic_write_json(quota_dir / f"codex-telemetry.{profile['name']}.json", payload)
    except OSError:
        pass

    result = build_codex_telemetry(hub, profile, now)
    result["quota_source"] = "live"
    result["source"] = "live"
    return result


def collect_codex_sqlite_entries(
    profile_name: str,
    profile_path: Path,
    *,
    include_subagents: bool = False,
) -> tuple[list[dict[str, Any]], set[str]]:
    db_path = profile_path / "state_5.sqlite"
    if not db_path.is_file():
        return [], set()

    entries: list[dict[str, Any]] = []
    covered_paths: set[str] = set()
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        cols = {row[1] for row in con.execute("PRAGMA table_info(threads)").fetchall()}
        if not cols or "id" not in cols or "rollout_path" not in cols:
            return [], set()

        wanted = [
            "id",
            "rollout_path",
            "updated_at" if "updated_at" in cols else "0 AS updated_at",
            "updated_at_ms" if "updated_at_ms" in cols else "NULL AS updated_at_ms",
            "cwd" if "cwd" in cols else "'' AS cwd",
            "title" if "title" in cols else "'' AS title",
            "name" if "name" in cols else "NULL AS name",
            "first_user_message" if "first_user_message" in cols else "'' AS first_user_message",
            "tokens_used" if "tokens_used" in cols else "NULL AS tokens_used",
            "model" if "model" in cols else "NULL AS model",
            "thread_source" if "thread_source" in cols else "'' AS thread_source",
            "archived" if "archived" in cols else "0 AS archived",
        ]
        sql = (
            f"SELECT {', '.join(wanted)} FROM threads "
            "ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC"
        )
        for row in con.execute(sql):
            (
                tid,
                rpath,
                upd_s,
                upd_ms,
                cwd,
                title,
                name,
                first_msg,
                tokens_used,
                model,
                thread_source,
                archived,
            ) = row
            if archived:
                continue
            if thread_source == "subagent" and not include_subagents:
                continue
            rp = Path(str(rpath)) if rpath else None
            norm_rpath = str(rpath).replace("\\", "/") if rpath else ""
            if rp is not None and not rp.is_file() and "/sessions/" in norm_rpath:
                rel = norm_rpath.split("/sessions/", 1)[1]
                cand = profile_path / "sessions" / Path(*[part for part in rel.split("/") if part])
                if cand.is_file():
                    rp = cand
            if rp is None or not rp.is_file():
                continue
            real = os.path.realpath(rp)
            covered_paths.add(real)
            ts = float(upd_ms) / 1000.0 if upd_ms else float(upd_s or 0)
            if ts <= 0:
                try:
                    ts = rp.stat().st_mtime
                except OSError:
                    ts = 0.0
            raw_title = name or title or first_msg or str(tid)
            entries.append(
                SessionSummary(
                    session_id=str(tid),
                    provider="codex",
                    profile=profile_name,
                    cwd=str(cwd or ""),
                    project_name=project_name(str(cwd or "")),
                    model=str(model or "gpt-6"),
                    title=shorten(str(raw_title)),
                    turns=None,
                    tokens_used=int(tokens_used) if tokens_used is not None else None,
                    updated_at=ts,
                    updated_at_iso=epoch_to_iso(ts),
                    transcript_path=str(rp),
                ).to_dict()
            )
    except sqlite3.Error:
        return [], set()
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
    return entries, covered_paths


def collect_codex_candidates(
    profile_name: str,
    profile_path: Path,
    covered_paths: set[str] | None = None,
) -> list[tuple[Any, ...]]:
    sessions_dir = profile_path / "sessions"
    if not sessions_dir.is_dir() and (profile_path / ".codex" / "sessions").is_dir():
        sessions_dir = profile_path / ".codex" / "sessions"
    if not sessions_dir.is_dir():
        return []

    covered = covered_paths or set()
    candidates: list[tuple[Any, ...]] = []
    for jsonl in sessions_dir.glob("*/*/*/*.jsonl"):
        if covered and os.path.realpath(jsonl) in covered:
            continue
        try:
            st = jsonl.stat()
        except OSError:
            continue
        candidates.append((st.st_mtime_ns, st.st_size, st.st_mtime, "codex", profile_name, jsonl))
    return candidates


def parse_codex_session(
    profile_name: str,
    jsonl: Path,
    st_mtime_ns: int,
    st_size: int,
    st_mtime: float,
    *,
    clean_user_text: Any,
    meta_cache: BoundedCache[str, tuple[int, int, dict[str, Any] | None]],
    include_subagents: bool = False,
) -> dict[str, Any] | None:
    cache_key = str(jsonl)
    cached = meta_cache.get(cache_key)
    if cached and cached[0] == st_mtime_ns and cached[1] == st_size:
        if cached[2] is None:
            return None
        if cached[2].get("_is_subagent") and not include_subagents:
            return None
        entry = {k: v for k, v in cached[2].items() if k != "_is_subagent"}
        entry["profile"] = profile_name
        return entry

    session_id = ""
    cwd = ""
    model = "gpt-6"
    first_user = ""
    head_real_turns = 0
    synthetic_in_head = 0
    last_ts = st_mtime
    is_subagent = False

    def _scan_lines(lines: list[str], *, skip_large: bool) -> None:
        nonlocal session_id, cwd, model, first_user, head_real_turns, synthetic_in_head, last_ts
        nonlocal is_subagent
        for line in lines:
            if skip_large and len(line) > 98304:
                continue
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = iso_to_epoch_or_zero(row.get("timestamp"))
            if ts > last_ts:
                last_ts = ts
            rtype = row.get("type")
            payload = row.get("payload") or {}
            if rtype == "session_meta":
                session_id = payload.get("id") or payload.get("session_id") or session_id
                cwd = payload.get("cwd") or cwd
                if payload.get("thread_source") == "subagent":
                    is_subagent = True
                base_inst = payload.get("base_instructions") or {}
                prov = base_inst.get("provenance") or {}
                if isinstance(prov.get("model"), str):
                    model = prov["model"]
            elif rtype == "response_item" and payload.get("type") == "message":
                if payload.get("role") == "user":
                    text = extract_content_text(payload.get("content", "")).strip()
                    if not text or text.startswith(SYNTHETIC_CODEX_PREFIXES):
                        synthetic_in_head += 1
                        continue
                    head_real_turns += 1
                    if not first_user:
                        first_user = clean_user_text(text)

    if st_size <= MAX_FULL_TRANSCRIPT_BYTES:
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as stream:
                _scan_lines(list(stream), skip_large=False)
        except OSError:
            pass
        turns = head_real_turns
    else:
        head_lines, marker_turns = read_head_and_count(
            jsonl,
            (b'"role":"user"', b'"role": "user"'),
            st_size,
            head_bytes=98304,
            max_lines=30,
            scan_bytes=131072,
        )
        _scan_lines(head_lines, skip_large=True)
        if not session_id and not first_user:
            _scan_lines(
                fast_head_lines_large(jsonl, max_bytes=262144, max_lines=15), skip_large=True
            )
        turns = max(head_real_turns, marker_turns - synthetic_in_head)

    if not session_id:
        session_id = jsonl.stem.removeprefix("rollout-")
    if first_user and turns == 0:
        turns = 1
    if not first_user and turns == 0:
        meta_cache.set(cache_key, (st_mtime_ns, st_size, None))
        return None

    cached_entry = {
        **SessionSummary(
            session_id=session_id,
            provider="codex",
            profile=profile_name,
            cwd=cwd,
            project_name=project_name(cwd),
            model=model,
            title=shorten(first_user or session_id),
            turns=turns,
            tokens_used=None,
            updated_at=last_ts,
            updated_at_iso=epoch_to_iso(last_ts),
            transcript_path=str(jsonl),
        ).to_dict(),
        "_is_subagent": is_subagent,
    }
    meta_cache.set(cache_key, (st_mtime_ns, st_size, cached_entry))
    if is_subagent and not include_subagents:
        return None
    return {k: v for k, v in cached_entry.items() if k != "_is_subagent"}


def parse_codex_messages(transcript_path: Path, clean_user_text: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if not transcript_path.is_file():
        return messages
    with transcript_path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") == "response_item":
                payload = row.get("payload") or {}
                if payload.get("type") == "message" and payload.get("role") in (
                    "user",
                    "assistant",
                ):
                    role = payload["role"]
                    text = extract_content_text(payload.get("content", "")).strip()
                    if role == "user" and text.startswith(SYNTHETIC_CODEX_PREFIXES):
                        continue
                    if role == "user":
                        text = clean_user_text(text)
                    if text:
                        messages.append(
                            {
                                "role": role,
                                "text": text,
                                "timestamp": row.get("timestamp") or "",
                            }
                        )
    return messages


class CodexAdapter:
    provider_id = "codex"

    def build_telemetry(self, hub: Hub, profile: dict[str, Any], now: float) -> dict[str, Any]:
        return build_codex_telemetry(hub, profile, now)

    def probe_telemetry(self, hub: Hub, profile_name: str, timeout: float = 12.0) -> dict[str, Any]:
        return probe_codex_telemetry(hub, profile_name, timeout=timeout)

    def parse_messages(self, transcript_path: Path) -> list[dict[str, Any]]:
        from harnesshub.sessions import _clean_user_text

        return parse_codex_messages(transcript_path, _clean_user_text)
