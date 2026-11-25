from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesshub.errors import HubError
from harnesshub.models import model_cli_args, normalize_model
from harnesshub.providers import ADAPTERS
from harnesshub.providers.agy import (
    agy_base_dir,
    collect_agy_candidates,
    parse_agy_messages,
    parse_agy_session,
    read_agy_conv_metadata,
    read_agy_history,
)
from harnesshub.providers.base import BoundedCache, cwd_matches
from harnesshub.providers.claude import (
    collect_claude_candidates,
    parse_claude_messages,
    parse_claude_session,
)
from harnesshub.providers.codex import (
    collect_codex_candidates,
    collect_codex_sqlite_entries,
    parse_codex_messages,
    parse_codex_session,
)

if TYPE_CHECKING:
    from harnesshub.service import Hub

USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)

_SESSION_META_CACHE: BoundedCache[str, tuple[int, int, dict[str, Any] | None]] = BoundedCache(512)
_HISTORY_CACHE: BoundedCache[str, tuple[int, int, dict[str, dict[str, Any]]]] = BoundedCache(128)
_AGY_CONV_META_CACHE: BoundedCache[str, tuple[int, int, dict[str, dict[str, Any]]]] = BoundedCache(
    128
)


def _clean_user_text(text: str) -> str:
    if not text:
        return ""
    match = USER_REQ_RE.search(text)
    if match:
        text = match.group(1)
    return text.strip()


def _parse_messages_from_file(provider: str, transcript_path: Path) -> list[dict[str, Any]]:
    adapter = ADAPTERS.get(provider)
    if adapter is not None:
        return adapter.parse_messages(transcript_path)
    if provider == "agy":
        return parse_agy_messages(transcript_path, _clean_user_text)
    if provider == "claude":
        return parse_claude_messages(transcript_path, _clean_user_text)
    if provider == "codex":
        return parse_codex_messages(transcript_path, _clean_user_text)
    return []


def _session_matches_query(entry: dict[str, Any], q_norm: str) -> bool:
    if not q_norm:
        return True
    haystack = " ".join(
        [
            str(entry.get("title") or ""),
            str(entry.get("cwd") or ""),
            str(entry.get("model") or ""),
            str(entry.get("session_id") or ""),
            str(entry.get("provider") or ""),
            str(entry.get("profile") or ""),
        ]
    ).lower()
    return q_norm in haystack


def list_sessions(
    hub: Hub,
    cwd: Path | None = None,
    provider: str | None = None,
    profile: str | None = None,
    *,
    query: str = "",
    limit: int = 50,
    include_subagents: bool = False,
) -> list[dict[str, Any]]:
    profiles = hub.profiles(provider)
    if profile is not None:
        profiles = [p for p in profiles if p["name"] == profile]
    profiles = sorted(
        profiles, key=lambda p: (0 if p.get("default") else 1, p["provider"], p["name"])
    )
    target_cwd = str(cwd.expanduser().resolve()) if cwd is not None else None
    q_norm = query.strip().lower()

    sqlite_entries: list[dict[str, Any]] = []
    candidates: list[tuple[Any, ...]] = []
    for prof in profiles:
        prov = prof["provider"]
        pname = prof["name"]
        ppath = Path(prof["path"])
        if not ppath.is_dir():
            continue
        if prov == "agy":
            candidates.extend(
                collect_agy_candidates(
                    pname,
                    ppath,
                    target_cwd=target_cwd,
                    history_cache=_HISTORY_CACHE,
                    conv_meta_cache=_AGY_CONV_META_CACHE,
                )
            )
        elif prov == "claude":
            candidates.extend(collect_claude_candidates(pname, ppath))
        elif prov == "codex":
            s_entries, covered = collect_codex_sqlite_entries(
                pname, ppath, include_subagents=include_subagents
            )
            sqlite_entries.extend(s_entries)
            candidates.extend(collect_codex_candidates(pname, ppath, covered_paths=covered))

    candidates.sort(key=lambda item: item[0], reverse=True)

    results: list[dict[str, Any]] = []
    exact: list[dict[str, Any]] = []
    no_cwd: list[dict[str, Any]] = []
    seen_realpaths: set[str] = set()

    def _accept(entry: dict[str, Any]) -> None:
        real = os.path.realpath(entry["transcript_path"])
        if real in seen_realpaths:
            return
        seen_realpaths.add(real)
        if not _session_matches_query(entry, q_norm):
            return
        if target_cwd is None:
            results.append(entry)
        else:
            scwd = entry["cwd"]
            if cwd_matches(scwd, target_cwd):
                exact.append(entry)
            elif not scwd:
                no_cwd.append(entry)

    for s_entry in sqlite_entries:
        _accept(s_entry)

    max_inspect = (limit * 2) if (target_cwd is None and not q_norm) else max(limit * 6, 200)
    for inspected, (st_mtime_ns, st_size, st_mtime, prov, pname, payload) in enumerate(candidates):
        if inspected >= max_inspect:
            break
        if target_cwd is None and not q_norm and not sqlite_entries and len(results) >= limit:
            break
        entry: dict[str, Any] | None = None
        if prov == "agy":
            cid, transcript, meta, cmeta = payload
            entry = parse_agy_session(
                pname,
                cid,
                transcript,
                st_mtime_ns,
                st_size,
                st_mtime,
                meta,
                cmeta,
                clean_user_text=_clean_user_text,
                meta_cache=_SESSION_META_CACHE,
            )
        elif prov == "claude":
            entry = parse_claude_session(
                pname,
                payload,
                st_mtime_ns,
                st_size,
                st_mtime,
                clean_user_text=_clean_user_text,
                meta_cache=_SESSION_META_CACHE,
            )
        elif prov == "codex":
            entry = parse_codex_session(
                pname,
                payload,
                st_mtime_ns,
                st_size,
                st_mtime,
                clean_user_text=_clean_user_text,
                meta_cache=_SESSION_META_CACHE,
                include_subagents=include_subagents,
            )
        else:
            entry = None

        if entry is None:
            continue
        _accept(entry)

    if target_cwd is not None:
        results = exact if exact else no_cwd
    results.sort(key=lambda s: s["updated_at"], reverse=True)
    return results[:limit]


def _find_session_direct(
    hub: Hub,
    session_id: str,
    provider: str | None = None,
    profile: str | None = None,
) -> dict[str, Any] | None:
    profiles = hub.profiles(provider)
    if profile is not None:
        profiles = [p for p in profiles if p["name"] == profile]
    for prof in profiles:
        prov = prof["provider"]
        pname = prof["name"]
        ppath = Path(prof["path"])
        if not ppath.is_dir():
            continue
        if prov == "agy":
            base = agy_base_dir(ppath)
            transcript = (
                base / "brain" / session_id / ".system_generated" / "logs" / "transcript.jsonl"
            )
            if transcript.is_file():
                try:
                    st = transcript.stat()
                except OSError:
                    continue
                history_map = read_agy_history(base / "history.jsonl", _HISTORY_CACHE)
                conv_meta_map = read_agy_conv_metadata(
                    base / "cache" / "conversation_metadata.json", _AGY_CONV_META_CACHE
                )
                entry = parse_agy_session(
                    pname,
                    session_id,
                    transcript,
                    st.st_mtime_ns,
                    st.st_size,
                    st.st_mtime,
                    history_map.get(session_id, {}),
                    conv_meta_map.get(session_id, {}),
                    clean_user_text=_clean_user_text,
                    meta_cache=_SESSION_META_CACHE,
                )
                if entry is not None:
                    return entry
        elif prov == "claude":
            projects_dir = ppath / "projects"
            if not projects_dir.is_dir() and (ppath / ".claude" / "projects").is_dir():
                projects_dir = ppath / ".claude" / "projects"
            if projects_dir.is_dir():
                for jsonl in projects_dir.glob(f"*/{session_id}.jsonl"):
                    try:
                        st = jsonl.stat()
                    except OSError:
                        continue
                    entry = parse_claude_session(
                        pname,
                        jsonl,
                        st.st_mtime_ns,
                        st.st_size,
                        st.st_mtime,
                        clean_user_text=_clean_user_text,
                        meta_cache=_SESSION_META_CACHE,
                    )
                    if entry is not None:
                        return entry
        elif prov == "codex":
            s_entries, _ = collect_codex_sqlite_entries(pname, ppath, include_subagents=True)
            for se in s_entries:
                if se["session_id"] == session_id or se["session_id"].startswith(session_id):
                    return se
            sessions_dir = ppath / "sessions"
            if not sessions_dir.is_dir() and (ppath / ".codex" / "sessions").is_dir():
                sessions_dir = ppath / ".codex" / "sessions"
            if sessions_dir.is_dir():
                for jsonl in sessions_dir.glob(f"*/*/*/*{session_id}*.jsonl"):
                    try:
                        st = jsonl.stat()
                    except OSError:
                        continue
                    entry = parse_codex_session(
                        pname,
                        jsonl,
                        st.st_mtime_ns,
                        st.st_size,
                        st.st_mtime,
                        clean_user_text=_clean_user_text,
                        meta_cache=_SESSION_META_CACHE,
                        include_subagents=True,
                    )
                    if entry is not None and (
                        entry["session_id"] == session_id
                        or entry["session_id"].startswith(session_id)
                    ):
                        return entry
    return None


def read_session(
    hub: Hub,
    session_id: str,
    provider: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    match = _find_session_direct(hub, session_id, provider=provider, profile=profile)
    if match is None:
        sessions = list_sessions(
            hub, cwd=None, provider=provider, profile=profile, limit=200, include_subagents=True
        )
        match = next(
            (
                s
                for s in sessions
                if s["session_id"] == session_id or s["session_id"].startswith(session_id)
            ),
            None,
        )
    if match is None:
        raise HubError(f"Диалог {session_id!r} не найден в зарегистрированных профилях")
    messages = _parse_messages_from_file(match["provider"], Path(match["transcript_path"]))
    return {
        **match,
        "messages": messages,
    }


def git_workspace_summary(cwd: Path, max_diff_chars: int = 4000) -> str:
    if not cwd.is_dir():
        return ""
    try:
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if status.returncode != 0:
            return ""
        diff_stat = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        parts = []
        if status.stdout.strip():
            parts.append(f"### Git Status\n```\n{status.stdout.strip()[:2000]}\n```")
        if diff_stat.stdout.strip():
            parts.append(
                f"### Git Diff Stat\n```\n{diff_stat.stdout.strip()[:max_diff_chars]}\n```"
            )
        return "\n\n".join(parts)
    except (OSError, subprocess.SubprocessError):
        return ""


def build_handoff_prompt(
    hub: Hub,
    session_id: str,
    *,
    note: str = "",
    max_messages: int = 10,
    max_chars_per_message: int = 1600,
    include_git: bool = True,
    cwd: Path | None = None,
) -> tuple[dict[str, Any], str]:
    session = read_session(hub, session_id)
    messages = session["messages"]
    effective_cwd = cwd or (Path(session["cwd"]) if session["cwd"] else Path.cwd())

    lines = [
        "# Контекст переданной сессии (HarnessHub Session Handoff)",
        f"- Источник: `{session['provider']}/{session['profile']}` (модель `{session['model']}`)",
        f"- ID исходного диалога: `{session['session_id']}`",
        f"- Рабочий каталог: `{effective_cwd}`",
        "",
    ]
    if messages:
        first_msg = messages[0]
        lines.extend(
            [
                "## Исходная постановка задачи",
                first_msg["text"][:max_chars_per_message],
                "",
            ]
        )
        tail = messages[1:][-max_messages:] if len(messages) > 1 else []
        if tail:
            lines.append("## Последние шаги диалога")
            for msg in tail:
                role_label = "Пользователь" if msg["role"] == "user" else "Ассистент"
                snippet = msg["text"]
                if len(snippet) > max_chars_per_message:
                    snippet = snippet[:max_chars_per_message] + "\n...[сокращено]..."
                lines.append(f"### {role_label}\n{snippet}\n")

    if include_git and effective_cwd.is_dir():
        git_info = git_workspace_summary(effective_cwd)
        if git_info:
            lines.extend(["## Состояние репозитория", git_info, ""])

    lines.extend(
        [
            "## Текущее задание",
            note.strip()
            if note.strip()
            else "Продолжи решение задачи с учётом переданного контекста выше.",
        ]
    )
    return session, "\n".join(lines)


def resume_cli_args(provider: str, session_id: str, model: str | None = None) -> tuple[str, ...]:
    m_args = model_cli_args(provider, normalize_model(model)) if model else ()
    if provider == "codex":
        return (*m_args, "resume", session_id)
    if provider in ("agy", "claude"):
        return (*m_args, "--resume", session_id)
    raise HubError(f"Продолжение сессии по ID пока не поддерживается для {provider}")


def handoff_cli_args(
    target_provider: str, handoff_prompt: str, model: str | None = None
) -> tuple[str, ...]:
    m_args = model_cli_args(target_provider, normalize_model(model)) if model else ()
    if target_provider == "agy":
        return (*m_args, "-i", handoff_prompt)
    if target_provider in ("claude", "codex"):
        return (*m_args, handoff_prompt)
    return (*m_args, handoff_prompt)
