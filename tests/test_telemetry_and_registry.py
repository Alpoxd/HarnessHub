from __future__ import annotations

import base64
import json
import sqlite3
import time
from pathlib import Path

import pytest
from textual.widgets import DataTable, Select, Static, TabbedContent

from harnesshub.cli import main
from harnesshub.models import catalog_models, normalize_model, rank_candidates
from harnesshub.service import Hub
from harnesshub.sessions import list_sessions
from harnesshub.telemetry import account_telemetry, probe_agy_telemetry
from harnesshub.tui import HarnessHubApp


def _fake_jwt(claims: dict) -> str:
    hdr = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{hdr}.{body}.sig"


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path / "hub", lock_root=tmp_path / "locks")


def test_codex_models_cache_is_strict_and_never_mixes_other_harnesses(hub, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    codex_prof = hub.create("codex", "free-main")
    cpath = Path(codex_prof["path"])

    future_ts = int(time.time()) + 86400 * 10
    past_iso = "2026-11-15T10:00:06+00:00"
    jwt = _fake_jwt(
        {
            "email": "user@example.com",
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
                "chatgpt_subscription_active_until": past_iso,
            },
        }
    )
    (cpath / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": jwt}}),
        encoding="utf-8",
    )
    # Only gpt-6-luna visible, gpt-reserve hidden, NO gpt-6.1-sol!
    (cpath / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-6-luna",
                        "display_name": "GPT-6-Luna",
                        "visibility": "list",
                        "context_window": 272000,
                    },
                    {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide"},
                ]
            }
        ),
        encoding="utf-8",
    )

    # SQLite token stats
    db_path = cpath / "state_5.sqlite"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE threads ("
        "id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER, updated_at_ms INTEGER, "
        "cwd TEXT, title TEXT, name TEXT, first_user_message TEXT, tokens_used INTEGER, "
        "model TEXT, thread_source TEXT, archived INTEGER DEFAULT 0)"
    )
    sess_dir = cpath / "sessions" / "2026" / "11" / "24"
    sess_dir.mkdir(parents=True)
    rollout = sess_dir / "rollout-1.jsonl"
    rollout.write_text('{"type":"session_meta","payload":{"id":"t1"}}\n', encoding="utf-8")
    con.execute(
        "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "t1",
            str(rollout),
            int(time.time()),
            int(time.time() * 1000),
            str(cpath),
            "Тестовый заголовок",
            "Красивое имя чата",
            "Привет",
            123456,
            "gpt-6-luna",
            "cli",
            0,
        ),
    )
    con.commit()
    con.close()

    # Cached quota from live/cache
    qdir = hub.store.root / "quotas"
    qdir.mkdir(parents=True)
    (qdir / "codex-usage.free-main.json").write_text(
        json.dumps(
            {
                "plan": "free",
                "windows": [
                    {
                        "window": "primary",
                        "remaining_percent": 82.5,
                        "minutes": 43200,
                        "resets_at": future_ts,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # Also register AGY with claude/gemini models to verify strict isolation
    agy_prof = hub.create("agy", "acc1")
    (Path(agy_prof["path"]) / ".models").write_text(
        "gemini-4-neon-high\nclaude-opus-5-5-high\ngpt-oss-120b-medium\n",
        encoding="utf-8",
    )

    tel = account_telemetry(hub, codex_prof)
    model_ids = [m["id"] for m in tel["models"]]
    assert model_ids == ["gpt-6-luna"]
    assert "gpt-reserve" not in model_ids
    assert "gpt-6.1-sol" not in model_ids
    assert tel["plan"] == "free"
    assert tel["plan_expired"] is True
    assert "2026-11-15" in tel["plan_detail"]
    assert tel["remaining_percent"] == 82.5
    assert tel["tokens"]["total_tokens"] == 123456
    assert tel["tokens"]["sessions_count"] == 1
    assert tel["account_label"] == "user@example.com"

    codex_catalog = catalog_models(hub, provider="codex")
    assert [c["model"] for c in codex_catalog] == ["gpt-6-luna"]
    assert all("claude" not in c["model"] and "gemini" not in c["model"] for c in codex_catalog)

    # Proprietary gpt-6-astra must NOT match AGY when Codex doesn't have it
    assert rank_candidates(hub, "gpt-6-astra") == []
    # gpt-oss must match AGY, not Codex
    oss_ranked = rank_candidates(hub, "gpt-oss")
    assert len(oss_ranked) == 1
    assert oss_ranked[0]["provider"] == "agy"


def test_claude_catalog_credentials_and_stats_cache(hub):
    claude_prof = hub.create("claude", "work")
    cpath = Path(claude_prof["path"])
    future_ms = int((time.time() + 86400 * 20) * 1000)
    (cpath / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "subscriptionType": "pro",
                    "rateLimitTier": "default_claude_ai",
                    "expiresAt": future_ms,
                }
            }
        ),
        encoding="utf-8",
    )
    cat_dir = cpath / "cache" / "model-catalog"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalog.json").write_text(
        json.dumps(
            {
                "catalog": {
                    "config": {
                        "models": [
                            {
                                "id": "claude-sonnet-5-5",
                                "name": "Sonnet 5.5",
                                "runtime": {
                                    "max_input_tokens": 1000000,
                                    "default_effort": "medium",
                                },
                            },
                            {
                                "id": "claude-fable-5-2",
                                "name": "Fable 5.2",
                                "disabled": True,
                                "badge": {"message": "Pro, Max, or Ultra"},
                            },
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (cpath / "stats-cache.json").write_text(
        json.dumps(
            {
                "totalSessions": 12,
                "totalMessages": 340,
                "dailyModelTokens": [
                    {"date": "2026-11-23", "tokensByModel": {"claude-sonnet-5-5": 15000}},
                    {"date": "2026-11-24", "tokensByModel": {"claude-sonnet-5-5": 25000}},
                ],
                "modelUsage": {
                    "claude-sonnet-5-5": {
                        "inputTokens": 1000,
                        "outputTokens": 2000,
                        "cacheReadInputTokens": 30000,
                        "cacheCreationInputTokens": 7000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    tel = account_telemetry(hub, claude_prof)
    assert [m["id"] for m in tel["models"]] == ["claude-sonnet-5-5"]
    assert [m["id"] for m in tel["locked_models"]] == ["claude-fable-5-2"]
    assert tel["plan"] == "pro"
    assert tel["plan_expires_iso"] is not None
    assert "PRO" in tel["plan_detail"]
    assert tel["tokens"]["total_tokens"] == 40000
    assert tel["tokens"]["recent_7d_tokens"] == 40000
    assert tel["tokens"]["peak_daily_tokens"] == 25000
    assert tel["tokens"]["sessions_count"] == 12


def test_agy_live_quota_percentages_and_transcript_tokens(hub, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    agy_prof = hub.create("agy", "acc1")
    apath = Path(agy_prof["path"])
    base = apath / ".gemini" / "antigravity-cli"
    (base / "antigravity-oauth-token").write_text(
        json.dumps({"id_token_email": "acc1@gmail.com", "token": {"access_token": "tok-test"}}),
        encoding="utf-8",
    )

    # Transcript with token usage
    tr_dir = base / "brain" / "conv-1" / ".system_generated" / "logs"
    tr_dir.mkdir(parents=True)
    (tr_dir / "transcript.jsonl").write_text(
        json.dumps(
            {
                "type": "USER_INPUT",
                "created_at": "2026-11-24T12:00:00Z",
                "content": "Проверь квоты",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-24T12:00:10Z",
                "model": "gemini-4-neon-high",
                "input_tokens": 5000,
                "output_tokens": 1200,
                "cache_read_tokens": 4000,
                "content": "Готово",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "harnesshub.telemetry._agy_acp_probe",
        lambda hub, prof, env, timeout=20.0: {
            "models": [
                {
                    "id": "gemini-4-neon-high",
                    "name": "Gemini 4 Neon (High)",
                    "provider": "agy",
                    "family": "gemini",
                    "context_window": None,
                    "effort": None,
                    "hidden": False,
                },
                {
                    "id": "claude-opus-5-5-high",
                    "name": "Claude Opus 5.5 (High)",
                    "provider": "agy",
                    "family": "claude",
                    "context_window": None,
                    "effort": None,
                    "hidden": False,
                },
            ],
            "tier": {"name": "Google AI Pro", "id": "g1-pro-tier"},
        },
    )

    def fake_cc_post(opener, access_token, endpoint, payload):
        if endpoint == "loadCodeAssist":
            return {
                "cloudaicompanionProject": "proj-1",
                "paidTier": {"name": "Google AI Pro"},
            }
        if endpoint == "retrieveUserQuotaSummary":
            return {
                "groups": [
                    {
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {
                                "bucketId": "3p-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.2871408,
                                "resetTime": "2026-11-25T11:17:06Z",
                            }
                        ],
                    }
                ]
            }
        if endpoint == "retrieveUserQuota":
            return {
                "buckets": [
                    {
                        "modelId": "claude-opus-5-5-high",
                        "remainingFraction": 0.2871408,
                        "resetTime": "2026-11-25T11:17:06Z",
                    },
                    {
                        "modelId": "gemini-4-hydrogen-sonar",
                        "remainingFraction": 0.425,
                        "resetTime": "2026-11-24T17:00:00Z",
                    },
                ]
            }
        return {}

    monkeypatch.setattr("harnesshub.telemetry._agy_cloudcode_post", fake_cc_post)

    tel = probe_agy_telemetry(hub, "acc1")
    assert tel["plan"] == "Google AI Pro"
    assert tel["tier"] == "g4"
    assert tel["remaining_percent"] == 28.7
    assert tel["model_quotas"]["claude-opus-5-5-high"]["remaining_percent"] == 28.7
    assert tel["tokens"]["total_tokens"] == 6200
    assert tel["tokens"]["by_model"]["gemini-4-neon-high"] == 6200
    assert normalize_model("neon") == "gemini-4-neon-high"
    assert normalize_model("sol-6.2") == "gpt-6.2-sol"

    ranked_opus = rank_candidates(hub, "opus")
    assert ranked_opus[0]["remaining_percent"] == 28.7
    ranked_neon = rank_candidates(hub, "neon")
    assert ranked_neon[0]["remaining_percent"] == 42.5


def test_codex_sqlite_sessions_dedup_and_search(hub, tmp_path):
    p1 = hub.create("codex", "main")
    p2 = hub.create("codex", "acc2")
    proj = tmp_path / "repo"
    proj.mkdir()

    shared_sessions = Path(p1["path"]) / "sessions" / "2026" / "11" / "24"
    shared_sessions.mkdir(parents=True)
    rollout = shared_sessions / "rollout-shared.jsonl"
    rollout.write_text('{"type":"session_meta","payload":{"id":"s-shared"}}\n', encoding="utf-8")
    (Path(p2["path"]) / "sessions").symlink_to(Path(p1["path"]) / "sessions")

    for prof in (p1, p2):
        con = sqlite3.connect(Path(prof["path"]) / "state_5.sqlite")
        con.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER, updated_at_ms INTEGER, "
            "cwd TEXT, title TEXT, name TEXT, first_user_message TEXT, tokens_used INTEGER, "
            "model TEXT, thread_source TEXT, archived INTEGER DEFAULT 0)"
        )
        con.execute(
            "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "s-shared",
                str(rollout),
                int(time.time()),
                int(time.time() * 1000),
                str(proj),
                "Raw prompt",
                "Рефакторинг реестра",
                "Первое сообщение",
                98765,
                "gpt-6.2-sol",
                "cli",
                0,
            ),
        )
        con.commit()
        con.close()

    sessions = list_sessions(hub, cwd=proj)
    assert len(sessions) == 1
    assert sessions[0]["title"] == "Рефакторинг реестра"
    assert sessions[0]["tokens_used"] == 98765
    assert sessions[0]["profile"] == "main"
    assert sessions[0]["project_name"] == "repo"
    assert list_sessions(hub, cwd=proj, query="реестра")[0]["session_id"] == "s-shared"
    assert list_sessions(hub, cwd=proj, query="не-существует") == []


async def test_tui_separated_tabs_account_inspector_and_chat_preview(tmp_path, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    codex_prof = hub.create("codex", "acc2")
    agy_prof = hub.create("agy", "acc1")

    (Path(codex_prof["path"]) / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-6.2-sol", "display_name": "GPT-6.2-Sol", "visibility": "list"}
                ]
            }
        ),
        encoding="utf-8",
    )
    (Path(agy_prof["path"]) / ".models").write_text("gemini-4-neon-high\n", encoding="utf-8")

    # Add a Codex session
    cx_dir = Path(codex_prof["path"]) / "sessions" / "2026" / "11" / "24"
    cx_dir.mkdir(parents=True)
    (cx_dir / "rollout-ui.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-11-24T14:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "sess-ui-1", "cwd": str(Path.cwd())},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-11-24T14:00:05Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Покажи превью переписки"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    app = HarnessHubApp(hub, "codex")
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        # Account inspector shows ONLY Codex model, never Gemini/Claude
        insp_text = str(app.query_one("#account-inspector", Static).render())
        assert "gpt-6.2-sol" in insp_text
        assert "gemini-4-neon-high" not in insp_text

        # Model registry tab filters strictly by provider
        app.query_one(TabbedContent).active = "models-tab"
        await pilot.pause()
        app.query_one("#registry-provider-filter", Select).value = "codex"
        await pilot.pause()
        assert [r["model"] for r in app.registry_rows] == ["gpt-6.2-sol"]

        # Chat history tab renders table & preview pane
        app.query_one(TabbedContent).active = "sessions-tab"
        await pilot.pause()
        assert app.query_one("#sessions-table", DataTable).row_count == 1
        preview_text = str(app.query_one("#session-preview", Static).render())
        assert "Покажи превью переписки" in preview_text


def test_cli_accounts_and_models_filters(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    base = ["--data-dir", str(tmp_path / "hub"), "--json"]
    assert main(base + ["profiles", "new", "codex", "acc2"]) == 0
    capsys.readouterr()

    assert main(base + ["accounts", "codex"]) == 0
    accounts = json.loads(capsys.readouterr().out)
    assert len(accounts) == 1
    assert accounts[0]["provider"] == "codex"

    assert main(base + ["models", "list", "-p", "codex", "--family", "gpt", "-q", "6.2"]) == 0
    models = json.loads(capsys.readouterr().out)
    assert any(m["model"] == "gpt-6.2-sol" for m in models)
    assert all(m["providers"] == ["codex"] for m in models)
