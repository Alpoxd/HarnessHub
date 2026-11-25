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


def run_isolated_process(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
    merge_stderr: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run subprocess in an isolated session/tree and kill entire tree on timeout/interrupt."""
    process = subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name != "nt",
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
