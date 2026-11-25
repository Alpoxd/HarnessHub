from pathlib import Path
from typing import Any

from textual.widgets import DataTable, Input, OptionList, Select, TabbedContent

from harnesshub.service import Hub
from harnesshub.tui import ConfirmRemove, HarnessHubApp, InstallForm, ProfileForm, ProxyForm


async def test_keyboard_create_profile_and_change_harness(tmp_path):
    hub = Hub(tmp_path / "hub")
    app = HarnessHubApp(hub, "codex")
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        await pilot.click("#new")
        await pilot.pause()
        screen: Any = app.screen
        assert isinstance(screen, ProfileForm)
        screen.query_one("#profile-name", Input).value = "work"
        await pilot.click("#save")
        await pilot.pause()
        assert hub.profile("codex", "work")
        assert app.query_one("#profiles", DataTable).row_count == 1
        app.query_one("#harness-list", OptionList).highlighted = 4
        await pilot.pause()
        assert app.provider == "kimi"


async def test_compact_terminal_still_mounts_and_modal_escape_works(tmp_path):
    app = HarnessHubApp(Hub(tmp_path / "hub"), "codex")
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, ProfileForm)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]


async def test_proxy_tui_edits_global_and_profile_settings(tmp_path):
    hub = Hub(tmp_path / "hub")
    hub.create("codex", "work")
    app = HarnessHubApp(hub, "codex")
    async with app.run_test(size=(120, 42)) as pilot:
        app.query_one(TabbedContent).active = "settings-tab"
        await pilot.pause()
        await pilot.click("#global-proxy")
        await pilot.pause()
        screen: Any = app.screen
        assert isinstance(screen, ProxyForm)
        screen.query_one("#proxy-mode", Select).value = "url"
        screen.query_one("#proxy-url", Input).value = "http://local-proxy:8080"
        await pilot.click("#save")
        await pilot.pause()
        assert hub.proxy_info()["effective"]["url"] == "http://local-proxy:8080"
        await pilot.click("#profile-proxy")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ProxyForm)
        screen.query_one("#proxy-mode", Select).value = "direct"
        await pilot.click("#save")
        await pilot.pause()
        assert hub.proxy_info("codex", "work")["effective"]["mode"] == "direct"


async def test_missing_harness_has_official_install_dialog(tmp_path, monkeypatch):
    from harnesshub.registry import Harness

    monkeypatch.setattr(Harness, "executable", lambda self: None)
    hub = Hub(tmp_path / "hub")
    app = HarnessHubApp(hub, "qoder")
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        await pilot.click("#install-harness")
        await pilot.pause()
        screen: Any = app.screen
        assert isinstance(screen, InstallForm)
        assert screen.plan["source"] == "https://registry.npmjs.org/@qoder-ai/qodercli"
        await pilot.press("escape")
        await pilot.pause()
        assert not hub.store.root.exists()


async def test_remove_profile_modal_cancel_and_confirm(tmp_path, monkeypatch):
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub = Hub(tmp_path / "hub")
    profile = hub.create("agy", "acc5")
    profile_dir = Path(profile["path"])
    app = HarnessHubApp(hub, "agy")
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        await pilot.click("#remove")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmRemove)
        await pilot.press("escape")
        await pilot.pause()
        assert len(hub.profiles("agy")) == 1

        await pilot.click("#remove")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmRemove)
        await pilot.click("#remove-confirm")
        await pilot.pause()
        assert hub.profiles("agy") == []
        assert app.query_one("#profiles", DataTable).row_count == 0
        assert profile_dir.is_dir()
