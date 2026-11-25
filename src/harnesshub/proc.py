from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any


def terminate_process_tree(
    process: Any,
    *,
    os_mod: Any = os,
    subprocess_mod: Any = subprocess,
    shutil_mod: Any = shutil,
) -> None:
    """Terminate a spawned subprocess and its descendants cross-platform."""
    if os_mod.name != "nt":
        try:
            os_mod.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        taskkill = shutil_mod.which("taskkill.exe")
        if taskkill:
            try:
                subprocess_mod.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess_mod.DEVNULL,
                    stderr=subprocess_mod.DEVNULL,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess_mod.TimeoutExpired):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    try:
        process.communicate(timeout=5)
    except subprocess_mod.TimeoutExpired:
        if getattr(process, "stdout", None):
            process.stdout.close()
        process.wait(timeout=5)


def wrap_windows_command(
    command: list[str],
    *,
    os_name: str | None = None,
    shutil_mod: Any = shutil,
    environ: Any = None,
) -> list[str]:
    """Wrap .ps1/.cmd/.bat entrypoints cleanly on Windows so Popen never fails with WinError 193."""
    if not command or (os_name or os.name) != "nt":
        return list(command)
    env_map = os.environ if environ is None else environ
    head = command[0]
    lower = head.lower()
    if lower.endswith(".ps1"):
        pwsh = shutil_mod.which("pwsh") or shutil_mod.which("powershell") or "powershell.exe"
        return [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *command]
    if lower.endswith((".cmd", ".bat")):
        comspec = env_map.get("COMSPEC") or shutil_mod.which("cmd.exe") or "cmd.exe"
        return [comspec, "/d", "/c", *command]
    return list(command)


def detached_popen_kwargs(*, os_name: str | None = None, no_window: bool = False) -> dict[str, Any]:
    """Return cross-platform Popen kwargs to launch a detached GUI/background child process."""
    if (os_name or os.name) != "nt":
        return {"start_new_session": True}
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(
        subprocess, "DETACHED_PROCESS", 0x00000008
    )
    if no_window:
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": flags}


def hidden_popen_kwargs(*, os_name: str | None = None) -> dict[str, Any]:
    """Return Popen kwargs that suppress console windows for background probes on Windows."""
    if (os_name or os.name) != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def run_isolated_process(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
    merge_stderr: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run subprocess in an isolated session/tree and kill entire tree on timeout/interrupt."""
    launch_cmd = wrap_windows_command(command)
    extra_kwargs: dict[str, Any] = (
        {"start_new_session": True} if os.name != "nt" else hidden_popen_kwargs()
    )
    process = subprocess.Popen(
        launch_cmd,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **extra_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        terminate_process_tree(process)
        raise
    return subprocess.CompletedProcess(
        args=command,
        returncode=process.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )
