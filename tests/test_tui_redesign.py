from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import DataTable, Input, Select, Static, TabbedContent

from harnesshub.cli import main
from harnesshub.locking import FileLock, resource_lock
from harnesshub.models import catalog_models, group_catalog_models, rank_candidates
from harnesshub.providers.base import atomic_write_json, classify_agy_tier, create_link
from harnesshub.service import Hub
from harnesshub.telemetry import account_telemetry
from harnesshub.tui import HarnessHubApp, ResumeProfileForm
from harnesshub.tui.formatters import (
    fmt_agy_group_quotas,
    fmt_cli_quota_summary,
    fmt_codex_group_quotas,
    fmt_window_rows,
)


def _seed_agy_quota(
    hub: Hub,
    profile_name: str,
    *,
    gemini_pct: float,
    tp_5h_pct: float,
    tp_7d_pct: float,
    plan: str = "Google AI Pro",
) -> None:
    atomic_write_json(
        hub.store.root / "quotas" / f"agy-telemetry.{profile_name}.json",
        {
            "provider": "agy",
            "profile": profile_name,
            "plan": plan,
            "remaining_percent": min(gemini_pct, tp_5h_pct, tp_7d_pct),
            "windows": [
                {
                    "window": "3p-weekly",
                    "label": "Claude and GPT models (weekly)",
                    "remaining_percent": tp_7d_pct,
                    "used_percent": 100.0 - tp_7d_pct,
                    "resets_at": 1796032800,
                },
                {
                    "window": "3p-5h",
                    "label": "Claude and GPT models (5h)",
                    "remaining_percent": tp_5h_pct,
                    "used_percent": 100.0 - tp_5h_pct,
                    "resets_at": 1795622400,
                },
                {
                    "window": "gemini",
                    "label": "Gemini пул",
                    "remaining_percent": gemini_pct,
                    "used_percent": 100.0 - gemini_pct,
                    "resets_at": 1795620600,
                },
            ],
        },
    )


def _seed_codex_quota(
    hub: Hub,
    profile_name: str,
    *,
    pct_5h: float,
    pct_7d: float,
    plan: str = "Plus",
) -> None:
    atomic_write_json(
        hub.store.root / "quotas" / f"codex-telemetry.{profile_name}.json",
        {
            "provider": "codex",
            "profile": profile_name,
            "plan": plan,
            "remaining_percent": min(pct_5h, pct_7d),
            "windows": [
                {
                    "window": "primary",
                    "label": "5ч",
                    "remaining_percent": pct_5h,
                    "used_percent": 100.0 - pct_5h,
                    "minutes": 300,
                    "resets_at": 1795629600,
                },
                {
                    "window": "secondary",
                    "label": "7д",
                    "remaining_percent": pct_7d,
                    "used_percent": 100.0 - pct_7d,
                    "minutes": 10080,
                    "resets_at": 1795975200,
                },
            ],
        },
    )


def test_classify_agy_tier_distinguishes_g4h_46_from_38_46() -> None:
    assert (
        classify_agy_tier(["gemini-4-hydrogen-high", "gemini-4-argon-high", "claude-opus-5-5-high"])
        == "g4+5.5"
    )
    assert classify_agy_tier(["gemini-4-argon-high", "claude-opus-5-5-high"]) == "argon+5.5"
    assert classify_agy_tier(["gemini-4-hydrogen-high", "claude-opus-4-6-thinking"]) == "g4h+4.6"
    assert classify_agy_tier(["gemini-4-hydrogen-high"]) == "g4-hydro"
    assert classify_agy_tier(["gemini-3.8-flash-high", "claude-opus-4-6-thinking"]) == "3.8+4.6"
    assert classify_agy_tier([]) == "unknown"


def test_per_group_quota_formatters_and_cli_accounts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    hub.create("agy", "pro1")
    hub.create("codex", "acc2")
    _seed_agy_quota(hub, "pro1", gemini_pct=88.0, tp_5h_pct=100.0, tp_7d_pct=0.0)
    _seed_codex_quota(hub, "acc2", pct_5h=100.0, pct_7d=12.0)

    agy_info = hub.profiles("agy")[0]
    agy_tel = account_telemetry(hub, agy_info, live=False)
    gem_txt, tp_txt = fmt_agy_group_quotas(agy_tel)
    assert "88%" in gem_txt.plain
    assert "5ч" in tp_txt.plain and "100%" in tp_txt.plain
    assert "7д" in tp_txt.plain and "0%" in tp_txt.plain
    assert "Gem 88%" in fmt_cli_quota_summary(agy_tel)
    assert "3P" in fmt_cli_quota_summary(agy_tel)

    codex_tel = account_telemetry(hub, hub.profiles("codex")[0], live=False)
    w5, w7 = fmt_codex_group_quotas(codex_tel)
    assert "100%" in w5.plain
    assert "12%" in w7.plain
    assert "5ч 100%" in fmt_cli_quota_summary(codex_tel)
    assert "7д 12%" in fmt_cli_quota_summary(codex_tel)

    window_lines = fmt_window_rows(agy_tel)
    assert len(window_lines) == 3

    assert main(["--data-dir", str(tmp_path / "hub"), "accounts"]) == 0
    out = capsys.readouterr().out
    assert "Gem 88%" in out
    assert "5ч 100%" in out


def test_rank_candidates_prefers_higher_quota_over_idle_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    p1 = hub.create("agy", "pro1")
    p2 = hub.create("agy", "pro2")
    for p in (p1, p2):
        (Path(p["path"]) / ".models").write_text("gemini-4-carbon-medium\n", encoding="utf-8")
    _seed_agy_quota(hub, "pro1", gemini_pct=81.9, tp_5h_pct=100.0, tp_7d_pct=100.0)
    _seed_agy_quota(hub, "pro2", gemini_pct=28.7, tp_5h_pct=100.0, tp_7d_pct=100.0)

    # Hold shared lock on pro1 so it is "active" (parallel dialogs allowed)
    lock_p1 = resource_lock(Path(p1["path"]), hub.lock_root)
    with FileLock(lock_p1, shared=True):
        ranked = rank_candidates(hub, "gemini-4-carbon-medium")
    assert ranked[0]["profile"] == "pro1"
    assert ranked[0]["active"] is True
    assert ranked[1]["profile"] == "pro2"
    assert ranked[1]["active"] is False

    # When quotas are equal, idle still tie-breaks ahead of active
    _seed_agy_quota(hub, "pro2", gemini_pct=81.9, tp_5h_pct=100.0, tp_7d_pct=100.0)
    with FileLock(lock_p1, shared=True):
        tied = rank_candidates(hub, "gemini-4-carbon-medium")
    assert tied[0]["profile"] == "pro2"
    assert tied[1]["profile"] == "pro1"


def test_shared_agy_brain_tokens_are_deduplicated_in_catalog_and_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    p1 = hub.create("agy", "pro1")
    p2 = hub.create("agy", "pro2")

    for p in (p1, p2):
        (Path(p["path"]) / ".models").write_text("gemini-4-hydrogen-high\n", encoding="utf-8")

    # Canonical brain in pro1, symlinked from pro2 (shared history)
    shared_brain = Path(p1["path"]) / ".gemini" / "antigravity-cli" / "brain"
    sess_dir = shared_brain / "conv-1" / ".system_generated" / "logs"
    sess_dir.mkdir(parents=True)
    (sess_dir / "transcript.jsonl").write_text(
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-25T10:00:00Z",
                "model": "gemini-4-hydrogen-high",
                "input_tokens": 1000,
                "output_tokens": 500,
                "content": "Готово",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    p2_agy_dir = Path(p2["path"]) / ".gemini" / "antigravity-cli"
    p2_agy_dir.mkdir(parents=True, exist_ok=True)
    p2_brain = p2_agy_dir / "brain"
    if p2_brain.exists() and not p2_brain.is_symlink():
        p2_brain.rmdir()
    create_link(shared_brain, p2_brain)

    rows = catalog_models(hub, provider="agy")
    hydro = next(r for r in rows if r["model"] == "gemini-4-hydrogen-high")
    assert hydro["total_tokens"] == 1500
    assert hydro["total_profiles"] == 2

    assert main(["--data-dir", str(tmp_path / "hub"), "accounts", "agy"]) == 0
    out = capsys.readouterr().out
    assert "общие" in out


def test_group_catalog_models_collapses_effort_variants() -> None:
    raw = [
        {
            "provider": "agy",
            "model": "gemini-4-carbon-low",
            "display_name": "Gemini 4 Carbon (Low)",
            "family": "gemini",
            "profiles_ready": 2,
            "profiles_total": 3,
            "best_remaining_percent": 81.9,
            "total_tokens": 100,
            "providers": ["agy"],
        },
        {
            "provider": "agy",
            "model": "gemini-4-carbon-medium",
            "display_name": "Gemini 4 Carbon (Medium)",
            "family": "gemini",
            "profiles_ready": 2,
            "profiles_total": 3,
            "best_remaining_percent": 81.9,
            "total_tokens": 500,
            "providers": ["agy"],
        },
        {
            "provider": "agy",
            "model": "gemini-4-carbon-high",
            "display_name": "Gemini 4 Carbon (High)",
            "family": "gemini",
            "profiles_ready": 2,
            "profiles_total": 3,
            "best_remaining_percent": 81.9,
            "total_tokens": 2000,
            "providers": ["agy"],
        },
    ]
    grouped_high = group_catalog_models(raw, preferred_effort="high")
    assert len(grouped_high) == 1
    assert grouped_high[0]["model"] == "gemini-4-carbon-high"
    assert grouped_high[0]["display_name"] == "Gemini 4 Carbon"
    assert grouped_high[0]["efforts"] == ["low", "medium", "high"]
    assert grouped_high[0]["total_tokens"] == 2600

    grouped_med = group_catalog_models(raw, preferred_effort="medium")
    assert grouped_med[0]["model"] == "gemini-4-carbon-medium"
    assert grouped_med[0]["selected_effort"] == "medium"


async def test_tui_redesign_quotas_models_effort_sessions_and_shortcuts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    p1 = hub.create("agy", "pro1")
    p2 = hub.create("agy", "ind1")
    hub.create("codex", "acc2")

    (Path(p1["path"]) / ".models").write_text(
        "gemini-4-carbon-low\ngemini-4-carbon-medium\ngemini-4-carbon-high\nclaude-opus-5-5-high\n",
        encoding="utf-8",
    )
    (Path(p2["path"]) / ".models").write_text(
        "gemini-4-hydrogen-medium\ngemini-4-hydrogen-high\nclaude-opus-4-6-thinking\n",
        encoding="utf-8",
    )
    _seed_agy_quota(hub, "pro1", gemini_pct=82.0, tp_5h_pct=100.0, tp_7d_pct=15.0)
    _seed_agy_quota(hub, "ind1", gemini_pct=100.0, tp_5h_pct=100.0, tp_7d_pct=90.0)
    _seed_codex_quota(hub, "acc2", pct_5h=95.0, pct_7d=40.0)

    # Seed an AGY session in pro1
    agy_base = Path(p1["path"]) / ".gemini" / "antigravity-cli"
    s_dir = agy_base / "brain" / "sess-agy-1"
    log_dir = s_dir / ".system_generated" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "transcript.jsonl").write_text(
        json.dumps(
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-11-25T10:00:00Z",
                "content": "Проверь новую вкладку диалогов",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (agy_base / "history.jsonl").write_text(
        json.dumps(
            {
                "display": "Проверь новую вкладку диалогов",
                "timestamp": 1795600000000,
                "workspace": str(Path.cwd()),
                "conversationId": "sess-agy-1",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    app = HarnessHubApp(hub, "agy")
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()

        # 1. AGY table has per-group columns and inspector shows both Gemini and Claude/GPT bars
        table = app.query_one("#profiles", DataTable)
        col_labels = [str(c.label) for c in table.columns.values()]
        assert "Gemini" in col_labels
        assert "Claude / GPT" in col_labels
        insp = str(app.query_one("#account-inspector", Static).render())
        assert "Gemini" in insp
        assert "Claude / GPT" in insp
        assert "82%" in insp

        # 2. Project input guard: pressing '/' focuses #project, typing 'q' does not quit
        proj_inp = app.query_one("#project", Input)
        orig_project = proj_inp.value
        await pilot.press("/")
        await pilot.pause()
        assert app.focused is proj_inp
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running
        proj_inp.value = orig_project
        # Move focus out and switch tabs via digit shortcuts 2, 3, 4, 1
        table.focus()
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "models-tab"

        # 3. Effort variants collapsed to 1 row per base model, switching effort updates slug
        carbon_rows = [r for r in app.registry_rows if r["base_model"] == "gemini-4-carbon"]
        assert len(carbon_rows) == 1
        assert carbon_rows[0]["model"] == "gemini-4-carbon-high"
        app.query_one("#effort-filter", Select).value = "medium"
        await pilot.pause()
        carbon_med = [r for r in app.registry_rows if r["base_model"] == "gemini-4-carbon"]
        assert carbon_med[0]["model"] == "gemini-4-carbon-medium"

        # 4. Sessions tab + ResumeProfileForm modal (#session-resume-as)
        await pilot.press("3")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "sessions-tab"
        assert app.query_one("#sessions-table", DataTable).row_count == 1
        await pilot.click("#session-resume-as")
        await pilot.pause()
        assert isinstance(app.screen, ResumeProfileForm)
        await pilot.press("escape")
        await pilot.pause()

        # 5. Settings tab via '4'
        await pilot.press("4")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "settings-tab"


async def test_tui_safe_exit_while_background_worker_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harnesshub.registry.sys.platform", "linux")
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    hub.create("codex", "acc2")

    started = asyncio.Event()

    def slow_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        import time

        started.set()
        time.sleep(0.05)
        return {"provider": "codex", "profile": "acc2", "plan": "Plus"}

    monkeypatch.setattr("harnesshub.tui.app.account_telemetry", slow_probe)
    app = HarnessHubApp(hub, "codex")
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        app.action_usage()
        await pilot.pause(0.02)
        # Immediately exit while worker is in flight — must not raise NoMatches/errors
        app.exit()
        await pilot.pause(0.08)
