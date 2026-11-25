from __future__ import annotations

import contextvars
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesshub.catalog_loader import AGY_MODEL_LABELS, AGY_POOL_MODELS, DEFAULT_AGY_MODELS
from harnesshub.errors import HubError
from harnesshub.providers.base import (
    MAX_FULL_TRANSCRIPT_BYTES,
    AccountTelemetry,
    BoundedCache,
    SessionSummary,
    ai_ask_state_dirs,
    atomic_write_json,
    atomic_write_text,
    classify_agy_tier,
    cwd_matches,
    empty_tokens,
    epoch_to_iso,
    iso_to_epoch,
    iso_to_epoch_or_zero,
    model_family,
    profile_lock_state,
    project_name,
    quota_bucket,
    read_head_and_count,
    read_label_file,
    safe_json_read,
    shorten,
    strip_file_uri,
)
from harnesshub.rpc import JsonRpcStdioClient

if TYPE_CHECKING:
    from harnesshub.service import Hub

_AGY_TOKEN_STATS_CACHE: BoundedCache[str, tuple[tuple[int, int, int], dict[str, Any]]] = (
    BoundedCache(128)
)
_AGY_VERSION_CACHE: BoundedCache[tuple[str, int], str] = BoundedCache(32)
_AGY_USER_AGENT_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agy_user_agent", default="antigravity/1.5.16"
)


def agy_base_dir(profile_path: Path) -> Path:
    nested = profile_path / ".gemini" / "antigravity-cli"
    if nested.is_dir():
        return nested
    return profile_path


def agy_models_file(profile_path: Path) -> Path:
    direct = profile_path / ".models"
    nested = profile_path / ".gemini" / "antigravity-cli" / ".models"
    if direct.is_file() and direct.stat().st_size > 0:
        return direct
    if nested.is_file() and nested.stat().st_size > 0:
        return nested
    return direct


def read_agy_profile_models(profile_path: Path) -> tuple[list[str], str]:
    mf = agy_models_file(profile_path)
    models: list[str] = []
    if mf.is_file():
        try:
            models = sorted(
                {
                    line.strip()
                    for line in mf.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip()
                }
            )
        except OSError:
            models = []
    tier = classify_agy_tier(models)
    return models, tier


def read_agy_quota_state(hub: Hub, profile_name: str, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    spent: dict[str, float] = {}
    cooldown_until: float | None = None
    for state_dir in ai_ask_state_dirs(hub):
        if not state_dir.is_dir():
            continue
        cd_file = state_dir / f"cooldown.{profile_name}"
        if cd_file.is_file():
            try:
                ts = float(cd_file.read_text(encoding="utf-8").strip())
                if ts > now:
                    cooldown_until = max(cooldown_until or 0.0, ts)
            except (OSError, ValueError):
                pass
        prefix = f"agy-quota.{profile_name}."
        try:
            entries = list(state_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            bucket = entry.name[len(prefix) :]
            try:
                ts = float(entry.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if ts > now:
                spent[bucket] = max(spent.get(bucket, 0.0), ts)
    return {
        "in_cooldown": cooldown_until is not None,
        "cooldown_until": cooldown_until,
        "spent_buckets": spent,
    }


def scan_agy_token_stats(base: Path) -> dict[str, Any]:
    brain_dir = base / "brain"
    if not brain_dir.is_dir():
        return {}
    try:
        entries = [d for d in brain_dir.iterdir() if d.is_dir()]
    except OSError:
        return {}

    transcripts: list[tuple[int, int, Path]] = []
    max_mtime_ns = 0
    sum_sizes = 0
    for conv_dir in entries:
        tr = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
        try:
            tr_st = tr.stat()
            if tr_st.st_size > 0:
                transcripts.append((tr_st.st_mtime_ns, tr_st.st_size, tr))
                if tr_st.st_mtime_ns > max_mtime_ns:
                    max_mtime_ns = tr_st.st_mtime_ns
                sum_sizes += tr_st.st_size
        except OSError:
            continue

    key = os.path.realpath(brain_dir)
    is_shared = brain_dir.is_symlink()
    sig = (len(transcripts), max_mtime_ns, sum_sizes)
    cached = _AGY_TOKEN_STATS_CACHE.get(key)
    if cached and cached[0] == sig:
        result = dict(cached[1])
        result["shared_history"] = is_shared
        result["stats_source_key"] = key
        return result

    sessions_count = len(entries)
    summaries_db = base / "conversation_summaries.db"
    if summaries_db.is_file():
        con: sqlite3.Connection | None = None
        try:
            con = sqlite3.connect(f"file:{summaries_db}?mode=ro", uri=True, timeout=0.3)
            row = con.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()
            if row and row[0]:
                sessions_count = int(row[0])
        except sqlite3.Error:
            pass
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

    transcripts.sort(key=lambda item: item[0], reverse=True)

    inp_sum = 0
    out_sum = 0
    cache_sum = 0
    by_model: dict[str, int] = {}
    for _, _, tr in transcripts[:120]:
        try:
            with tr.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if "PLANNER_RESPONSE" not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("type") != "PLANNER_RESPONSE":
                        continue
                    inp = int(row.get("input_tokens") or 0)
                    out = int(row.get("output_tokens") or 0)
                    cr = int(row.get("cache_read_tokens") or 0)
                    inp_sum += inp
                    out_sum += out
                    cache_sum += cr
                    m = row.get("model")
                    if isinstance(m, str) and m:
                        by_model[m] = by_model.get(m, 0) + inp + out
        except OSError:
            continue

    stats = {
        "total_tokens": inp_sum + out_sum,
        "input_tokens": inp_sum,
        "output_tokens": out_sum,
        "cached_tokens": cache_sum,
        "sessions_count": sessions_count,
        "by_model": dict(sorted(by_model.items(), key=lambda kv: kv[1], reverse=True)),
        "shared_history": is_shared,
        "stats_source_key": key,
    }
    _AGY_TOKEN_STATS_CACHE.set(key, (sig, dict(stats)))
    return stats


def read_agy_token_meta(profile_path: Path) -> tuple[bool, str | None, str | None]:
    base = agy_base_dir(profile_path)
    candidates = [
        profile_path / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
        base / "antigravity-oauth-token",
    ]
    tok_file = next((c for c in candidates if c.is_file() and c.stat().st_size > 0), None)
    if tok_file is None:
        return False, read_label_file(profile_path), None
    raw = safe_json_read(tok_file)
    email = None
    access_token = None
    if isinstance(raw, dict):
        if isinstance(raw.get("id_token_email"), str) and raw["id_token_email"]:
            email = raw["id_token_email"]
        tok_obj = raw.get("token")
        if isinstance(tok_obj, dict) and isinstance(tok_obj.get("access_token"), str):
            access_token = tok_obj["access_token"]
        elif isinstance(raw.get("access_token"), str):
            access_token = raw["access_token"]
    return True, email or read_label_file(profile_path), access_token


def build_agy_telemetry(hub: Hub, profile: dict[str, Any], now: float) -> dict[str, Any]:
    name = profile["name"]
    path = Path(profile["path"])
    base = agy_base_dir(path)
    locked, active = profile_lock_state(hub, profile)
    logged_in, account_label, _ = read_agy_token_meta(path)

    cached_raw = safe_json_read(hub.store.root / "quotas" / f"agy-telemetry.{name}.json")
    cached = cached_raw if isinstance(cached_raw, dict) else {}

    slugs, tier = read_agy_profile_models(path)
    live_models_map: dict[str, dict[str, Any]] = {}
    for item in cached.get("models") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            live_models_map[item["id"]] = item

    if slugs:
        models = [
            {
                "id": slug,
                "name": (live_models_map.get(slug) or {}).get("name")
                or AGY_MODEL_LABELS.get(slug, slug),
                "provider": "agy",
                "family": model_family(slug),
                "context_window": (live_models_map.get(slug) or {}).get("context_window"),
                "effort": None,
                "hidden": False,
            }
            for slug in slugs
        ]
        models_source = "cache"
    elif live_models_map:
        models = [
            {
                "id": item["id"],
                "name": item.get("name") or AGY_MODEL_LABELS.get(item["id"], item["id"]),
                "provider": "agy",
                "family": model_family(item["id"]),
                "context_window": item.get("context_window"),
                "effort": None,
                "hidden": False,
            }
            for item in live_models_map.values()
        ]
        tier = classify_agy_tier([str(m["id"]) for m in models])
        models_source = "live"
    else:
        models = [
            {
                "id": slug,
                "name": AGY_MODEL_LABELS.get(slug, slug),
                "provider": "agy",
                "family": model_family(slug),
                "context_window": None,
                "effort": None,
                "hidden": False,
            }
            for slug in DEFAULT_AGY_MODELS
        ]
        models_source = "default"

    quota_state = read_agy_quota_state(hub, name, now)
    spent_buckets = dict(quota_state["spent_buckets"])
    in_cooldown = bool(quota_state["in_cooldown"])

    windows: list[dict[str, Any]] = []
    model_quotas: dict[str, dict[str, Any]] = {}
    if isinstance(cached.get("model_quotas"), dict):
        for m_id, mq in cached["model_quotas"].items():
            if isinstance(mq, dict) and mq.get("remaining_percent") is not None:
                b = quota_bucket(m_id)
                if b in spent_buckets or in_cooldown:
                    model_quotas[m_id] = {
                        "remaining_percent": 0.0,
                        "resets_at": int(spent_buckets.get(b) or quota_state["cooldown_until"] or 0)
                        or None,
                    }
                else:
                    model_quotas[m_id] = dict(mq)

    if isinstance(cached.get("windows"), list):
        for w in cached["windows"]:
            if not isinstance(w, dict) or w.get("remaining_percent") is None:
                continue
            rem_val = round(max(0.0, min(100.0, float(w["remaining_percent"]))), 1)
            w_id = str(w.get("window") or "")
            w_resets = w.get("resets_at")
            if in_cooldown:
                rem_val = 0.0
                w_resets = quota_state["cooldown_until"] or w_resets
            elif w_id in spent_buckets:
                rem_val = 0.0
                w_resets = spent_buckets[w_id]
            windows.append(
                {
                    "window": w_id,
                    "label": w.get("label") or w_id,
                    "remaining_percent": rem_val,
                    "used_percent": round(100.0 - rem_val, 1),
                    "minutes": w.get("minutes"),
                    "resets_at": int(w_resets) if w_resets else None,
                }
            )

    if windows:
        rem: float | None = 0.0 if in_cooldown else min(w["remaining_percent"] for w in windows)
        min_w = min(windows, key=lambda w: w["remaining_percent"])
        resets_at: float | None = min_w.get("resets_at") or quota_state["cooldown_until"]
        quota_source = "cooldown" if in_cooldown else "cache"
    elif in_cooldown or spent_buckets:
        rem = 0.0 if in_cooldown else None
        resets_at = quota_state["cooldown_until"] or next(iter(spent_buckets.values()), None)
        quota_source = "cooldown"
    else:
        rem = None
        resets_at = None
        quota_source = "local" if logged_in else "unavailable"

    plan_name = cached.get("plan")
    plan_detail = (
        f"{plan_name} · {tier}"
        if plan_name and tier != "unknown"
        else (str(plan_name) if plan_name else (tier if tier != "unknown" else "—"))
    )

    tokens = empty_tokens()
    agy_stats = scan_agy_token_stats(base)
    if agy_stats:
        tokens.update(agy_stats)

    return AccountTelemetry(
        provider="agy",
        profile=name,
        path=str(path),
        locked=locked,
        active=active,
        logged_in=logged_in,
        account_label=account_label,
        plan=plan_name or tier,
        plan_detail=plan_detail,
        plan_expires_at=None,
        plan_expires_iso=None,
        plan_expired=False,
        tier=tier,
        quota_source=quota_source,
        checked_at=cached.get("checked_at"),
        in_cooldown=in_cooldown,
        spent_buckets=spent_buckets,
        remaining_percent=rem,
        resets_at=resets_at,
        windows=windows,
        model_quotas=model_quotas,
        reset_credits=0,
        models=models,
        locked_models=[],
        models_source=models_source,
        models_cached=bool(slugs or live_models_map),
        tokens=tokens,
    ).to_dict()


def _detect_agy_user_agent(hub: Hub) -> str:
    spec = hub.harness("agy")
    exe = spec.executable()
    if not exe:
        return "antigravity/1.5.16"
    base_cmd = list(spec.launcher) if spec.launcher else [exe]
    try:
        mtime_ns = Path(exe).stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    cache_key = (exe, mtime_ns)
    cached = _AGY_VERSION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    version = "1.5.16"
    try:
        completed = subprocess.run(
            [*base_cmd, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        match = re.search(
            r"(\d+\.\d+\.\d+)", (completed.stdout or "") + " " + (completed.stderr or "")
        )
        if match:
            version = match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    ua = f"antigravity/{version}"
    _AGY_VERSION_CACHE.set(cache_key, ua)
    return ua


def agy_acp_probe(
    hub: Hub, profile: dict[str, Any], env: dict[str, str], timeout: float
) -> dict[str, Any]:
    spec = hub.harness("agy")
    executable = spec.executable()
    if executable is None:
        raise HubError("AGY не найден в PATH")
    base_cmd = list(spec.launcher) if spec.launcher else [executable]

    models: list[dict[str, Any]] = []
    tier_info: dict[str, Any] = {}
    try:
        with JsonRpcStdioClient([*base_cmd, "acp", "--stdio"], env=env) as client:
            init = client.request(
                "initialize",
                {"protocolVersion": 1, "clientCapabilities": {}},
                req_id=1,
                timeout=min(timeout, 12.0),
                optional=True,
            )
            if init:
                snew = client.request(
                    "session/new",
                    {"cwd": str(Path.cwd()), "mcpServers": []},
                    req_id=2,
                    timeout=min(timeout, 12.0),
                    optional=True,
                )
                raw_models = (snew.get("models") or {}).get("availableModels") or []
                for item in raw_models:
                    if isinstance(item, dict) and isinstance(item.get("modelId"), str):
                        models.append(
                            {
                                "id": item["modelId"],
                                "name": item.get("name")
                                or AGY_MODEL_LABELS.get(item["modelId"], item["modelId"]),
                                "provider": "agy",
                                "family": model_family(item["modelId"]),
                                "context_window": None,
                                "effort": None,
                                "hidden": False,
                            }
                        )
                tier_info = ((snew.get("_meta") or {}).get("antigravity") or {}).get("tier") or {}
    except OSError:
        pass

    if not models:
        try:
            completed = subprocess.run(
                [*base_cmd, "models"],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            slugs = sorted(
                {
                    line.split()[0]
                    for line in completed.stdout.replace("\r", "\n").splitlines()
                    if line.strip() and line.lstrip().startswith(("gemini-", "claude-", "gpt-"))
                }
            )
            models = [
                {
                    "id": s,
                    "name": AGY_MODEL_LABELS.get(s, s),
                    "provider": "agy",
                    "family": model_family(s),
                    "context_window": None,
                    "effort": None,
                    "hidden": False,
                }
                for s in slugs
            ]
        except subprocess.TimeoutExpired as exc:
            raise HubError(f"agy models не ответил за {timeout:g} секунд") from exc
        except OSError as exc:
            raise HubError(f"Не удалось запустить agy models: {exc}") from exc

    return {"models": models, "tier": tier_info}


def agy_http_opener(hub: Hub, profile: dict[str, Any]) -> urllib.request.OpenerDirector:
    policy, _ = hub.proxy_policy("agy", profile)
    mode = policy.get("mode")
    if mode == "url" and policy.get("url"):
        handler = urllib.request.ProxyHandler({"http": policy["url"], "https": policy["url"]})
        return urllib.request.build_opener(handler)
    if mode == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    env_proxies: dict[str, str] = {}
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        if os.environ.get(k):
            scheme = "https" if "https" in k.lower() else "http"
            env_proxies[scheme] = os.environ[k]
    if env_proxies:
        return urllib.request.build_opener(urllib.request.ProxyHandler(env_proxies))
    return urllib.request.build_opener()


def agy_cloudcode_post(
    opener: urllib.request.OpenerDirector,
    access_token: str,
    endpoint: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    req = urllib.request.Request(
        f"https://daily-cloudcode-pa.googleapis.com/v1internal:{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": _AGY_USER_AGENT_CTX.get(),
        },
        method="POST",
    )
    with opener.open(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}


def probe_agy_telemetry(hub: Hub, profile_name: str, timeout: float = 20.0) -> dict[str, Any]:
    import harnesshub.telemetry as telemetry_mod

    profile = hub.profile("agy", profile_name)
    path = Path(profile["path"])
    env = hub.environment("agy", profile)

    acp_data = telemetry_mod._agy_acp_probe(hub, profile, env, timeout=timeout)
    models: list[dict[str, Any]] = acp_data["models"]
    if not models:
        raise HubError("Сервер AGY не вернул список моделей (проверь вход и прокси)")

    slugs = sorted({m["id"] for m in models})
    atomic_write_text(path / ".models", "\n".join(slugs) + "\n", mode=0o600)

    # Re-read token after ACP probe (ACP refreshes access_token if expired)
    _, _, access_token = read_agy_token_meta(path)
    tier_info = acp_data.get("tier") or {}
    plan_name: str | None = tier_info.get("name") if isinstance(tier_info, dict) else None
    windows: list[dict[str, Any]] = []
    model_quotas: dict[str, dict[str, Any]] = {}

    if access_token:
        ua_token = _AGY_USER_AGENT_CTX.set(_detect_agy_user_agent(hub))
        try:
            opener = agy_http_opener(hub, profile)
            lca = telemetry_mod._agy_cloudcode_post(opener, access_token, "loadCodeAssist", {})
            if isinstance(lca, dict):
                paid = lca.get("paidTier") or {}
                cur = lca.get("currentTier") or {}
                plan_name = paid.get("name") or cur.get("name") or plan_name
                proj = lca.get("cloudaicompanionProject")
            else:
                proj = None

            summary = telemetry_mod._agy_cloudcode_post(
                opener, access_token, "retrieveUserQuotaSummary", {}
            )
            if isinstance(summary, dict):
                for group in summary.get("groups") or []:
                    if not isinstance(group, dict):
                        continue
                    gname = group.get("displayName") or ""
                    for bucket in group.get("buckets") or []:
                        if not isinstance(bucket, dict):
                            continue
                        rf = bucket.get("remainingFraction")
                        if not isinstance(rf, (int, float)):
                            continue
                        rem = round(max(0.0, min(100.0, float(rf) * 100.0)), 1)
                        reset_ep = iso_to_epoch(bucket.get("resetTime"))
                        bid = str(bucket.get("bucketId") or bucket.get("window") or "quota")
                        wtype = str(bucket.get("window") or bid)
                        mins = 300 if wtype == "5h" else (10080 if wtype == "weekly" else None)
                        windows.append(
                            {
                                "window": bid,
                                "label": f"{gname} ({wtype})" if gname else wtype,
                                "remaining_percent": rem,
                                "used_percent": round(100.0 - rem, 1),
                                "minutes": mins,
                                "resets_at": int(reset_ep) if reset_ep else None,
                            }
                        )

            ruq_payload = {"project": proj} if proj else {}
            ruq = telemetry_mod._agy_cloudcode_post(
                opener, access_token, "retrieveUserQuota", ruq_payload
            )
            gemini_pool_rem: float | None = None
            gemini_pool_reset: int | None = None
            if isinstance(ruq, dict):
                for b in ruq.get("buckets") or []:
                    if not isinstance(b, dict):
                        continue
                    mid = b.get("modelId")
                    rf = b.get("remainingFraction")
                    if not isinstance(mid, str) or not isinstance(rf, (int, float)):
                        continue
                    rem = round(max(0.0, min(100.0, float(rf) * 100.0)), 1)
                    rep = iso_to_epoch(b.get("resetTime"))
                    model_quotas[mid] = {
                        "remaining_percent": rem,
                        "resets_at": int(rep) if rep else None,
                    }
                    if mid in AGY_POOL_MODELS:
                        gemini_pool_rem = (
                            rem if gemini_pool_rem is None else min(gemini_pool_rem, rem)
                        )
                        if rep:
                            gemini_pool_reset = int(rep)
            if gemini_pool_rem is not None:
                windows.append(
                    {
                        "window": "gemini",
                        "label": "Gemini пул",
                        "remaining_percent": gemini_pool_rem,
                        "used_percent": round(100.0 - gemini_pool_rem, 1),
                        "minutes": None,
                        "resets_at": gemini_pool_reset,
                    }
                )
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
            pass
        finally:
            _AGY_USER_AGENT_CTX.reset(ua_token)

    now = time.time()
    payload = {
        "provider": "agy",
        "profile": profile_name,
        "source": "live",
        "checked_at": now,
        "plan": plan_name,
        "tier": classify_agy_tier(slugs),
        "models": models,
        "windows": windows,
        "model_quotas": model_quotas,
    }
    try:
        atomic_write_json(hub.store.root / "quotas" / f"agy-telemetry.{profile_name}.json", payload)
    except OSError:
        pass

    result = build_agy_telemetry(hub, profile, now)
    result["quota_source"] = "live" if (windows or model_quotas) else result["quota_source"]
    result["source"] = "live"
    return result


def read_agy_history(
    history_file: Path,
    history_cache: BoundedCache[str, tuple[int, int, dict[str, dict[str, Any]]]],
) -> dict[str, dict[str, Any]]:
    if not history_file.is_file():
        return {}
    try:
        st = history_file.stat()
    except OSError:
        return {}
    key = os.path.realpath(history_file)
    cached = history_cache.get(key)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]

    history_map: dict[str, dict[str, Any]] = {}
    try:
        with history_file.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = item.get("conversationId")
                if not isinstance(cid, str) or not cid:
                    continue
                entry = history_map.setdefault(
                    cid, {"workspace": item.get("workspace"), "ts": 0.0, "first_display": ""}
                )
                if item.get("workspace"):
                    entry["workspace"] = item["workspace"]
                ts = iso_to_epoch_or_zero(item.get("timestamp"))
                if ts > entry["ts"]:
                    entry["ts"] = ts
                disp = str(item.get("display") or "").strip()
                if disp and not disp.startswith("/") and not entry["first_display"]:
                    entry["first_display"] = disp
    except OSError:
        return {}
    history_cache.set(key, (st.st_mtime_ns, st.st_size, history_map))
    return history_map


def read_agy_conv_metadata(
    meta_file: Path,
    conv_meta_cache: BoundedCache[str, tuple[int, int, dict[str, dict[str, Any]]]],
) -> dict[str, dict[str, Any]]:
    if not meta_file.is_file():
        return {}
    try:
        st = meta_file.stat()
    except OSError:
        return {}
    key = os.path.realpath(meta_file)
    cached = conv_meta_cache.get(key)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]

    result: dict[str, dict[str, Any]] = {}
    try:
        payload = json.loads(meta_file.read_text(encoding="utf-8", errors="replace"))
        convs = payload.get("conversations") if isinstance(payload, dict) else None
        if isinstance(convs, dict):
            for cid, item in convs.items():
                if not isinstance(item, dict):
                    continue
                summary = item.get("summary") or {}
                if not isinstance(summary, dict):
                    continue
                uris = summary.get("WorkspaceURIs") or []
                ws = strip_file_uri(str(uris[0])) if uris else ""
                title = str(summary.get("Title") or "").strip()
                preview = str(summary.get("Preview") or "").strip()
                num_steps = summary.get("NumSteps")
                ts = iso_to_epoch_or_zero(
                    summary.get("UpdatedAt") or item.get("last_modified_time")
                )
                result[cid] = {
                    "title": title,
                    "preview": preview,
                    "workspace": ws,
                    "steps": int(num_steps) if isinstance(num_steps, int) else 0,
                    "ts": ts,
                }
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    conv_meta_cache.set(key, (st.st_mtime_ns, st.st_size, result))
    return result


def parse_agy_session(
    profile_name: str,
    cid: str,
    transcript: Path,
    st_mtime_ns: int,
    st_size: int,
    st_mtime: float,
    meta: dict[str, Any],
    conv_meta: dict[str, Any] | None,
    *,
    clean_user_text: Any,
    meta_cache: BoundedCache[str, tuple[int, int, dict[str, Any] | None]],
) -> dict[str, Any] | None:
    conv_meta = conv_meta or {}
    cache_key = os.path.realpath(transcript)
    cached = meta_cache.get(cache_key)
    if cached and cached[0] == st_mtime_ns and cached[1] == st_size:
        if cached[2] is None:
            return None
        entry = dict(cached[2])
        entry["profile"] = profile_name
        ws = meta.get("workspace") or conv_meta.get("workspace") or ""
        if ws and not entry.get("cwd"):
            entry["cwd"] = ws
            entry["project_name"] = project_name(ws)
        if conv_meta.get("title") and (not entry.get("title") or entry["title"] == cid):
            entry["title"] = shorten(conv_meta["title"])
        return entry

    first_user = (
        meta.get("first_display") or conv_meta.get("title") or conv_meta.get("preview") or ""
    )
    last_ts = max(st_mtime, meta.get("ts") or 0.0, conv_meta.get("ts") or 0.0)
    model = "gemini-4"
    tokens_total = 0
    has_tokens = False
    turn_count = 0

    def _consume(lines: list[str], *, count_turns: bool, skip_large: bool) -> None:
        nonlocal first_user, last_ts, model, tokens_total, has_tokens, turn_count
        for line in lines:
            if skip_large and len(line) > 32768:
                continue
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = iso_to_epoch_or_zero(row.get("created_at"))
            if ts > last_ts:
                last_ts = ts
            rtype = row.get("type")
            if rtype == "USER_INPUT":
                if count_turns:
                    turn_count += 1
                if not first_user:
                    first_user = clean_user_text(str(row.get("content") or ""))
            elif rtype == "PLANNER_RESPONSE":
                if isinstance(row.get("model"), str) and row["model"]:
                    model = row["model"]
                inp = int(row.get("input_tokens") or 0)
                out = int(row.get("output_tokens") or 0)
                if inp or out:
                    tokens_total += inp + out
                    has_tokens = True

    if st_size <= MAX_FULL_TRANSCRIPT_BYTES:
        try:
            with transcript.open("r", encoding="utf-8", errors="replace") as stream:
                _consume(list(stream), count_turns=True, skip_large=False)
        except OSError:
            pass
    else:
        head_lines, turn_count = read_head_and_count(
            transcript,
            (b'"USER_INPUT"',),
            st_size,
            head_bytes=65536,
            max_lines=40,
            scan_bytes=196608,
        )
        _consume(head_lines, count_turns=False, skip_large=True)
        has_tokens = False
        tokens_total = 0

    if first_user and turn_count == 0:
        turn_count = max(1, conv_meta.get("steps") or 1)
    if not first_user and turn_count == 0:
        meta_cache.set(cache_key, (st_mtime_ns, st_size, None))
        return None

    cwd = meta.get("workspace") or conv_meta.get("workspace") or ""
    title_text = conv_meta.get("title") or first_user or cid
    result = SessionSummary(
        session_id=cid,
        provider="agy",
        profile=profile_name,
        cwd=cwd,
        project_name=project_name(cwd),
        model=model,
        title=shorten(title_text),
        turns=turn_count,
        tokens_used=tokens_total if has_tokens else None,
        updated_at=last_ts,
        updated_at_iso=epoch_to_iso(last_ts),
        transcript_path=str(transcript),
    ).to_dict()
    meta_cache.set(cache_key, (st_mtime_ns, st_size, dict(result)))
    return result


def collect_agy_candidates(
    profile_name: str,
    profile_path: Path,
    target_cwd: str | None = None,
    *,
    history_cache: BoundedCache[str, tuple[int, int, dict[str, dict[str, Any]]]],
    conv_meta_cache: BoundedCache[str, tuple[int, int, dict[str, dict[str, Any]]]],
) -> list[tuple[Any, ...]]:
    base = agy_base_dir(profile_path)
    history_map = read_agy_history(base / "history.jsonl", history_cache)
    conv_meta_map = read_agy_conv_metadata(
        base / "cache" / "conversation_metadata.json", conv_meta_cache
    )
    brain_dir = base / "brain"
    if not brain_dir.is_dir():
        return []
    try:
        conv_dirs = list(brain_dir.iterdir())
    except OSError:
        return []

    candidates: list[tuple[Any, ...]] = []
    for conv_dir in conv_dirs:
        if not conv_dir.is_dir():
            continue
        cid = conv_dir.name
        meta = history_map.get(cid, {})
        cmeta = conv_meta_map.get(cid, {})
        if target_cwd is not None:
            ws = meta.get("workspace") or cmeta.get("workspace") or ""
            if ws and not cwd_matches(ws, target_cwd):
                continue
        transcript = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
        try:
            st = transcript.stat()
        except OSError:
            continue
        candidates.append(
            (
                st.st_mtime_ns,
                st.st_size,
                st.st_mtime,
                "agy",
                profile_name,
                (cid, transcript, meta, cmeta),
            )
        )
    return candidates


def parse_agy_messages(transcript_path: Path, clean_user_text: Any) -> list[dict[str, Any]]:
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
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            if rtype == "USER_INPUT":
                messages.append(
                    {
                        "role": "user",
                        "text": clean_user_text(content),
                        "timestamp": row.get("created_at") or "",
                    }
                )
            elif row.get("source") == "MODEL" and rtype in ("GENERIC", "PLANNER_RESPONSE"):
                messages.append(
                    {
                        "role": "assistant",
                        "text": content,
                        "timestamp": row.get("created_at") or "",
                    }
                )
    return messages


class AgyAdapter:
    provider_id = "agy"

    def build_telemetry(self, hub: Hub, profile: dict[str, Any], now: float) -> dict[str, Any]:
        return build_agy_telemetry(hub, profile, now)

    def probe_telemetry(self, hub: Hub, profile_name: str, timeout: float = 20.0) -> dict[str, Any]:
        return probe_agy_telemetry(hub, profile_name, timeout=timeout)

    def parse_messages(self, transcript_path: Path) -> list[dict[str, Any]]:
        from harnesshub.sessions import _clean_user_text

        return parse_agy_messages(transcript_path, _clean_user_text)
