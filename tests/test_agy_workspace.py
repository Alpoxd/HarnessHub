from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from textual.widgets import Button, Static, TabbedContent

from harnesshub.agy_workspace import (
    agy_history_status,
    resolve_real_home,
    set_agy_shared_history,
    sync_agy_profiles,
)
from harnesshub.cli import main
from harnesshub.consult import headless_command
from harnesshub.errors import HubError
from harnesshub.locking import FileLock, resource_lock
from harnesshub.service import Hub
from harnesshub.sessions import list_sessions
from harnesshub.tui import HarnessHubApp


def _write_agy_session(
    base_dir: Path,
    cid: str,
    title: str,
    cwd: Path,
    *,
    db_row: bool = True,
) -> None:
    log_dir = base_dir / "brain" / cid / ".system_generated" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "transcript.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step_index": 0,
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "created_at": "2026-11-24T12:00:00Z",
                        "content": f"<USER_REQUEST>\n{title}\n</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "model": "gemini-4-hydrogen-high",
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "created_at": "2026-11-24T12:01:00Z",
                        "content": f"Ответ для {cid}",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    conv_dir = base_dir / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    (conv_dir / f"{cid}.pb").write_bytes(f"pb:{cid}".encode())

    hist = base_dir / "history.jsonl"
    with hist.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "display": title,
                    "timestamp": 1795521459530,
                    "workspace": str(cwd),
                    "conversationId": cid,
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    if db_row:
        db_path = base_dir / "conversation_summaries.db"
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE IF NOT EXISTS conversation_summaries (id TEXT PRIMARY KEY, title TEXT)"
        )
        con.execute(
            "INSERT OR REPLACE INTO conversation_summaries (id, title) VALUES (?, ?)",
            (cid, title),
        )
        con.commit()
        con.close()


def test_discover_and_import_agy_main_when_store_is_inside_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    fake_home = tmp_path / "user-home"
    (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    monkeypatch.setenv("HARNESSHUB_USER_HOME", str(fake_home))

    store_dir = fake_home / ".local" / "share" / "harnesshub"
    hub = Hub(store_dir, lock_root=tmp_path / "locks")

    discovered = hub.existing_profiles()
    assert {
        "provider": "agy",
        "name": "main",
        "path": str(fake_home.resolve()),
    } in discovered

    imported = hub.import_profile("agy", "main", fake_home)
    assert imported["path"] == str(fake_home.resolve())

    # Importing store_root or a subdir inside store_root must still be rejected
    (store_dir / "profiles").mkdir(parents=True, exist_ok=True)
    with pytest.raises(HubError, match="Каталог данных менеджера"):
        hub.import_profile("agy", "bad1", store_dir)
    with pytest.raises(HubError, match="Каталог данных менеджера"):
        hub.import_profile("agy", "bad2", store_dir / "profiles")

    # Resolving real home from inside a child profile falls back to passwd home
    monkeypatch.delenv("HARNESSHUB_USER_HOME", raising=False)
    monkeypatch.setattr("harnesshub.agy_workspace._posix_passwd_home", lambda: fake_home.resolve())
    acc1 = hub.create("agy", "acc1")
    monkeypatch.setenv("HOME", acc1["path"])
    assert resolve_real_home(hub) == fake_home.resolve()


def test_automode_cli_flags_overrides_and_consult_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    monkeypatch.setattr(
        "harnesshub.registry.Harness.executable",
        lambda self: f"/usr/bin/{self.commands[0]}",
    )
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    agy_acc1 = hub.create("agy", "acc1")
    agy_acc2 = hub.create("agy", "acc2")
    claude_work = hub.create("claude", "work")
    codex_work = hub.create("codex", "work")

    # Disabled by default for backwards compatibility
    assert "--dangerously-skip-permissions" not in hub.command("agy", agy_acc1, cwd=tmp_path)

    # Enabling AGY automode adds --dangerously-skip-permissions for runs, never for login/consult
    info = hub.set_automode(True, "agy")
    assert info["effective"] is True
    assert "--dangerously-skip-permissions" in hub.command("agy", agy_acc1, cwd=tmp_path)
    assert "--dangerously-skip-permissions" not in hub.command(
        "agy", agy_acc1, login=True, cwd=tmp_path
    )
    assert "--dangerously-skip-permissions" not in hub.command(
        "agy", agy_acc1, automode=False, cwd=tmp_path
    )
    assert "--dangerously-skip-permissions" not in hub.command(
        "agy", agy_acc1, args=("--mode", "plan"), cwd=tmp_path
    )

    consult_cmd = headless_command(hub, "agy", agy_acc1, "gemini-4-argon-high", "check", tmp_path)
    assert "--dangerously-skip-permissions" not in consult_cmd
    assert consult_cmd[consult_cmd.index("--mode") + 1] == "plan"

    # Per-profile override & inherit
    hub.set_automode(False, "agy", "acc1")
    assert "--dangerously-skip-permissions" not in hub.command("agy", agy_acc1, cwd=tmp_path)
    assert "--dangerously-skip-permissions" in hub.command("agy", agy_acc2, cwd=tmp_path)
    hub.set_automode(None, "agy", "acc1")
    assert "--dangerously-skip-permissions" in hub.command("agy", agy_acc1, cwd=tmp_path)

    # Global automode applies to claude and codex too
    hub.set_automode(True)
    assert "--dangerously-skip-permissions" in hub.command("claude", claude_work, cwd=tmp_path)
    assert "--yolo" in hub.command("codex", codex_work, cwd=tmp_path)
    assert "--yolo" not in hub.command("codex", codex_work, args=("-s", "read-only"), cwd=tmp_path)


def test_agy_environment_locale_and_dbus_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    fake_home = tmp_path / "real-home"
    (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    monkeypatch.setenv("HARNESSHUB_USER_HOME", str(fake_home))
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")

    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    main_prof = hub.import_profile("agy", "main", fake_home)
    acc1 = hub.create("agy", "acc1")

    acc1_env = hub.environment("agy", acc1)
    assert acc1_env["LANG"] == "en_US.UTF-8"
    assert acc1_env["LC_ALL"] == "C.UTF-8"
    assert acc1_env["LANGUAGE"] == "en_US"
    assert acc1_env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/nonexistent/bus"

    # Canonical main without file token keeps session DBUS for keyring access
    main_env = hub.environment("agy", main_prof)
    assert main_env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"

    # Once main stores antigravity-oauth-token on disk, DBUS is suppressed like agyq does
    tok = fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    tok.write_text('{"id_token_email":"main@example.com"}', encoding="utf-8")
    main_env_tok = hub.environment("agy", main_prof)
    assert main_env_tok["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/nonexistent/bus"


def test_agy_shared_history_merges_chats_and_keeps_tokens_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    fake_home = tmp_path / "real-home"
    proj = tmp_path / "project"
    proj.mkdir()
    monkeypatch.setenv("HARNESSHUB_USER_HOME", str(fake_home))

    main_base = fake_home / ".gemini" / "antigravity-cli"
    main_base.mkdir(parents=True)
    (main_base / "antigravity-oauth-token").write_text(
        '{"id_token_email":"main@example.com"}', encoding="utf-8"
    )
    _write_agy_session(main_base, "conv-main", "Основной чат", proj)

    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    hub.import_profile("agy", "main", fake_home)
    acc1 = hub.create("agy", "acc1")
    acc2 = hub.create("agy", "acc2")

    base1 = Path(acc1["path"]) / ".gemini" / "antigravity-cli"
    (base1 / "antigravity-oauth-token").write_text(
        '{"id_token_email":"acc1@example.com"}', encoding="utf-8"
    )
    _write_agy_session(base1, "conv-1", "Чат первого профиля", proj)
    (base1 / "conversation_summaries.db-wal").write_bytes(b"stale-wal")

    base2 = Path(acc2["path"]) / ".gemini" / "antigravity-cli"
    (base2 / "antigravity-oauth-token").write_text(
        '{"id_token_email":"acc2@example.com"}', encoding="utf-8"
    )
    _write_agy_session(base2, "conv-2", "Чат второго профиля", proj)

    res = set_agy_shared_history(hub, True, home=fake_home)
    assert res["enabled"] is True
    assert set(res["linked"]) == {"acc1", "acc2"}
    assert res["canonical"] == ["main"]

    # Tokens MUST remain strictly isolated regular files
    assert not (base1 / "antigravity-oauth-token").is_symlink()
    assert not (base2 / "antigravity-oauth-token").is_symlink()
    assert "acc1@example.com" in (base1 / "antigravity-oauth-token").read_text(encoding="utf-8")
    assert "acc2@example.com" in (base2 / "antigravity-oauth-token").read_text(encoding="utf-8")
    assert "main@example.com" in (main_base / "antigravity-oauth-token").read_text(encoding="utf-8")

    # History dirs/files in profiles point to shared canonical store
    assert os.path.realpath(base1 / "brain") == str((main_base / "brain").resolve())
    assert os.path.realpath(base2 / "conversations") == str((main_base / "conversations").resolve())
    assert os.path.realpath(base1 / "conversation_summaries.db") == str(
        (main_base / "conversation_summaries.db").resolve()
    )
    assert not (base1 / "conversation_summaries.db-wal").exists()

    # All 3 conversations merged into canonical SQLite index and brain/
    with sqlite3.connect(main_base / "conversation_summaries.db") as con:
        ids = {r[0] for r in con.execute("SELECT id FROM conversation_summaries").fetchall()}
    assert ids == {"conv-main", "conv-1", "conv-2"}
    assert (main_base / "conversations" / "conv-1.pb").is_file()
    assert (main_base / "conversations" / "conv-2.pb").is_file()

    # list_sessions deduplicates symlinks across accounts
    sessions = list_sessions(hub, cwd=None)
    assert {s["session_id"] for s in sessions} == {"conv-main", "conv-1", "conv-2"}

    # Can resume shared conversation using another profile (no relogin!)
    launched: list[tuple[list[str], str]] = []

    class _FakeProc:
        def wait(self) -> int:
            return 0

    def fake_popen(
        cmd: list[str], env: dict[str, str], cwd: Path | None = None, **kw: object
    ) -> _FakeProc:
        launched.append((list(cmd), str(env["HOME"])))
        return _FakeProc()

    monkeypatch.setattr("harnesshub.registry.Harness.executable", lambda self: "/usr/bin/agy")
    monkeypatch.setattr("harnesshub.service.subprocess.Popen", fake_popen)
    assert hub.resume_session("conv-1", target_profile="acc2", cwd=proj) == 0
    assert launched[-1][1] == acc2["path"]
    assert "--resume" in launched[-1][0] and "conv-1" in launched[-1][0]

    # Newly created AGY profile automatically links when share-history is ON
    acc3 = hub.create("agy", "acc3")
    base3 = Path(acc3["path"]) / ".gemini" / "antigravity-cli"
    assert (base3 / "brain").is_symlink()
    assert os.path.realpath(base3 / "brain") == str((main_base / "brain").resolve())

    # Turning off share-history restores independent local dirs while keeping canonical data intact
    off_res = set_agy_shared_history(hub, False, home=fake_home, copy_local=False)
    assert off_res["enabled"] is False
    assert set(off_res["unlinked"]) == {"acc1", "acc2", "acc3"}
    assert (base1 / "brain").is_dir() and not (base1 / "brain").is_symlink()
    assert (main_base / "brain" / "conv-1").is_dir()


def test_agy_shared_history_skips_busy_profile_and_sync_adapts_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    fake_home = tmp_path / "real-home"
    src_base = fake_home / ".gemini" / "antigravity-cli"
    src_base.mkdir(parents=True)
    (src_base / "settings.json").write_text(
        json.dumps(
            {
                "model": "Gemini 4 Neon (XHigh)",
                "orchestrator": {"planner": "Gemini 4 Carbon (High)"},
                "colorScheme": "dark",
            }
        ),
        encoding="utf-8",
    )
    (fake_home / ".gemini" / "config").mkdir(parents=True)
    (fake_home / ".gemini" / "config" / "GEMINI.md").write_text("# Rules\n", encoding="utf-8")
    (fake_home / ".agents" / "skills" / "demo").mkdir(parents=True)
    (fake_home / ".agents" / "skills" / "demo" / "SKILL.md").write_text("hi\n", encoding="utf-8")
    (fake_home / ".gitconfig").write_text("[user]\n\tname = Test\n", encoding="utf-8")
    monkeypatch.setenv("HARNESSHUB_USER_HOME", str(fake_home))

    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    acc1 = hub.create("agy", "acc1")
    acc2 = hub.create("agy", "acc2")
    (Path(acc1["path"]) / ".models").write_text(
        "gemini-4-hydrogen-high\nclaude-opus-5-5-high\n", encoding="utf-8"
    )
    (Path(acc2["path"]) / ".models").write_text(
        "gemini-3.8-flash-high\nclaude-sonnet-4-6\n", encoding="utf-8"
    )

    sync_res = sync_agy_profiles(hub, home=fake_home)
    assert set(sync_res["synced"]) == {"acc1", "acc2"}

    s1 = json.loads(
        (Path(acc1["path"]) / ".gemini" / "antigravity-cli" / "settings.json").read_text()
    )
    s2 = json.loads(
        (Path(acc2["path"]) / ".gemini" / "antigravity-cli" / "settings.json").read_text()
    )
    assert s1["model"] == "Gemini 4 Neon (XHigh)"
    assert "orchestrator" in s1
    assert s2["model"] == "Gemini 3.8 Flash (High)"
    assert "orchestrator" not in s2
    assert (Path(acc2["path"]) / ".agents" / "skills" / "demo" / "SKILL.md").read_text() == "hi\n"
    assert (Path(acc1["path"]) / ".gemini" / "config" / "GEMINI.md").is_symlink()

    # Busy profile is deferred instead of crashing
    with FileLock(resource_lock(Path(acc1["path"]), hub.lock_root), shared=True):
        res = set_agy_shared_history(hub, True, home=fake_home)
        assert "acc1" in res["deferred_busy"]
        assert "acc2" in res["linked"]
        status = agy_history_status(hub, home=fake_home)
        modes = {r["profile"]: r["mode"] for r in status["profiles"]}
        assert modes["acc2"] == "shared"

    # Once unlocked, launching acc1 lazily links shared history before spawning process
    monkeypatch.setattr("harnesshub.registry.Harness.executable", lambda self: "/usr/bin/agy")

    class _DoneProc:
        def wait(self) -> int:
            return 0

    monkeypatch.setattr("harnesshub.service.subprocess.Popen", lambda *a, **kw: _DoneProc())
    assert hub.launch("agy", "acc1", cwd=tmp_path) == 0
    assert (Path(acc1["path"]) / ".gemini" / "antigravity-cli" / "brain").is_symlink()


async def test_agy_cli_and_tui_workspace_controls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    fake_home = tmp_path / "real-home"
    (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    monkeypatch.setenv("HARNESSHUB_USER_HOME", str(fake_home))

    data_dir = tmp_path / "hub"
    base = ["--data-dir", str(data_dir), "--json"]
    assert main([*base, "profiles", "new", "agy", "acc1"]) == 0
    capsys.readouterr()

    assert main([*base, "agy", "automode", "on"]) == 0
    assert json.loads(capsys.readouterr().out)["effective"] is True

    assert main([*base, "agy", "share-history", "on", "--home", str(fake_home)]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True

    assert main([*base, "doctor"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["agy_automode"] is True
    assert doc["agy_shared_history"] is True

    assert main([*base, "agy", "automode", "off"]) == 0
    capsys.readouterr()

    # Verify TUI widgets and interactions on standard size (120x42) and compact size (80x30)
    hub = Hub(data_dir)
    app = HarnessHubApp(hub, "agy")
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        auto_btn = app.query_one("#toggle-automode", Button)
        assert "ВЫКЛ" in str(auto_btn.label)
        assert await pilot.click("#toggle-automode") is True
        await pilot.pause()
        assert hub.automode_enabled("agy") is True
        assert "ВКЛ" in str(app.query_one("#toggle-automode", Button).label)

        assert await pilot.click("#--content-tab-settings-tab") is True
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "settings-tab"
        assert "включён" in str(app.query_one("#agy-workspace-summary", Static).render())
        app.query_one("#agy-history-toggle", Button).scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click("#agy-history-toggle") is True
        await pilot.pause()
        assert bool(hub.store.read()["settings"].get("agy_shared_history")) is False
        app.query_one("#agy-sync", Button).scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click("#agy-sync") is True
        await pilot.pause()

    compact_app = HarnessHubApp(hub, "agy")
    async with compact_app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        assert "ВКЛ" in str(compact_app.query_one("#toggle-automode", Button).label)
        assert await pilot.click("#--content-tab-settings-tab") is True
        await pilot.pause()
        compact_app.query_one("#agy-automode-toggle", Button).scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click("#agy-automode-toggle") is True
        await pilot.pause()
        assert hub.automode_enabled("agy") is False
