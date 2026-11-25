from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Select, TabbedContent

from harnesshub.cli import main
from harnesshub.consult import consult_peers
from harnesshub.errors import HubError
from harnesshub.ide import launch_ide
from harnesshub.locking import FileLock, resource_lock
from harnesshub.mcp import serve_mcp
from harnesshub.models import catalog_models, mark_quota_cooldown, normalize_model, rank_candidates
from harnesshub.service import Hub
from harnesshub.sessions import build_handoff_prompt, list_sessions, read_session
from harnesshub.tui import ConsultForm, HandoffForm, HarnessHubApp
from harnesshub.usage import read_usage


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path / "hub", lock_root=tmp_path / "locks")


def test_model_first_ranking_respects_tiers_cooldowns_locks_and_spare_slots(
    hub, tmp_path, monkeypatch
):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    acc1 = hub.create("agy", "acc1")
    acc2 = hub.create("agy", "acc2")
    acc3 = hub.create("agy", "acc3")

    (Path(acc1["path"]) / ".models").write_text(
        "gemini-4-argon-high\ngemini-4-hydrogen-high\nclaude-opus-5-5-high\ngemini-3.8-flash-high\n",
        encoding="utf-8",
    )
    (Path(acc2["path"]) / ".models").write_text(
        "gemini-3.8-flash-high\nclaude-sonnet-4-6\n",
        encoding="utf-8",
    )
    (Path(acc3["path"]) / ".models").write_text(
        "gemini-4-argon-high\ngemini-4-hydrogen-high\nclaude-opus-5-5-high\ngemini-3.8-flash-high\n",
        encoding="utf-8",
    )
    for prof in (acc1, acc2, acc3):
        tok = Path(prof["path"]) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        tok.parent.mkdir(parents=True, exist_ok=True)
        tok.write_text("token", encoding="utf-8")

    # 1. Requesting argon ranks acc1 & acc3 ahead of acc2 (which only has flash substitute)
    ranked_argon = rank_candidates(hub, "argon")
    assert [r["profile"] for r in ranked_argon[:2]] == ["acc1", "acc3"]
    assert ranked_argon[0]["exact_match"] is True
    assert ranked_argon[2]["profile"] == "acc2"
    assert ranked_argon[2]["resolved_model"] == "gemini-3.8-flash-high"

    # 2. Marking acc1 argon quota spent moves acc3 to #1
    mark_quota_cooldown(hub, "agy", "acc1", "argon", duration_seconds=3600)
    ranked_after_cd = rank_candidates(hub, "argon")
    assert ranked_after_cd[0]["profile"] == "acc3"
    assert ranked_after_cd[0]["quota_ready"] is True

    # 3. Requesting flash prefers acc2 (3.8+4.6 tier) to keep g4+5.5 slots (acc1/acc3) spare
    ranked_flash = rank_candidates(hub, "flash")
    assert ranked_flash[0]["profile"] == "acc2"

    # 4. Locking acc3 while acc1 is in cooldown falls back or shifts order
    with FileLock(resource_lock(Path(acc3["path"]), hub.lock_root)):
        ranked_locked = rank_candidates(hub, "argon")
        assert ranked_locked[0]["locked"] is False
        assert ranked_locked[-1]["profile"] == "acc3"
        assert ranked_locked[-1]["locked"] is True


async def test_agy_and_claude_usage_reporting(hub, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    acc1 = hub.create("agy", "acc1")
    (Path(acc1["path"]) / ".models").write_text(
        "gemini-4-argon-high\ngemini-4-hydrogen-high\nclaude-opus-5-5-high\n",
        encoding="utf-8",
    )
    mark_quota_cooldown(hub, "agy", "acc1", "argon", duration_seconds=1800)
    usage = await read_usage(hub, "agy", "acc1")
    assert usage["plan"] == "g4+5.5"
    by_window = {w["window"]: w["remaining_percent"] for w in usage["windows"]}
    assert by_window == {"argon": 0}

    hub.create("claude", "work")
    c_usage = await read_usage(hub, "claude", "work")
    assert c_usage["windows"] == []
    mark_quota_cooldown(hub, "claude", "work", duration_seconds=1800)
    c_usage_cd = await read_usage(hub, "claude", "work")
    assert c_usage_cd["windows"][0]["remaining_percent"] == 0


def test_unified_sessions_and_cross_harness_handoff(hub, tmp_path, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()

    # 1. Create an AGY profile with a session transcript
    agy_prof = hub.create("agy", "acc1")
    agy_base = Path(agy_prof["path"]) / ".gemini" / "antigravity-cli"
    (agy_base / "history.jsonl").write_text(
        json.dumps(
            {
                "display": "Спроектируй единый роутер квот",
                "timestamp": 1795521459530,
                "workspace": str(project_dir),
                "conversationId": "agy-conv-123456",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    agy_log = agy_base / "brain" / "agy-conv-123456" / ".system_generated" / "logs"
    agy_log.mkdir(parents=True)
    (agy_log / "transcript.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step_index": 0,
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "created_at": "2026-11-24T12:00:00Z",
                        "content": (
                            "<USER_REQUEST>\nСпроектируй единый роутер квот\n</USER_REQUEST>"
                        ),
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "created_at": "2026-11-24T12:01:00Z",
                        "content": "Предлагаю модуль models.py с ранжированием слотов.",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # 2. Create a Claude profile with a session transcript
    claude_prof = hub.create("claude", "work")
    c_proj = Path(claude_prof["path"]) / "projects" / "-my-project"
    c_proj.mkdir(parents=True)
    (c_proj / "claude-sess-987654.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-11-24T13:00:00Z",
                        "cwd": str(project_dir),
                        "sessionId": "claude-sess-987654",
                        "message": {"role": "user", "content": "Проверь безопасность прокси"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-11-24T13:01:00Z",
                        "cwd": str(project_dir),
                        "sessionId": "claude-sess-987654",
                        "message": {
                            "model": "claude-opus-5-5",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Пароли прокси замаскированы."}],
                        },
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # 3. Create a Codex profile with a session transcript
    codex_prof = hub.create("codex", "main")
    cx_dir = Path(codex_prof["path"]) / "sessions" / "2026" / "11" / "24"
    cx_dir.mkdir(parents=True)
    (cx_dir / "rollout-codex-777.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-11-24T14:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "codex-sess-777",
                            "cwd": str(project_dir),
                            "base_instructions": {"provenance": {"model": "gpt-6.1-sol"}},
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-11-24T14:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "# AGENTS.md system block"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-11-24T14:00:05Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "Оптимизируй алгоритм блокировок"}
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-11-24T14:00:10Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Используем flock без удаления файла.",
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    sessions = list_sessions(hub, cwd=project_dir)
    assert len(sessions) == 3
    assert {s["provider"] for s in sessions} == {"agy", "claude", "codex"}

    detail = read_session(hub, "agy-conv-123456")
    assert len(detail["messages"]) == 2
    assert detail["messages"][0]["text"] == "Спроектируй единый роутер квот"

    _, handoff_prompt = build_handoff_prompt(
        hub, "agy-conv-123456", note="Перенеси логику в Codex и напиши тесты", cwd=project_dir
    )
    assert "Спроектируй единый роутер квот" in handoff_prompt
    assert "Предлагаю модуль models.py" in handoff_prompt
    assert "Перенеси логику в Codex и напиши тесты" in handoff_prompt

    plan = hub.plan_handoff(
        "agy-conv-123456",
        target_model="sol",
        note="Допиши тесты",
        cwd=project_dir,
    )
    assert plan["target_provider"] == "codex"
    assert plan["target_profile"] == "main"
    assert plan["target_model"] == "gpt-6.1-sol"
    assert plan["cli_args"][:2] == ["-m", "gpt-6.1-sol"]


def test_isolated_ide_plan_and_dry_run(hub, tmp_path, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    agy_prof = hub.create("agy", "acc1")
    claude_prof = hub.create("claude", "work")
    codex_prof = hub.create("codex", "main")
    hub.set_proxy({"mode": "url", "url": "http://127.0.0.1:10809"})

    res = launch_ide(
        hub,
        "agy",
        profile="acc1",
        claude_profile="work",
        codex_profile="main",
        cwd=tmp_path,
        dry_run=True,
    )
    assert res["status"] == "dry-run"
    assert res["ide"] == "agy"
    assert res["profiles"] == {"agy": "acc1", "claude": "work", "codex": "main"}
    assert "--user-data-dir" in res["command"]
    assert str(Path(agy_prof["path"]) / ".ide-data" / "agy") in res["command"]
    assert "--proxy-server" in res["command"]
    assert "http://127.0.0.1:10809" in res["command"]
    assert Path(claude_prof["path"]).is_dir() and Path(codex_prof["path"]).is_dir()


def test_peer_consultation_and_recursion_guard(hub, tmp_path, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub.create("codex", "main")
    hub.create("claude", "work")

    # Recursion guard blocks nested calls when AI_CONSULTATION=1
    monkeypatch.setenv("AI_CONSULTATION", "1")
    monkeypatch.delenv("HARNESSHUB_ALLOW_NESTED_CONSULT", raising=False)
    with pytest.raises(HubError, match="AI_CONSULTATION=1"):
        consult_peers(hub, "Проверь архитектуру", models=["sol"], cwd=tmp_path)

    # Dry-run works and selects peer targets across families
    dry = consult_peers(
        hub,
        "Проверь архитектуру",
        models=["sol", "opus"],
        cwd=tmp_path,
        dry_run=True,
    )
    assert len(dry["opinions"]) == 2
    assert {op["provider"] for op in dry["opinions"]} == {"codex", "claude"}
    assert all(op["status"] == "dry-run" for op in dry["opinions"])


def test_mcp_stdio_server_tools_and_protocol(hub, tmp_path, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub.create("codex", "main")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "list_models_and_quotas",
                "arguments": {"model": "sol"},
            },
        },
    ]
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    stdout = io.StringIO()
    assert serve_mcp(hub, stdin=stdin, stdout=stdout) == 0

    responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(responses) == 3
    assert responses[0]["id"] == 1
    assert responses[0]["result"]["serverInfo"]["name"] == "harnesshub"
    tool_names = {t["name"] for t in responses[1]["result"]["tools"]}
    assert {
        "consult_peer",
        "consult_council",
        "list_models_and_quotas",
        "list_project_sessions",
    } <= tool_names
    ranked = json.loads(responses[2]["result"]["content"][0]["text"])
    assert ranked[0]["provider"] == "codex"
    assert ranked[0]["resolved_model"] == "gpt-6.1-sol"


def test_cli_new_subcommands_json_output(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    base = ["--data-dir", str(tmp_path / "hub"), "--json"]
    assert main(base + ["profiles", "new", "codex", "work"]) == 0
    capsys.readouterr()

    assert main(base + ["models", "list"]) == 0
    models = json.loads(capsys.readouterr().out)
    assert any(m["model"] == "gpt-6.1-sol" for m in models)

    assert main(base + ["models", "rank", "astra"]) == 0
    ranked = json.loads(capsys.readouterr().out)
    assert ranked[0]["profile"] == "work"
    assert ranked[0]["resolved_model"] == "gpt-6-astra"

    assert main(base + ["smart", "sol", "--dry-run"]) == 0
    chosen = json.loads(capsys.readouterr().out)
    assert chosen["provider"] == "codex"
    assert chosen["resolved_model"] == "gpt-6.1-sol"

    assert main(base + ["ide", "vscode", "--codex-profile", "work", "--dry-run"]) == 0
    ide_plan = json.loads(capsys.readouterr().out)
    assert ide_plan["status"] == "dry-run"
    assert ide_plan["profiles"]["codex"] == "work"

    assert main(base + ["consult", "-m", "sol", "--dry-run", "Оцени код"]) == 0
    consult_res = json.loads(capsys.readouterr().out)
    assert consult_res["opinions"][0]["model"] == "gpt-6.1-sol"
    assert normalize_model("hydro") == "gemini-4-hydrogen-high"
    assert len(catalog_models(Hub(tmp_path / "hub"))) > 0


async def test_tui_models_and_sessions_tabs(tmp_path, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub = Hub(tmp_path / "hub")
    codex_prof = hub.create("codex", "work")
    cx_dir = Path(codex_prof["path"]) / "sessions" / "2026" / "11" / "24"
    cx_dir.mkdir(parents=True)
    (cx_dir / "rollout-1.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-11-24T14:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "sess-1", "cwd": str(Path.cwd())},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-11-24T14:00:05Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Тестовый диалог"}],
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
        app.query_one(TabbedContent).active = "models-tab"
        await pilot.pause()
        app.query_one("#smart-model-select", Select).value = "gpt-6.1-sol"
        await pilot.pause()
        assert app.query_one("#model-candidates", DataTable).row_count == 1

        await pilot.click("#consult-open")
        await pilot.pause()
        assert isinstance(app.screen, ConsultForm)
        await pilot.press("escape")
        await pilot.pause()

        app.query_one(TabbedContent).active = "sessions-tab"
        await pilot.pause()
        assert app.query_one("#sessions-table", DataTable).row_count == 1
        await pilot.click("#session-handoff")
        await pilot.pause()
        assert isinstance(app.screen, HandoffForm)
        await pilot.press("escape")
        await pilot.pause()


async def test_fast_session_scan_and_lazy_tui_navigation(tmp_path, monkeypatch):
    import time

    from textual.widgets import OptionList

    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub = Hub(tmp_path / "hub")
    claude_prof = hub.create("claude", "work")
    c_proj = Path(claude_prof["path"]) / "projects" / "-proj"
    c_proj.mkdir(parents=True)
    huge_payload = "x" * 200_000
    (c_proj / "big-claude.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-11-24T13:00:00Z",
                        "cwd": str(tmp_path),
                        "sessionId": "big-claude",
                        "message": {"role": "user", "content": "Быстрый тест"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-11-24T13:01:00Z",
                        "cwd": str(tmp_path),
                        "sessionId": "big-claude",
                        "message": {
                            "model": "claude-opus-5-5",
                            "role": "assistant",
                            "content": [{"type": "text", "text": huge_payload}],
                        },
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    t0 = time.perf_counter()
    sessions = list_sessions(hub, cwd=tmp_path)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "big-claude"
    assert sessions[0]["title"] == "Быстрый тест"

    detail = read_session(hub, "big-claude")
    assert len(detail["messages"]) == 2
    assert len(detail["messages"][1]["text"]) == 200_000

    scan_calls = 0
    import harnesshub.tui as tui_mod

    orig_list_sessions = tui_mod.list_sessions

    def spy_list_sessions(*args, **kwargs):
        nonlocal scan_calls
        scan_calls += 1
        return orig_list_sessions(*args, **kwargs)

    monkeypatch.setattr(tui_mod, "list_sessions", spy_list_sessions)

    app = HarnessHubApp(hub, "claude")
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        # profiles-tab is active initially, so list_sessions must not have been called
        assert scan_calls == 0

        # Switching harnesses on sidebar must not scan sessions
        app.query_one("#harness-list", OptionList).highlighted = 1
        await pilot.pause()
        app.query_one("#harness-list", OptionList).highlighted = 0
        await pilot.pause()
        assert scan_calls == 0

        # Switching to sessions-tab lazily populates #sessions-table
        app.query_one("#project", Input).value = str(tmp_path)
        app.query_one(TabbedContent).active = "sessions-tab"
        await pilot.pause()
        assert scan_calls == 1
        assert app.query_one("#sessions-table", DataTable).row_count == 1
