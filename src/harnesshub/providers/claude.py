from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesshub.catalog_loader import CLAUDE_MODEL_LABELS, DEFAULT_CLAUDE_MODELS
from harnesshub.errors import HubError
from harnesshub.providers.base import (
    MAX_FULL_TRANSCRIPT_BYTES,
    AccountTelemetry,
    BoundedCache,
    SessionSummary,
    ai_ask_state_dirs,
    atomic_write_json,
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

if TYPE_CHECKING:
    from harnesshub.service import Hub


def read_claude_quota_state(
    hub: Hub, profile_name: str, now: float | None = None
) -> dict[str, Any]:
    now = time.time() if now is None else now
    resets_at: float | None = None
    for state_dir in ai_ask_state_dirs(hub):
        for candidate in (
            state_dir / f"claude-cooldown.{profile_name}",
            state_dir / "claude_cooldown",
        ):
            if candidate.is_file():
                try:
                    ts = float(candidate.read_text(encoding="utf-8").strip())
                    if ts > now:
                        resets_at = max(resets_at or 0.0, ts)
                except (OSError, ValueError):
                    pass
    return {
        "in_cooldown": resets_at is not None,
        "resets_at": resets_at,
        "remaining_percent": 0.0 if resets_at is not None else None,
    }


def read_claude_model_catalog(
    profile_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    cat_dir = profile_path / "cache" / "model-catalog"
    if cat_dir.is_dir():
        files = sorted(cat_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for cf in files:
            payload = safe_json_read(cf)
            if not isinstance(payload, dict):
                continue
            raw_models = (((payload.get("catalog") or {}).get("config") or {}).get("models")) or []
            if not isinstance(raw_models, list) or not raw_models:
                continue
            enabled: list[dict[str, Any]] = []
            locked: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                mid = item.get("id")
                if not isinstance(mid, str) or not mid or mid in seen:
                    continue
                seen.add(mid)
                name = item.get("name") or CLAUDE_MODEL_LABELS.get(mid, mid)
                if item.get("disabled") is True:
                    badge = (item.get("badge") or {}).get("message") or "План"
                    locked.append({"id": mid, "name": name, "badge": badge})
                    continue
                runtime = item.get("runtime") or {}
                enabled.append(
                    {
                        "id": mid,
                        "name": name,
                        "provider": "claude",
                        "family": model_family(mid),
                        "context_window": runtime.get("max_input_tokens"),
                        "effort": runtime.get("default_effort"),
                        "hidden": False,
                    }
                )
            if not enabled and locked:
                enabled = [
                    {
                        "id": m["id"],
                        "name": m["name"],
                        "provider": "claude",
                        "family": model_family(m["id"]),
                        "context_window": None,
                        "effort": None,
                        "hidden": False,
                    }
                    for m in locked
                    if "sonnet" in m["id"] or "haiku" in m["id"]
                ]
            if enabled or locked:
                return enabled, locked, "cache"

    return (
        [
            {
                "id": slug,
                "name": CLAUDE_MODEL_LABELS.get(slug, slug),
                "provider": "claude",
                "family": model_family(slug),
                "context_window": None,
                "effort": None,
                "hidden": False,
            }
            for slug in DEFAULT_CLAUDE_MODELS
        ],
        [],
        "default",
    )


def read_claude_stats_cache(profile_path: Path) -> dict[str, Any]:
    sc = safe_json_read(profile_path / "stats-cache.json")
    if not isinstance(sc, dict):
        return {}
    inp = 0
    out = 0
    cached = 0
    by_model: dict[str, int] = {}
    for m, mu in (sc.get("modelUsage") or {}).items():
        if not isinstance(mu, dict):
            continue
        mi = int(mu.get("inputTokens") or 0)
        mo = int(mu.get("outputTokens") or 0)
        mc = int(mu.get("cacheReadInputTokens") or 0) + int(mu.get("cacheCreationInputTokens") or 0)
        inp += mi
        out += mo
        cached += mc
        total_m = mi + mo + mc
        if total_m > 0:
            by_model[m] = total_m

    daily = sc.get("dailyModelTokens") or []
    daily_totals: list[int] = []
    for day in daily:
        if isinstance(day, dict) and isinstance(day.get("tokensByModel"), dict):
            daily_totals.append(sum(int(v or 0) for v in day["tokensByModel"].values()))
    recent_7d = sum(daily_totals[-7:]) if daily_totals else None
    peak_daily = max(daily_totals) if daily_totals else None

    return {
        "total_tokens": inp + out + cached,
        "input_tokens": inp,
        "output_tokens": out,
        "cached_tokens": cached,
        "recent_7d_tokens": recent_7d,
        "peak_daily_tokens": peak_daily,
        "sessions_count": sc.get("totalSessions"),
        "messages_count": sc.get("totalMessages"),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: kv[1], reverse=True)),
    }


def build_claude_telemetry(hub: Hub, profile: dict[str, Any], now: float) -> dict[str, Any]:
    name = profile["name"]
    path = Path(profile["path"])
    locked, active = profile_lock_state(hub, profile)
    creds_file = path / ".credentials.json"
    creds = safe_json_read(creds_file)
    logged_in = creds_file.is_file() or any(path.glob("*.json"))

    sub_type: str | None = None
    rate_tier: str | None = None
    expires_at: float | None = None
    if isinstance(creds, dict) and isinstance(creds.get("claudeAiOauth"), dict):
        oa = creds["claudeAiOauth"]
        sub_type = oa.get("subscriptionType") or sub_type
        rate_tier = oa.get("rateLimitTier") or rate_tier
        expires_at = iso_to_epoch(oa.get("expiresAt"))

    account_label = read_label_file(path)
    claude_json_path = path / ".claude.json"
    if not claude_json_path.is_file() and path.resolve() == (Path.home() / ".claude").resolve():
        claude_json_path = Path.home() / ".claude.json"
    cjson = safe_json_read(claude_json_path)
    if isinstance(cjson, dict) and isinstance(cjson.get("oauthAccount"), dict):
        acc = cjson["oauthAccount"]
        account_label = acc.get("emailAddress") or acc.get("displayName") or account_label
        if not sub_type and isinstance(acc.get("organizationType"), str):
            sub_type = acc["organizationType"].removeprefix("claude_")

    cached = safe_json_read(hub.store.root / "quotas" / f"claude-telemetry.{name}.json")
    if isinstance(cached, dict):
        sub_type = cached.get("plan") or sub_type
        expires_at = cached.get("plan_expires_at") or expires_at
        account_label = cached.get("account_label") or account_label

    quota_state = read_claude_quota_state(hub, name, now)
    in_cooldown = bool(quota_state["in_cooldown"])
    resets_at = quota_state["resets_at"]

    models, locked_models, models_source = read_claude_model_catalog(path)
    tokens = empty_tokens()
    stats = read_claude_stats_cache(path)
    if stats:
        tokens.update(stats)

    plan_expires_iso = epoch_to_date(expires_at)
    plan_expired = bool(expires_at is not None and expires_at < now)
    plan = sub_type or "claude"
    if sub_type and plan_expires_iso:
        suffix = "вход истёк" if plan_expired else "вход до"
        plan_detail = f"{sub_type.upper()} · {suffix} {plan_expires_iso}"
    elif sub_type:
        plan_detail = sub_type.upper()
    else:
        plan_detail = "Claude" if logged_in else "—"

    rem = 0.0 if in_cooldown else None
    quota_source = "cooldown" if in_cooldown else ("local" if logged_in else "unavailable")
    windows = (
        [
            {
                "window": "primary",
                "label": "429 кулдаун",
                "remaining_percent": 0.0,
                "used_percent": 100.0,
                "minutes": None,
                "resets_at": int(resets_at) if resets_at else None,
            }
        ]
        if in_cooldown
        else []
    )

    return AccountTelemetry(
        provider="claude",
        profile=name,
        path=str(path),
        locked=locked,
        active=active,
        logged_in=logged_in,
        account_label=account_label,
        plan=plan,
        plan_detail=plan_detail,
        plan_expires_at=expires_at,
        plan_expires_iso=plan_expires_iso,
        plan_expired=plan_expired,
        tier=plan,
        quota_source=quota_source,
        checked_at=cached.get("checked_at") if isinstance(cached, dict) else None,
        in_cooldown=in_cooldown,
        spent_buckets={"claude": resets_at} if in_cooldown and resets_at else {},
        remaining_percent=rem,
        resets_at=resets_at,
        windows=windows,
        model_quotas={},
        reset_credits=0,
        models=models,
        locked_models=locked_models,
        models_source=models_source,
        models_cached=models_source == "cache",
        rate_limit_tier=rate_tier,
        tokens=tokens,
    ).to_dict()


def probe_claude_telemetry(hub: Hub, profile_name: str, timeout: float = 8.0) -> dict[str, Any]:
    profile = hub.profile("claude", profile_name)
    spec = hub.harness("claude")
    executable = spec.executable()
    if executable is None:
        raise HubError("Claude Code не найден в PATH")
    base_cmd = list(spec.launcher) if spec.launcher else [executable]
    env = hub.environment("claude", profile)
    try:
        completed = subprocess.run(
            [*base_cmd, "auth", "status", "--json"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HubError(f"claude auth status не ответил за {timeout:g} секунд") from exc
    except OSError as exc:
        raise HubError(f"Не удалось запустить claude auth status: {exc}") from exc

    parsed: dict[str, Any] = {}
    if completed.stdout.strip():
        try:
            raw = json.loads(completed.stdout)
            if isinstance(raw, dict):
                parsed = raw
        except json.JSONDecodeError:
            parsed = {}
    if completed.returncode != 0 and not parsed:
        raise HubError("Claude Code: не удалось проверить статус входа")

    now = time.time()
    payload = {
        "provider": "claude",
        "profile": profile_name,
        "source": "live",
        "checked_at": now,
        "logged_in": bool(parsed.get("loggedIn")),
        "plan": parsed.get("subscriptionType") or parsed.get("planTier"),
        "plan_expires_at": iso_to_epoch(parsed.get("loginExpiresAt")),
        "account_label": parsed.get("email"),
    }
    try:
        atomic_write_json(
            hub.store.root / "quotas" / f"claude-telemetry.{profile_name}.json", payload
        )
    except OSError:
        pass

    result = build_claude_telemetry(hub, profile, now)
    result["source"] = "live"
    return result


def collect_claude_candidates(profile_name: str, profile_path: Path) -> list[tuple[Any, ...]]:
    projects_dir = profile_path / "projects"
    if not projects_dir.is_dir() and (profile_path / ".claude" / "projects").is_dir():
        projects_dir = profile_path / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []

    candidates: list[tuple[Any, ...]] = []
    for jsonl in projects_dir.glob("*/*.jsonl"):
        try:
            st = jsonl.stat()
        except OSError:
            continue
        candidates.append((st.st_mtime_ns, st.st_size, st.st_mtime, "claude", profile_name, jsonl))
    return candidates


def parse_claude_session(
    profile_name: str,
    jsonl: Path,
    st_mtime_ns: int,
    st_size: int,
    st_mtime: float,
    *,
    clean_user_text: Any,
    meta_cache: BoundedCache[str, tuple[int, int, dict[str, Any] | None]],
) -> dict[str, Any] | None:
    cache_key = str(jsonl)
    cached = meta_cache.get(cache_key)
    if cached and cached[0] == st_mtime_ns and cached[1] == st_size:
        if cached[2] is None:
            return None
        entry = dict(cached[2])
        entry["profile"] = profile_name
        return entry

    session_id = jsonl.stem
    cwd = ""
    model = "claude"
    first_user = ""
    last_ts = st_mtime
    tokens_total = 0
    has_tokens = False
    turn_count = 0

    def _scan_lines(lines: list[str], *, count_turns: bool, skip_large: bool) -> None:
        nonlocal cwd, model, first_user, last_ts, tokens_total, has_tokens, turn_count
        for line in lines:
            if skip_large and len(line) > 65536:
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
            if not cwd and isinstance(row.get("cwd"), str):
                cwd = row["cwd"]
            if not cwd and isinstance(row.get("attachment"), dict):
                snap = row["attachment"].get("snapshot") or {}
                if isinstance(snap.get("workingDirectory"), str):
                    cwd = snap["workingDirectory"]
            rtype = row.get("type")
            if rtype == "user":
                if count_turns:
                    turn_count += 1
                msg = row.get("message") or {}
                text = clean_user_text(extract_content_text(msg.get("content", "")))
                if text and not first_user:
                    first_user = text
            elif rtype == "assistant":
                msg = row.get("message") or {}
                if isinstance(msg.get("model"), str):
                    model = msg["model"]
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    inp = int(usage.get("input_tokens") or 0)
                    out = int(usage.get("output_tokens") or 0)
                    if inp or out:
                        tokens_total += inp + out
                        has_tokens = True
            elif rtype == "queue-operation" and row.get("operation") == "enqueue":
                if not first_user and isinstance(row.get("content"), str):
                    first_user = clean_user_text(row["content"])

    if st_size <= MAX_FULL_TRANSCRIPT_BYTES:
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as stream:
                _scan_lines(list(stream), count_turns=True, skip_large=False)
        except OSError:
            pass
    else:
        head_lines, turn_count = read_head_and_count(
            jsonl,
            (b'"type":"user"', b'"type": "user"'),
            st_size,
            head_bytes=65536,
            max_lines=25,
            scan_bytes=131072,
        )
        _scan_lines(head_lines, count_turns=False, skip_large=True)
        if not first_user and not cwd:
            _scan_lines(
                fast_head_lines_large(jsonl, max_bytes=262144, max_lines=10),
                count_turns=False,
                skip_large=True,
            )
        has_tokens = False
        tokens_total = 0

    if not first_user and turn_count == 0:
        meta_cache.set(cache_key, (st_mtime_ns, st_size, None))
        return None

    result = SessionSummary(
        session_id=session_id,
        provider="claude",
        profile=profile_name,
        cwd=cwd,
        project_name=project_name(cwd),
        model=model,
        title=shorten(first_user or session_id),
        turns=max(1, turn_count),
        tokens_used=tokens_total if has_tokens else None,
        updated_at=last_ts,
        updated_at_iso=epoch_to_iso(last_ts),
        transcript_path=str(jsonl),
    ).to_dict()
    meta_cache.set(cache_key, (st_mtime_ns, st_size, dict(result)))
    return result


def parse_claude_messages(transcript_path: Path, clean_user_text: Any) -> list[dict[str, Any]]:
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
            rtype = row.get("type")
            if rtype in ("user", "assistant"):
                msg = row.get("message") or {}
                text = extract_content_text(msg.get("content", "")).strip()
                if rtype == "user":
                    text = clean_user_text(text)
                if text:
                    messages.append(
                        {
                            "role": rtype,
                            "text": text,
                            "timestamp": row.get("timestamp") or "",
                        }
                    )
    return messages


class ClaudeAdapter:
    provider_id = "claude"

    def build_telemetry(self, hub: Hub, profile: dict[str, Any], now: float) -> dict[str, Any]:
        return build_claude_telemetry(hub, profile, now)

    def probe_telemetry(self, hub: Hub, profile_name: str, timeout: float = 8.0) -> dict[str, Any]:
        return probe_claude_telemetry(hub, profile_name, timeout=timeout)

    def parse_messages(self, transcript_path: Path) -> list[dict[str, Any]]:
        from harnesshub.sessions import _clean_user_text

        return parse_claude_messages(transcript_path, _clean_user_text)
