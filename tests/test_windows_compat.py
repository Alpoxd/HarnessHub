from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harnesshub.cli import _configure_console_utf8
from harnesshub.errors import HubError
from harnesshub.ide import IDE_TARGETS, find_ide_executable, launch_ide
from harnesshub.installation import Installer
from harnesshub.proc import detached_popen_kwargs, hidden_popen_kwargs, wrap_windows_command
from harnesshub.providers.base import (
    atomic_replace,
    create_link,
    cwd_matches,
    same_path_or_file,
    unlink_link,
)
from harnesshub.service import Hub


def test_wrap_windows_command_and_popen_kwargs() -> None:
    fake_shutil = SimpleNamespace(
        which=lambda name: f"C:\\Windows\\System32\\{name}" if name in ("pwsh", "cmd.exe") else None
    )
    assert wrap_windows_command(["codex.cmd", "--help"], os_name="posix") == [
        "codex.cmd",
        "--help",
    ]
    assert wrap_windows_command(
        [r"C:\tools\codex.cmd", "--help"],
        os_name="nt",
        shutil_mod=fake_shutil,
        environ={"COMSPEC": r"C:\Windows\System32\cmd.exe"},
    ) == [r"C:\Windows\System32\cmd.exe", "/d", "/c", r"C:\tools\codex.cmd", "--help"]
    assert wrap_windows_command(
        [r"C:\tools\install.ps1", "-Verbose"],
        os_name="nt",
        shutil_mod=fake_shutil,
    ) == [
        r"C:\Windows\System32\pwsh",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        r"C:\tools\install.ps1",
        "-Verbose",
    ]
    assert wrap_windows_command([r"C:\tools\agy.exe", "--version"], os_name="nt") == [
        r"C:\tools\agy.exe",
        "--version",
    ]

    assert detached_popen_kwargs(os_name="posix") == {"start_new_session": True}
    nt_detached = detached_popen_kwargs(os_name="nt")
    assert "start_new_session" not in nt_detached
    assert nt_detached["creationflags"] & 0x00000200
    assert nt_detached["creationflags"] & 0x00000008

    assert hidden_popen_kwargs(os_name="posix") == {}
    assert hidden_popen_kwargs(os_name="nt")["creationflags"] == 0x08000000


def test_agy_profile_creation_and_environment_on_win32(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr("harnesshub.locking._win32_try_lock", lambda _fd, *, shared=False: True)
    monkeypatch.setattr("harnesshub.locking._win32_unlock", lambda _fd: None)
    monkeypatch.setattr("harnesshub.registry.sys.platform", "win32")
    monkeypatch.setattr("harnesshub.service.sys.platform", "win32")
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    assert hub.harness("agy").profile_supported() is True

    profile = hub.create("agy", "winacc")
    prof_path = Path(profile["path"])
    assert (prof_path / ".gemini" / "antigravity-cli").is_dir()
    assert (prof_path / "AppData" / "Roaming").is_dir()
    assert (prof_path / "AppData" / "Local").is_dir()

    env = hub.environment("agy", profile)
    assert env["HOME"] == str(prof_path)
    assert env["USERPROFILE"] == str(prof_path)
    assert env["APPDATA"] == str(prof_path / "AppData" / "Roaming")
    assert env["LOCALAPPDATA"] == str(prof_path / "AppData" / "Local")
    assert "DBUS_SESSION_BUS_ADDRESS" not in env


def test_find_ide_executable_checks_windows_localappdata_and_launch_uses_creationflags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app = tmp_path / "LocalAppData"
    code_cmd = local_app / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"
    code_cmd.parent.mkdir(parents=True)
    code_cmd.write_text("@echo off\n", encoding="utf-8")

    monkeypatch.setattr("harnesshub.ide.shutil.which", lambda _cmd: None)
    found = find_ide_executable(
        IDE_TARGETS["vscode"],
        os_name="nt",
        environ={"LOCALAPPDATA": str(local_app)},
    )
    assert found == str(code_cmd)

    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    monkeypatch.setattr("harnesshub.ide.find_ide_executable", lambda _spec: str(code_cmd))
    monkeypatch.setattr(
        "harnesshub.ide.detached_popen_kwargs", lambda: {"creationflags": 0x00000208}
    )
    monkeypatch.setattr(
        "harnesshub.ide.wrap_windows_command",
        lambda cmd: ["cmd.exe", "/d", "/c", *cmd],
    )
    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 4242

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr("harnesshub.ide.subprocess.Popen", fake_popen)
    res = launch_ide(hub, "vscode", cwd=tmp_path)
    assert res["status"] == "launched"
    assert res["pid"] == 4242
    assert captured["cmd"][:3] == ["cmd.exe", "/d", "/c"]
    assert captured["kwargs"]["creationflags"] == 0x00000208
    assert "start_new_session" not in captured["kwargs"]


def test_create_link_windows_fallbacks_to_junction_and_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src_dir = tmp_path / "src_dir"
    src_dir.mkdir()
    (src_dir / "keep.txt").write_text("safe", encoding="utf-8")
    dst_dir = tmp_path / "dst_dir"

    src_file = tmp_path / "src.txt"
    src_file.write_text("hello", encoding="utf-8")
    dst_file = tmp_path / "dst.txt"

    # Simulate Windows without Developer Mode (symlink_to raises WinError 1314)
    def deny_symlink(self: Path, target: Path, target_is_directory: bool = False) -> None:
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", deny_symlink)

    mklink_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        mklink_calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="Junction created", stderr="")

    kind_dir = create_link(
        src_dir, dst_dir, os_name="nt", subprocess_mod=SimpleNamespace(run=fake_run)
    )
    assert kind_dir == "junction"
    assert mklink_calls[0][1:5] == ["/d", "/c", "mklink", "/J"]

    kind_file = create_link(src_file, dst_file, os_name="nt")
    assert kind_file == "hardlink"
    assert same_path_or_file(src_file, dst_file)
    assert dst_file.read_text(encoding="utf-8") == "hello"

    # unlink_link on a Windows junction uses os.rmdir, never deleting target directory contents
    junction_dir = tmp_path / "fake_junction"
    junction_dir.mkdir()
    rmdir_called: list[Path] = []
    monkeypatch.setattr("harnesshub.providers.base.is_link_or_junction", lambda *_a, **_kw: True)
    monkeypatch.setattr("harnesshub.providers.base.os.rmdir", lambda p: rmdir_called.append(p))
    unlink_link(junction_dir, os_name="nt")
    assert rmdir_called == [junction_dir]
    assert (src_dir / "keep.txt").read_text(encoding="utf-8") == "safe"


def test_atomic_replace_retries_permission_error_on_nt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "new.tmp"
    dst = tmp_path / "state.json"
    src.write_text("updated", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")

    real_replace = os.replace
    attempts = 0

    def flaky_replace(a: Any, b: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("sharing violation")
        real_replace(a, b)

    monkeypatch.setattr("harnesshub.providers.base.os.replace", flaky_replace)
    monkeypatch.setattr("harnesshub.providers.base.time.sleep", lambda _s: None)
    atomic_replace(src, dst, retries=4, os_name="nt")
    assert attempts == 3
    assert dst.read_text(encoding="utf-8") == "updated"


def test_npm_shebang_node_launcher_and_urllib_fallback_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    installer = Installer(hub)
    monkeypatch.setattr(installer, "_npm_launcher", lambda: (sys.executable, "mock-npm-cli.js"))
    monkeypatch.setattr(installer, "_check_node", lambda *a: None)

    def fake_npm_run(command: list[str], env: dict[str, str], cwd: Path, timeout: int = 900) -> str:
        packages = cwd / ("node_modules" if os.name == "nt" else "lib/node_modules")
        pkg = packages / "@openai/codex"
        bin_file = pkg / "bin" / "codex"
        bin_file.parent.mkdir(parents=True)
        bin_file.write_text("#!/usr/bin/env node\nconsole.log('ok')\n", encoding="utf-8")
        (pkg / "package.json").write_text(
            json.dumps({"version": "2.0.0", "bin": {"codex": "bin/codex"}}),
            encoding="utf-8",
        )
        return ""

    monkeypatch.setattr(installer, "_run", fake_npm_run)
    result = installer.install("codex")
    assert result["launcher"] == [sys.executable, result["executable"]]

    # _download_script_urllib rejects non-HTTPS URLs
    with pytest.raises(HubError, match="HTTPS"):
        installer._download_script_urllib("http://insecure.example/install.ps1", tmp_path / "x", {})


def test_configure_console_utf8_on_win32_and_cwd_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, str]] = []

    class _FakeStream:
        def reconfigure(self, **kwargs: str) -> None:
            calls.append(kwargs)

    monkeypatch.setattr("harnesshub.cli.sys.platform", "win32")
    monkeypatch.setattr("harnesshub.cli.sys.stdout", _FakeStream())
    monkeypatch.setattr("harnesshub.cli.sys.stderr", _FakeStream())
    _configure_console_utf8()
    assert calls == [
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
    ]

    repo = tmp_path / "Repo"
    sub = repo / "Sub"
    sub.mkdir(parents=True)
    assert cwd_matches(str(sub), str(repo))
