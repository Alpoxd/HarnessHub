from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any

from harnesshub.errors import HubError
from harnesshub.proc import hidden_popen_kwargs, wrap_windows_command


class JsonRpcStdioClient:
    """Cross-platform JSON-RPC 2.0 line-delimited stdio client using a reader thread."""

    def __init__(self, command: list[str], *, env: dict[str, str]):
        self.command = command
        self.env = env
        self.proc: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._reader: threading.Thread | None = None

    def __enter__(self) -> JsonRpcStdioClient:
        self.proc = subprocess.Popen(
            wrap_windows_command(self.command),
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            **hidden_popen_kwargs(),
        )
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader.start()
        return self

    def _pump_stdout(self) -> None:
        assert self.proc is not None
        stdout = self.proc.stdout
        if stdout is None:
            self._lines.put(None)
            return
        try:
            while True:
                line = stdout.readline()
                if not line:
                    break
                self._lines.put(line)
        except OSError:
            pass
        finally:
            self._lines.put(None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        req_id: int | str | None = None,
        timeout: float = 12.0,
        optional: bool = False,
        timeout_error: str | None = None,
        rpc_error_prefix: str = "",
    ) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdin is not None
        rid: int | str = method if req_id is None else req_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = self._lines.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if self.proc.poll() is not None and self._lines.empty():
                    break
                continue
            if raw is None:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(response, dict) or response.get("id") != rid:
                continue
            if "error" in response:
                if optional:
                    return {}
                msg = rpc_error_prefix or f"RPC ошибка для {method}"
                raise HubError(msg)
            result = response.get("result")
            return result if isinstance(result, dict) else {}

        if optional:
            return {}
        raise HubError(timeout_error or f"Запрос {method} не ответил вовремя")

    def __exit__(self, *_: object) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.5)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
                except Exception:
                    pass
