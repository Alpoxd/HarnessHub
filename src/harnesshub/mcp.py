from __future__ import annotations

import concurrent.futures
import json
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from harnesshub import __version__
from harnesshub.consult import consult_peers
from harnesshub.errors import HubError
from harnesshub.models import catalog_models, rank_candidates
from harnesshub.sessions import build_handoff_prompt, list_sessions

if TYPE_CHECKING:
    from harnesshub.service import Hub

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "consult_peer",
        "description": (
            "Запросить второе мнение у модели другого семейства (Gemini 4 Argon/Hydrogen, "
            "OpenAI Codex GPT-6 Astra/Sol, Claude Opus/Sonnet 5.5) с автоматическим выбором "
            "свободного аккаунта по квотам и передачей контекста текущей сессии и git diff."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Вопрос или задача для эксперта второго мнения",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Целевая модель или алиас (argon, hydro, astra, sol, opus, sonnet)"
                    ),
                },
                "from_family": {
                    "type": "string",
                    "description": (
                        "Текущее семейство (gemini, gpt, claude), "
                        "чтобы автоматически выбрать другое"
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": "Необязательный ID диалога для подгрузки контекста",
                },
                "include_session": {
                    "type": "boolean",
                    "description": (
                        "Включить контекст последнего диалога проекта (по умолчанию true)"
                    ),
                },
                "include_git": {
                    "type": "boolean",
                    "description": "Включить git status и diff stat (по умолчанию true)",
                },
                "cwd": {
                    "type": "string",
                    "description": "Путь к рабочему каталогу проекта",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "consult_council",
        "description": (
            "Параллельный консилиум нескольких семейств моделей (Gemini + GPT + Claude) "
            "с контекстом текущей задачи и git diff."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Вопрос или архитектурная развилка для консилиума",
                },
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Необязательный список моделей (по умолчанию argon + sol + opus)"
                    ),
                },
                "include_session": {"type": "boolean"},
                "include_git": {"type": "boolean"},
                "cwd": {"type": "string"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "list_models_and_quotas",
        "description": (
            "Показать каталог доступных моделей во всех зарегистрированных аккаунтах "
            "или отсортировать профили по остатку квоты для конкретной модели."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": (
                        "Если указана, возвращает ранжированный список аккаунтов для этой модели"
                    ),
                },
                "provider": {
                    "type": "string",
                    "description": "Необязательный фильтр по harness (agy, codex, claude)",
                },
            },
        },
    },
    {
        "name": "list_project_sessions",
        "description": (
            "Единая история диалогов проекта по всем harness (AGY, Claude Code, Codex) "
            "и генерация Handoff-контекста для переноса сессии между моделями."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Каталог проекта"},
                "provider": {"type": "string", "description": "Фильтр по harness"},
                "handoff_session_id": {
                    "type": "string",
                    "description": (
                        "Если указан ID сессии, возвращает готовый Handoff-бриф этого диалога"
                    ),
                },
            },
        },
    },
]


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    if key not in args or args[key] is None:
        return None
    val = args[key]
    if not isinstance(val, str):
        raise HubError(f"Параметр {key} должен быть строкой")
    return val


def _opt_bool(args: dict[str, Any], key: str, default: bool) -> bool:
    if key not in args or args[key] is None:
        return default
    val = args[key]
    if not isinstance(val, bool):
        raise HubError(f"Параметр {key} должен быть булевым")
    return val


def _opt_models_list(args: dict[str, Any], key: str) -> list[str] | None:
    if key not in args or args[key] is None:
        return None
    val = args[key]
    if not isinstance(val, list) or any(not isinstance(m, str) or not m.strip() for m in val):
        raise HubError(f"Параметр {key} должен быть массивом непустых строк")
    return val


def _handle_single_request(hub: Hub, request: Any) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    method = request.get("method")
    req_id = request.get("id")
    if not isinstance(method, str):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    raw_params = request.get("params")
    if raw_params is not None and not isinstance(raw_params, dict):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": "Invalid params"},
        }
    params: dict[str, Any] = raw_params or {}

    if method == "initialize":
        proto = params.get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": proto if isinstance(proto, str) and proto else "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "harnesshub", "version": __version__},
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments")
        if args is None:
            args = {}
        try:
            if not isinstance(name, str) or not name:
                raise HubError("Не указано имя MCP-инструмента")
            if not isinstance(args, dict):
                raise HubError("Поле arguments должно быть объектом")
            payload = _call_mcp_tool(hub, name, args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
                    ],
                    "isError": False,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Ошибка: {exc}"}],
                    "isError": True,
                },
            }
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def handle_mcp_request(hub: Hub, request: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(request, list):
        if not request:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
        responses: list[dict[str, Any]] = []
        for item in request:
            res = _handle_single_request(hub, item)
            if res is not None:
                responses.append(res)
        return responses or None
    return _handle_single_request(hub, request)


def _call_mcp_tool(
    hub: Hub, name: str, args: dict[str, Any]
) -> dict[str, Any] | list[dict[str, Any]]:
    if name == "consult_peer":
        question = _opt_str(args, "question") or ""
        model = _opt_str(args, "model")
        from_family = _opt_str(args, "from_family")
        cwd_raw = _opt_str(args, "cwd")
        session_id = _opt_str(args, "session_id")
        include_session = _opt_bool(args, "include_session", True)
        include_git = _opt_bool(args, "include_git", True)
        return consult_peers(
            hub,
            question,
            models=[model] if model else None,
            from_family=from_family,
            cwd=Path(cwd_raw) if cwd_raw else None,
            session_id=session_id,
            include_session=include_session,
            include_git=include_git,
        )
    if name == "consult_council":
        question = _opt_str(args, "question") or ""
        models = _opt_models_list(args, "models")
        cwd_raw = _opt_str(args, "cwd")
        include_session = _opt_bool(args, "include_session", True)
        include_git = _opt_bool(args, "include_git", True)
        return consult_peers(
            hub,
            question,
            models=models,
            council=True,
            cwd=Path(cwd_raw) if cwd_raw else None,
            include_session=include_session,
            include_git=include_git,
        )
    if name == "list_models_and_quotas":
        model = _opt_str(args, "model")
        provider = _opt_str(args, "provider")
        if model:
            return rank_candidates(hub, model, provider)
        return catalog_models(hub, provider)
    if name == "list_project_sessions":
        cwd_raw = _opt_str(args, "cwd")
        cwd = Path(cwd_raw) if cwd_raw else None
        handoff_id = _opt_str(args, "handoff_session_id")
        if handoff_id:
            session, prompt = build_handoff_prompt(hub, handoff_id, cwd=cwd)
            return {"session": session, "handoff_prompt": prompt}
        return list_sessions(hub, cwd=cwd, provider=_opt_str(args, "provider"))
    raise HubError(f"Неизвестный MCP-инструмент: {name}")


def _write_json_line(out: TextIO, write_lock: threading.Lock, payload: Any) -> None:
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with write_lock:
        out.write(line)
        out.flush()


def serve_mcp(hub: Hub, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    write_lock = threading.Lock()
    saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    pending: set[concurrent.futures.Future[None]] = set()
    try:

        def _process_async(req: Any) -> None:
            response = handle_mcp_request(hub, req)
            if response is not None:
                _write_json_line(out, write_lock, response)

        for line in inp:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                request = json.loads(stripped)
            except json.JSONDecodeError:
                _write_json_line(
                    out,
                    write_lock,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                )
                continue

            # Run slow tool calls concurrently so ping/tools/list never stall
            is_tool_call = (
                isinstance(request, dict) and request.get("method") == "tools/call"
            ) or (
                isinstance(request, list)
                and any(isinstance(r, dict) and r.get("method") == "tools/call" for r in request)
            )
            if is_tool_call:
                pending = {f for f in pending if not f.done()}
                fut = pool.submit(_process_async, request)
                pending.add(fut)
            else:
                response = handle_mcp_request(hub, request)
                if response is not None:
                    _write_json_line(out, write_lock, response)

        for fut in list(pending):
            fut.result()
    finally:
        pool.shutdown(wait=True)
        sys.stdout = saved_stdout
    return 0
