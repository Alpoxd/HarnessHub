from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from harnesshub.agy import AgyMaintenance
from harnesshub.consult import consult_peers
from harnesshub.errors import HubError
from harnesshub.installation import Installer
from harnesshub.models import model_cli_args

if TYPE_CHECKING:
    from harnesshub.service import Hub


@dataclass
class Request:
    action: str
    provider: str = ""
    profile: str | None = None
    native: bool = False
    cwd: str = ""
    model: str = ""
    session_id: str = ""
    note: str = ""
    council: bool = False


def execute_request(hub: Hub, request: Request) -> str:
    """Execute a foreground TUI request while the terminal is suspended or released."""
    try:
        if request.action in ("run", "login"):
            code = hub.launch(
                request.provider,
                request.profile,
                native=request.native,
                login=request.action == "login",
                cwd=Path(request.cwd),
            )
            return f"{request.provider}: клиент завершился с кодом {code}"
        if request.action == "run-model":
            assert request.profile is not None
            code = hub.launch(
                request.provider,
                request.profile,
                args=model_cli_args(request.provider, request.model),
                cwd=Path(request.cwd),
            )
            return (
                f"{request.provider}/{request.profile} ({request.model}): завершился с кодом {code}"
            )
        if request.action == "resume":
            code = hub.resume_session(
                request.session_id,
                target_profile=request.profile,
                cwd=Path(request.cwd),
            )
            return f"Диалог {request.session_id[:12]} завершён (код {code})"
        if request.action == "handoff":
            code = hub.handoff_session(
                request.session_id,
                target_model=request.model,
                note=request.note,
                cwd=Path(request.cwd),
            )
            return f"Handoff в {request.model} завершён (код {code})"
        if request.action == "consult":
            print("Запрашиваю второе мнение с контекстом задачи…", flush=True)
            res = consult_peers(
                hub,
                request.note,
                models=[request.model] if request.model else None,
                council=request.council,
                cwd=Path(request.cwd),
            )
            for op in res["opinions"]:
                header = (
                    f"\n=== {op['provider']}/{op['profile']} · {op['model']} ({op['status']}) ==="
                )
                print(header, flush=True)
                print(op.get("response") or op.get("error") or "", flush=True)
            try:
                input("\nНажми Enter для возврата в HarnessHub…")
            except (EOFError, KeyboardInterrupt):
                pass
            return f"Получено мнений: {len(res['opinions'])}"
        if request.action == "install":
            plan = Installer(hub).plan(request.provider, request.profile)
            print(f"Загружаю оригинал {plan['name']} · {plan['source']}…", flush=True)
            result = Installer(hub).install(request.provider, request.profile)
            return (
                f"{request.provider}: установлен"
                if result["status"] == "installed"
                else f"Установщик скачан: {result['path']}"
            )
        action = request.action.removeprefix("agy-")
        print(f"AGY: {action}…", flush=True)
        result = getattr(AgyMaintenance(hub), action)()
        return json.dumps(result, ensure_ascii=False)
    except (HubError, OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    except (KeyboardInterrupt, EOFError):
        return "Операция прервана"


def run_tui(hub: Hub) -> int:
    from harnesshub.tui.app import HarnessHubApp

    provider: str | None = None
    message = ""
    while True:
        try:
            request = HarnessHubApp(hub, provider, message, interactive=True).run()
        except KeyboardInterrupt:
            return 0
        if request is None:
            return 0
        provider = request.provider or provider
        message = execute_request(hub, request)
