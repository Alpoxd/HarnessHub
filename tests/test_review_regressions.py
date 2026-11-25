from __future__ import annotations

import io
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from harnesshub.agy import AgyMaintenance
from harnesshub.cli import main
from harnesshub.consult import consult_peers, headless_command, select_peer_targets
from harnesshub.errors import BusyError, HubError
from harnesshub.ide import launch_ide
from harnesshub.locking import FileLock, inspect_lock_state, resource_lock
from harnesshub.mcp import handle_mcp_request, serve_mcp
from harnesshub.models import mark_quota_cooldown, rank_candidates
from harnesshub.providers.agy import scan_agy_token_stats
from harnesshub.providers.base import cwd_matches, profile_lock_state, strip_file_uri
from harnesshub.rpc import JsonRpcStdioClient
from harnesshub.service import Hub
from harnesshub.telemetry import quota_bucket
from harnesshub.tui import HarnessHubApp


@pytest.fixture
def hub(tmp_path: Path) -> Hub:
    return Hub(tmp_path / "hub", lock_root=tmp_path / "locks")


def test_parallel_shared_sessions_do_not_block_and_tiebreak_ranking(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    p1 = hub.create("codex", "acc1")
    p2 = hub.create("codex", "acc2")
    for p in (p1, p2):
        (Path(p["path"]) / "auth.json").write_text('{"tokens":{}}', encoding="utf-8")
        (Path(p["path"]) / "models_cache.json").write_text(
            json.dumps({"models": [{"slug": "gpt-6.1-sol", "visibility": "list"}]}),
            encoding="utf-8",
        )

    lock_p1 = resource_lock(Path(p1["path"]), hub.lock_root)
    # Missing lock file must not be created during inspection
    assert inspect_lock_state(lock_p1) == (False, False)
    assert not lock_p1.exists()

    # Two shared session locks on the same profile succeed concurrently
    with FileLock(lock_p1, shared=True):
        with FileLock(lock_p1, shared=True):
            locked, active = profile_lock_state(hub, p1)
            assert locked is False
            assert active is True
            # Shared launch works while another shared session is active
            monkeypatch.setattr(hub, "command", lambda *a, **kw: [sys.executable, "-c", "pass"])
            assert hub.launch("codex", "acc1") == 0
            # Exclusive remove/login is blocked while shared session is active
            with pytest.raises(BusyError):
                hub.remove("codex", "acc1")

        # Ranking softens tie-break: idle acc2 ranks ahead of active acc1
        ranked = rank_candidates(hub, "sol")
        assert [r["profile"] for r in ranked] == ["acc2", "acc1"]

        # Quota-ready active acc1 still ranks ahead of depleted idle acc2
        qdir = hub.store.root / "quotas"
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "codex-usage.acc2.json").write_text(
            json.dumps({"windows": [{"window": "primary", "remaining_percent": 0.0}]}),
            encoding="utf-8",
        )
        ranked_quota = rank_candidates(hub, "sol")
        assert ranked_quota[0]["profile"] == "acc1"
        assert ranked_quota[0]["active"] is True


def test_ide_multiprofile_isolation_and_proxy_secret_redaction(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    agy_prof = hub.create("agy", "acc1")
    claude_prof = hub.create("claude", "work")
    codex_prof = hub.create("codex", "main")
    hub.set_proxy({"mode": "url", "url": "http://proxyuser:topsecret@127.0.0.1:8080"})

    plan = launch_ide(
        hub,
        "vscode",
        agy_profile="acc1",
        claude_profile="work",
        codex_profile="main",
        cwd=tmp_path,
        dry_run=True,
    )
    assert plan["isolated_env"]["HOME"] == agy_prof["path"]
    assert plan["isolated_env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/nonexistent/bus"
    assert plan["isolated_env"]["CLAUDE_CONFIG_DIR"] == claude_prof["path"]
    assert plan["isolated_env"]["CODEX_HOME"] == codex_prof["path"]
    # Proxy credentials must never appear in argv or dry-run JSON
    serialized = json.dumps(plan, ensure_ascii=False)
    assert "topsecret" not in serialized
    assert "proxyuser" not in serialized
    assert "--proxy-server" not in plan["command"]

    # Cursor IDE profile isolation rejects -p cleanly
    with pytest.raises(HubError, match="не поддерживается"):
        launch_ide(hub, "cursor", profile="foo", dry_run=True)


async def test_tui_escapes_markup_in_inspector_and_session_preview(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    claude_prof = hub.create("claude", "work")
    cpath = Path(claude_prof["path"])
    cat_dir = cpath / "cache" / "model-catalog"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalog.json").write_text(
        json.dumps(
            {
                "catalog": {
                    "config": {
                        "models": [
                            {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"},
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
    proj_dir = cpath / "projects" / "-proj"
    proj_dir.mkdir(parents=True)
    (proj_dir / "markup-sess.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-11-24T13:00:00Z",
                "cwd": str(Path.cwd()),
                "message": {
                    "role": "user",
                    "content": "Проверь список [1, 2] и тег [/bold] и [unknown_style]",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    app = HarnessHubApp(hub, "claude")
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        insp = str(app.query_one("#account-inspector", Static).render())
        assert "Pro, Max, or Ultra" in insp

        app.query_one(TabbedContent).active = "sessions-tab"
        await pilot.pause()
        assert app.query_one("#sessions-table", DataTable).row_count == 1
        preview = str(app.query_one("#session-preview", Static).render())
        assert "[/bold]" in preview
        assert "[unknown_style]" in preview


def test_cooldown_normalizes_aliases_and_known_quota_beats_unknown(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    assert quota_bucket("hydro") == "hydrogen"
    assert quota_bucket("orch") == "argon"

    acc1 = hub.create("agy", "acc1")
    acc2 = hub.create("agy", "acc2")
    for prof in (acc1, acc2):
        (Path(prof["path"]) / ".models").write_text(
            "gemini-4-argon-high\ngemini-4-hydrogen-high\ngemini-pro-orchestrator-g1-pro\n",
            encoding="utf-8",
        )
        tok = Path(prof["path"]) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        tok.parent.mkdir(parents=True, exist_ok=True)
        tok.write_text('{"id_token_email":"u@example.com"}', encoding="utf-8")

    mark_quota_cooldown(hub, "agy", "acc1", "hydro", duration_seconds=1800)
    ranked_hydro = rank_candidates(hub, "hydro")
    by_name = {r["profile"]: r for r in ranked_hydro}
    assert by_name["acc1"]["quota_ready"] is False
    assert ranked_hydro[0]["profile"] == "acc2"

    mark_quota_cooldown(hub, "agy", "acc2", "orch", duration_seconds=1800)
    assert rank_candidates(hub, "orch")[0]["profile"] == "acc1"

    # Known live/cache quota (82%) must rank ahead of unprobed (unknown) profile
    qdir = hub.store.root / "quotas"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "agy-telemetry.acc2.json").write_text(
        json.dumps(
            {
                "windows": [{"window": "hydrogen", "remaining_percent": 82.0}],
                "model_quotas": {"gemini-4-hydrogen-high": {"remaining_percent": 82.0}},
            }
        ),
        encoding="utf-8",
    )
    ranked_known = rank_candidates(hub, "hydro")
    assert ranked_known[0]["profile"] == "acc2"
    assert ranked_known[0]["quota_known"] is True
    assert ranked_known[0]["remaining_percent"] == 82.0


def test_consult_readonly_flags_and_lock_handling(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    agy_prof = hub.create("agy", "acc1")
    (Path(agy_prof["path"]) / ".models").write_text("gemini-4-argon-high\n", encoding="utf-8")
    claude_prof = hub.create("claude", "work")
    monkeypatch.setattr(
        "harnesshub.registry.Harness.executable",
        lambda self: f"/usr/bin/{self.commands[0]}",
    )

    agy_cmd = headless_command(hub, "agy", agy_prof, "gemini-4-argon-high", "check", tmp_path)
    assert "--mode" in agy_cmd and agy_cmd[agy_cmd.index("--mode") + 1] == "plan"

    claude_cmd = headless_command(hub, "claude", claude_prof, "claude-opus-5-5", "check", tmp_path)
    assert "--permission-mode" in claude_cmd
    assert claude_cmd[claude_cmd.index("--permission-mode") + 1] == "plan"
    assert "--disallowed-tools" in claude_cmd

    # Exclusive lock blocks select_peer_targets with BusyError; shared session allows it
    lock_file = resource_lock(Path(claude_prof["path"]), hub.lock_root)
    with FileLock(lock_file, shared=True):
        targets = select_peer_targets(hub, models=["opus"])
        assert targets[0]["profile"] == "work"
    with FileLock(lock_file, shared=False):
        with pytest.raises(BusyError):
            select_peer_targets(hub, models=["opus"])

    dry = consult_peers(hub, "Проверь план", models=["opus"], cwd=tmp_path, dry_run=True)
    assert dry["opinions"][0]["status"] == "dry-run"


def test_json_rpc_stdio_client_pipe_handling(tmp_path: Path) -> None:
    server = tmp_path / "rpc_echo.py"
    server.write_text(
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    m = req.get('method')\n"
        "    if m == 'initialized':\n"
        "        continue\n"
        "    if m == 'ping':\n"
        "        print(json.dumps({'id': req['id'], 'result': {'ok': True}}), flush=True)\n"
        "    elif m == 'fail':\n"
        "        err = {'code': -1, 'message': 'bad'}\n"
        "        print(json.dumps({'id': req['id'], 'error': err}), flush=True)\n"
        "    elif m == 'slow':\n"
        "        time.sleep(2.0)\n",
        encoding="utf-8",
    )
    with JsonRpcStdioClient([sys.executable, str(server)], env={}) as client:
        client.notify("initialized")
        assert client.request("ping", {}, timeout=2.0) == {"ok": True}
        assert client.request("fail", {}, optional=True, timeout=2.0) == {}
        with pytest.raises(HubError):
            client.request("fail", {}, optional=False, timeout=2.0)
        with pytest.raises(HubError, match="не ответил"):
            client.request("slow", {}, timeout=0.2)


def _make_elf(gates: int = 2) -> bytes:
    data = bytearray(512)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", data, 18, 0x3E)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HH", data, 54, 56, 1)
    struct.pack_into("<IIQ", data, 64, 1, 5, 128)
    struct.pack_into("<Q", data, 96, 384)
    for index in range(gates):
        start, register = 160 + index * 64, index
        first = 400 - (start + 9)
        sequence = bytes((0x48, 0x85, 0xC0 | register << 3 | register))
        sequence += b"\x0f\x84" + struct.pack("<i", first)
        sequence += bytes((0x80, 0x78 + register, 8, 0))
        sequence += b"\x0f\x85" + struct.pack("<i", first - 10)
        data[start : start + len(sequence)] = sequence
    return bytes(data)


def test_agy_manifest_cleanup_fast_check_and_external_helper(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux ELF patch test")

    target = tmp_path / "agy-bin"
    original = _make_elf()
    target.write_bytes(original)
    target.chmod(0o755)
    manager = AgyMaintenance(hub)
    manager.configure(binary=str(target))
    manifest_path = target.with_name(target.name + ".harnesshub.json")

    # Failed --version cleans up manifest
    monkeypatch.setattr(
        "harnesshub.agy.subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=1)
    )
    with pytest.raises(HubError):
        manager.heal()
    assert target.read_bytes() == original
    assert not manifest_path.exists()

    # Successful heal writes manifest with stat info; fast_check skips re-reading; restore cleans
    monkeypatch.setattr(
        "harnesshub.agy.subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0)
    )
    assert manager.heal()["changed"] is True
    assert manifest_path.is_file()
    assert manager.heal(fast_check=True)["changed"] is False
    manager.restore()
    assert not manifest_path.exists()

    # External helper passes on unknown signature if --version ok, rejects if unpatched gates left
    helper = tmp_path / "helper.sh"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)
    manager.configure(helper=str(helper))

    # Still unpatched -> must reject
    with pytest.raises(HubError, match="не подтвердил"):
        manager.heal()

    # Non-standard/updated binary -> accepted via helper when --version returns 0
    target.write_bytes(b"updated-new-agy-format")
    res = manager.heal()
    assert res["status"] == "patched"
    assert res["verified_by"] == "helper"


def test_mcp_validation_batch_and_stdout_protection(hub: Hub) -> None:
    r1 = handle_mcp_request(hub, 123)
    assert isinstance(r1, dict) and r1["error"]["code"] == -32600
    r2 = handle_mcp_request(hub, [])
    assert isinstance(r2, dict) and r2["error"]["code"] == -32600

    batch_res = handle_mcp_request(
        hub,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "consult_council",
                    "arguments": {"question": "hi", "models": "argon"},
                },
            },
        ],
    )
    assert isinstance(batch_res, list) and len(batch_res) == 2
    assert batch_res[0]["result"] == {}
    assert batch_res[1]["result"]["isError"] is True
    assert "массивом" in batch_res[1]["result"]["content"][0]["text"]

    # serve_mcp redirects stray stdout prints away from protocol output
    inp = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 10, "method": "ping"}) + "\n")
    out = io.StringIO()
    assert serve_mcp(hub, stdin=inp, stdout=out) == 0
    parsed = json.loads(out.getvalue().strip())
    assert parsed["id"] == 10


def test_agy_token_cache_invalidation_and_cross_platform_paths(tmp_path: Path) -> None:
    base = tmp_path / "agy-prof"
    tr_dir = base / "brain" / "conv-1" / ".system_generated" / "logs"
    tr_dir.mkdir(parents=True)
    tr = tr_dir / "transcript.jsonl"
    tr.write_text(
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-argon-high",
                "input_tokens": 100,
                "output_tokens": 50,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stats1 = scan_agy_token_stats(base)
    assert stats1["total_tokens"] == 150

    # Append to existing transcript without changing brain/ mtime
    with tr.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-4-argon-high",
                    "input_tokens": 200,
                    "output_tokens": 100,
                }
            )
            + "\n"
        )
    stats2 = scan_agy_token_stats(base)
    assert stats2["total_tokens"] == 450

    assert strip_file_uri("file:///C:/Users/dev/repo").replace("\\", "/") == "C:/Users/dev/repo"
    assert strip_file_uri("file:///home/user/my%20repo").replace("\\", "/") == "/home/user/my repo"
    assert cwd_matches(str(tmp_path / "repo" / "sub"), str(tmp_path / "repo"))
    assert not cwd_matches(str(tmp_path / "repo2"), str(tmp_path / "repo"))


def test_cli_human_output_for_consult_sessions_show_discover_and_empty(
    hub: Hub, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    monkeypatch.setattr("harnesshub.registry.Harness.executable", lambda self: "/usr/bin/codex")
    base = ["--data-dir", str(hub.store.root)]

    # Empty lists render friendly messages instead of raw []
    assert main(base + ["profiles", "list"]) == 0
    out_empty = capsys.readouterr().out
    assert "[]" not in out_empty
    assert "Профили" in out_empty

    # Create profile and session, check consult dry-run and sessions show human rendering
    prof = hub.create("codex", "work")
    sess_dir = Path(prof["path"]) / "sessions" / "2026" / "11" / "24"
    sess_dir.mkdir(parents=True)
    (sess_dir / "rollout-cli.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-11-24T12:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "sess-cli-1", "cwd": str(tmp_path)},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-11-24T12:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Привет [тег]"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(base + ["sessions", "show", "sess-cli-1"]) == 0
    out_show = capsys.readouterr().out
    assert "Привет [тег]" in out_show
    assert '"messages"' not in out_show

    assert main(base + ["consult", "-m", "sol", "--dry-run", "Проверь код"]) == 0
    out_consult = capsys.readouterr().out
    assert "Проверь код" in out_consult
    assert '"opinions"' not in out_consult
