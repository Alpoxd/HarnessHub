from __future__ import annotations

import concurrent.futures
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesshub.errors import BusyError, HubError
from harnesshub.locking import FileLock, resource_lock
from harnesshub.models import model_family, normalize_model, rank_candidates
from harnesshub.proc import run_isolated_process
from harnesshub.sessions import git_workspace_summary, list_sessions, read_session

if TYPE_CHECKING:
    from harnesshub.service import Hub

DEFAULT_FAMILY_REPRESENTATIVES: dict[str, tuple[str, ...]] = {
    "gemini": ("gemini-4-argon-high", "gemini-4-hydrogen-high", "gemini-3.8-flash-high"),
    "gpt": ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"),
    "claude": ("claude-opus-5-5-high", "claude-opus-5-5", "claude-sonnet-5-5-high"),
}


def check_recursion_guard() -> None:
    val = os.environ.get("AI_CONSULTATION", "")
    if val and val != "0" and os.environ.get("HARNESSHUB_ALLOW_NESTED_CONSULT") != "1":
        raise HubError(
            "Вложенные консультации заблокированы (AI_CONSULTATION=1). "
            "Консультант должен отвечать самостоятельно."
        )


def build_consultation_prompt(
    hub: Hub,
    question: str,
    *,
    cwd: Path | None = None,
    session_id: str | None = None,
    include_session: bool = True,
    include_git: bool = True,
    max_turns: int = 6,
) -> tuple[str, dict[str, Any]]:
    workspace = (cwd or Path.cwd()).expanduser().resolve()
    context_meta: dict[str, Any] = {
        "cwd": str(workspace),
        "session_id": None,
        "git_included": False,
    }
    sections: list[str] = [
        "Ты выступаешь независимым экспертом-консультантом "
        "(второе мнение другого семейства моделей).",
        "Проанализируй задачу и контекст ниже, укажи на узкие места, риски, баги "
        "или предложи более чистое решение.",
        "",
    ]

    if include_session:
        chosen_session: dict[str, Any] | None = None
        if session_id:
            chosen_session = read_session(hub, session_id)
        else:
            recent = list_sessions(hub, cwd=workspace, limit=1)
            if recent:
                try:
                    chosen_session = read_session(hub, recent[0]["session_id"])
                except HubError:
                    chosen_session = None
        if chosen_session and chosen_session.get("messages"):
            context_meta["session_id"] = chosen_session["session_id"]
            context_meta["session_source"] = (
                f"{chosen_session['provider']}/{chosen_session['profile']}"
            )
            sections.append(
                f"## Контекст текущего диалога "
                f"({chosen_session['provider']}/{chosen_session['profile']}, "
                f"ID `{chosen_session['session_id']}`)"
            )
            for msg in chosen_session["messages"][-max_turns:]:
                role = "Пользователь" if msg["role"] == "user" else "Текущий агент"
                sections.append(f"### {role}\n{msg['text'][:1400]}\n")

    if include_git and workspace.is_dir():
        git_summary = git_workspace_summary(workspace)
        if git_summary:
            context_meta["git_included"] = True
            sections.extend(["## Состояние репозитория", git_summary, ""])

    sections.extend(["## Вопрос / Задача для экспертизы", question.strip()])
    return "\n".join(sections), context_meta


def headless_command(
    hub: Hub,
    provider: str,
    profile: dict[str, Any],
    model: str,
    prompt: str,
    cwd: Path,
) -> list[str]:
    spec = hub.harness(provider)
    executable = spec.executable()
    if executable is None:
        raise HubError(f"{spec.name} не найден в PATH")
    base = list(spec.launcher) if spec.launcher else [executable]

    if provider == "agy":
        return [
            *base,
            "--mode",
            "plan",
            "--add-dir",
            str(cwd),
            "--model",
            model,
            "-p",
            prompt,
        ]
    if provider == "codex":
        return [
            *base,
            "-c",
            'cli_auth_credentials_store="file"',
            "exec",
            "-C",
            str(cwd),
            "-s",
            "read-only",
            "-m",
            model,
            prompt,
        ]
    if provider == "claude":
        return [
            *base,
            "--permission-mode",
            "plan",
            "--disallowed-tools",
            "Edit,Write,NotebookEdit",
            "--model",
            model,
            "-p",
            prompt,
        ]
    return [*base, "--model", model, "-p", prompt]


def select_peer_targets(
    hub: Hub,
    *,
    models: list[str] | None = None,
    from_family: str | None = None,
    council: bool = False,
) -> list[dict[str, Any]]:
    requested_models: list[str] = []
    if models:
        requested_models.extend(models)
    elif council:
        for fam in ("gemini", "gpt", "claude"):
            if from_family and fam == from_family.lower():
                continue
            requested_models.append(DEFAULT_FAMILY_REPRESENTATIVES[fam][0])
    elif from_family:
        excluded = from_family.lower()
        for fam in ("gpt", "claude", "gemini"):
            if fam != excluded:
                requested_models.append(DEFAULT_FAMILY_REPRESENTATIVES[fam][0])
                break
    else:
        requested_models.append("gemini-4-argon-high")

    selected: list[dict[str, Any]] = []
    used_profiles: set[tuple[str, str]] = set()
    for req in requested_models:
        ranked = rank_candidates(hub, req)
        if not ranked and req in DEFAULT_FAMILY_REPRESENTATIVES.get(model_family(req), ()):
            for fallback in DEFAULT_FAMILY_REPRESENTATIVES.get(model_family(req), ()):
                ranked = rank_candidates(hub, fallback)
                if ranked:
                    break
        if not ranked:
            raise HubError(f"Нет зарегистрированных профилей, поддерживающих модель {req!r}")
        unlocked = [c for c in ranked if not c["locked"]]
        if not unlocked:
            raise BusyError(f"Все профили для модели {req!r} сейчас заняты эксклюзивной операцией")
        chosen = next(
            (c for c in unlocked if (c["provider"], c["profile"]) not in used_profiles),
            unlocked[0],
        )
        used_profiles.add((chosen["provider"], chosen["profile"]))
        selected.append(chosen)
    return selected


def _run_single_consultation(
    hub: Hub,
    candidate: dict[str, Any],
    prompt: str,
    cwd: Path,
    timeout: float,
    dry_run: bool,
) -> dict[str, Any]:
    provider = candidate["provider"]
    profile_name = candidate["profile"]
    resolved_model = candidate["resolved_model"]
    profile = hub.profile(provider, profile_name)
    cmd = headless_command(hub, provider, profile, resolved_model, prompt, cwd)
    if dry_run:
        return {
            "provider": provider,
            "profile": profile_name,
            "model": resolved_model,
            "family": model_family(resolved_model),
            "status": "dry-run",
            "command": cmd,
            "response": "",
        }
    env = hub.environment(provider, profile)
    env["AI_CONSULTATION"] = "1"
    started = time.time()
    try:
        with FileLock(resource_lock(Path(profile["path"]), hub.lock_root), shared=True):
            completed = run_isolated_process(
                cmd, env=env, cwd=cwd, timeout=timeout, merge_stderr=False
            )
    except BusyError as exc:
        return {
            "provider": provider,
            "profile": profile_name,
            "model": resolved_model,
            "family": model_family(resolved_model),
            "status": "error",
            "elapsed_sec": round(time.time() - started, 2),
            "error": str(exc),
            "response": "",
        }
    except subprocess.TimeoutExpired:
        return {
            "provider": provider,
            "profile": profile_name,
            "model": resolved_model,
            "family": model_family(resolved_model),
            "status": "timeout",
            "elapsed_sec": round(time.time() - started, 2),
            "error": f"Превышен таймаут ({timeout:g}с)",
            "response": "",
        }
    except OSError as exc:
        return {
            "provider": provider,
            "profile": profile_name,
            "model": resolved_model,
            "family": model_family(resolved_model),
            "status": "error",
            "elapsed_sec": round(time.time() - started, 2),
            "error": str(exc),
            "response": "",
        }

    ok = completed.returncode == 0
    return {
        "provider": provider,
        "profile": profile_name,
        "model": resolved_model,
        "family": model_family(resolved_model),
        "status": "ok" if ok else "error",
        "returncode": completed.returncode,
        "elapsed_sec": round(time.time() - started, 2),
        "response": completed.stdout.strip(),
        "error": completed.stderr.strip() if not ok else "",
    }


def consult_peers(
    hub: Hub,
    question: str,
    *,
    models: list[str] | None = None,
    from_family: str | None = None,
    council: bool = False,
    cwd: Path | None = None,
    session_id: str | None = None,
    include_session: bool = True,
    include_git: bool = True,
    timeout: float = 180.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not dry_run:
        check_recursion_guard()
    if not question or not question.strip():
        raise HubError("Укажи вопрос или задачу для консультации")

    workspace = (cwd or Path.cwd()).expanduser().resolve()
    targets = select_peer_targets(
        hub,
        models=[normalize_model(m, root=hub.store.root) for m in models] if models else None,
        from_family=from_family,
        council=council,
    )
    prompt, context_meta = build_consultation_prompt(
        hub,
        question,
        cwd=workspace,
        session_id=session_id,
        include_session=include_session,
        include_git=include_git,
    )

    if len(targets) == 1:
        opinions = [_run_single_consultation(hub, targets[0], prompt, workspace, timeout, dry_run)]
    else:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(targets))
        try:
            futures = [
                pool.submit(
                    _run_single_consultation, hub, target, prompt, workspace, timeout, dry_run
                )
                for target in targets
            ]
            opinions = [f.result() for f in futures]
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)

    return {
        "question": question.strip(),
        "context": context_meta,
        "opinions": opinions,
    }
