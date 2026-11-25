from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesshub.errors import BusyError
from harnesshub.locking import FileLock, inspect_lock_state, resource_lock
from harnesshub.providers.agy import read_agy_profile_models
from harnesshub.providers.base import atomic_write_json, atomic_write_text, safe_json_read

if TYPE_CHECKING:
    from harnesshub.service import Hub

SHARED_HISTORY_DIRS: tuple[str, ...] = ("conversations", "brain", "annotations", "knowledge")
SHARED_HISTORY_FILES: tuple[str, ...] = (
    "history.jsonl",
    "conversation_summaries.db",
    "jetbox_summaries_proto.pb",
)


def _posix_passwd_home() -> Path | None:
    if sys.platform == "win32":
        return None
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir).expanduser().resolve()
    except (ImportError, KeyError, OSError):
        return None


def resolve_real_home(hub: Hub | None = None, explicit_home: Path | None = None) -> Path:
    """Resolve the user's real home directory, even when launched inside an AGY profile."""
    if explicit_home is not None:
        return explicit_home.expanduser().resolve()
    override = os.environ.get("HARNESSHUB_USER_HOME")
    if override and override.strip():
        return Path(override).expanduser().resolve()
    if hub is not None:
        configured = hub.store.read()["settings"].get("agy_shared_home")
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser().resolve()

    env_home = Path.home().expanduser().resolve()
    pw_home = _posix_passwd_home()
    if pw_home is None or env_home == pw_home:
        return env_home

    agy_profiles_root = Path(os.environ.get("AGY_PROFILES", pw_home / "agy-profiles")).resolve()
    if env_home == agy_profiles_root or env_home.is_relative_to(agy_profiles_root):
        return pw_home
    if hub is not None:
        managed_root = (hub.store.root / "profiles").resolve()
        if env_home.is_relative_to(managed_root):
            return pw_home
        for prof in hub.profiles("agy"):
            if prof["name"] == "main":
                continue
            try:
                if env_home == Path(prof["path"]).expanduser().resolve():
                    return pw_home
            except OSError:
                continue
    return env_home


def _is_agy_profile_busy(hub: Hub, profile_path: Path) -> bool:
    res_locked, res_active = inspect_lock_state(resource_lock(profile_path, hub.lock_root))
    leg_locked, leg_active = inspect_lock_state(profile_path / ".lock")
    if res_locked or res_active or leg_locked or leg_active:
        return True
    if not sys.platform.startswith("linux"):
        return False
    target = os.path.realpath(profile_path)
    my_pid = str(os.getpid())
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return False
    for entry in proc_entries:
        if not entry.isdigit() or entry == my_pid:
            continue
        try:
            comm = Path(f"/proc/{entry}/comm").read_text(encoding="utf-8", errors="ignore").strip()
            if comm != "agy":
                continue
            raw_env = Path(f"/proc/{entry}/environ").read_bytes()
            for item in raw_env.split(b"\0"):
                if item.startswith(b"HOME="):
                    val = item.split(b"=", 1)[1].decode("utf-8", errors="ignore")
                    if os.path.realpath(val) == target:
                        return True
        except OSError:
            continue
    return False


def _merge_sqlite_db(src_db: Path, dst_db: Path) -> None:
    if os.path.realpath(src_db) == os.path.realpath(dst_db):
        return
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(dst_db, timeout=3.0)
        con.execute("ATTACH DATABASE ? AS src_db", (str(src_db),))
        dst_tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM main.sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        src_tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM src_db.sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        for table in sorted(dst_tables & src_tables):
            t_esc = table.replace('"', '""')
            dst_cols = [
                row[1] for row in con.execute(f'PRAGMA main.table_info("{t_esc}")').fetchall()
            ]
            src_cols = {
                row[1] for row in con.execute(f'PRAGMA src_db.table_info("{t_esc}")').fetchall()
            }
            cols = [c for c in dst_cols if c in src_cols]
            if not cols:
                continue
            cols_sql = ", ".join(f'"{c.replace(chr(34), chr(34) * 2)}"' for c in cols)
            con.execute(
                f'INSERT OR IGNORE INTO main."{t_esc}" ({cols_sql}) '
                f'SELECT {cols_sql} FROM src_db."{t_esc}"'
            )
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        if con is not None:
            try:
                con.execute("DETACH DATABASE src_db")
            except sqlite3.Error:
                pass
            try:
                con.close()
            except sqlite3.Error:
                pass


def _copy_or_merge_sqlite(src_db: Path, dst_db: Path) -> bool:
    if os.path.realpath(src_db) == os.path.realpath(dst_db):
        return False
    if not dst_db.exists():
        dst_db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with (
                sqlite3.connect(src_db, timeout=3.0) as src_con,
                sqlite3.connect(dst_db, timeout=3.0) as dst_con,
            ):
                src_con.backup(dst_con)
            dst_db.chmod(0o600)
            return True
        except sqlite3.Error:
            shutil.copy2(src_db, dst_db)
            return True
    _merge_sqlite_db(src_db, dst_db)
    return True


def _ensure_shared_base(shared_base: Path) -> None:
    shared_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    for sub in SHARED_HISTORY_DIRS:
        (shared_base / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    (shared_base / "cache").mkdir(mode=0o700, parents=True, exist_ok=True)
    hist = shared_base / "history.jsonl"
    if not hist.exists():
        fd = os.open(hist, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)


def _merge_profile_history_into_shared(prof_base: Path, shared_base: Path) -> dict[str, int]:
    _ensure_shared_base(shared_base)
    copied_files = 0
    merged_history_lines = 0
    if prof_base.resolve() == shared_base.resolve():
        return {"copied_files": 0, "merged_history_lines": 0}

    for sub in SHARED_HISTORY_DIRS:
        local_dir = prof_base / sub
        target_root = shared_base / sub
        if not local_dir.is_dir() or local_dir.is_symlink():
            continue
        for root_dir, _, files in os.walk(local_dir, followlinks=False):
            rel = os.path.relpath(root_dir, local_dir)
            dest_dir = target_root if rel == "." else target_root / rel
            dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            for fn in files:
                sfile = Path(root_dir) / fn
                if sfile.is_symlink() or not sfile.is_file():
                    continue
                tfile = dest_dir / fn
                try:
                    if not tfile.exists() or sfile.stat().st_size > tfile.stat().st_size:
                        shutil.copy2(sfile, tfile)
                        copied_files += 1
                except OSError:
                    continue

    local_hist = prof_base / "history.jsonl"
    shared_hist = shared_base / "history.jsonl"
    if (
        local_hist.is_file()
        and not local_hist.is_symlink()
        and local_hist.stat().st_size > 0
        and os.path.realpath(local_hist) != os.path.realpath(shared_hist)
    ):
        existing_lines: set[str] = set()
        if shared_hist.is_file():
            existing_lines = {
                line.rstrip("\r\n")
                for line in shared_hist.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            }
        extra: list[str] = []
        for line in local_hist.read_text(encoding="utf-8", errors="replace").splitlines():
            clean = line.rstrip("\r\n")
            if clean.strip() and clean not in existing_lines:
                existing_lines.add(clean)
                extra.append(clean + "\n")
        if extra:
            with shared_hist.open("a", encoding="utf-8") as stream:
                stream.writelines(extra)
            merged_history_lines += len(extra)

    local_db = prof_base / "conversation_summaries.db"
    shared_db = shared_base / "conversation_summaries.db"
    if local_db.is_file() and not local_db.is_symlink() and local_db.stat().st_size > 0:
        _copy_or_merge_sqlite(local_db, shared_db)

    local_pb = prof_base / "jetbox_summaries_proto.pb"
    shared_pb = shared_base / "jetbox_summaries_proto.pb"
    if (
        local_pb.is_file()
        and not local_pb.is_symlink()
        and local_pb.stat().st_size > 0
        and (not shared_pb.exists() or local_pb.stat().st_size > shared_pb.stat().st_size)
    ):
        shutil.copy2(local_pb, shared_pb)
        copied_files += 1

    local_meta = prof_base / "cache" / "conversation_metadata.json"
    shared_meta = shared_base / "cache" / "conversation_metadata.json"
    if local_meta.is_file() and not local_meta.is_symlink():
        local_data = safe_json_read(local_meta)
        shared_data = safe_json_read(shared_meta)
        if isinstance(local_data, dict) and isinstance(local_data.get("conversations"), dict):
            base_payload = shared_data if isinstance(shared_data, dict) else {}
            merged_convs = dict(base_payload.get("conversations") or {})
            changed = False
            for cid, entry in local_data["conversations"].items():
                if cid not in merged_convs:
                    merged_convs[cid] = entry
                    changed = True
            if changed or not shared_meta.exists():
                base_payload["conversations"] = merged_convs
                atomic_write_json(shared_meta, base_payload)

    return {"copied_files": copied_files, "merged_history_lines": merged_history_lines}


def _ensure_symlink(src: Path, dst: Path) -> None:
    if os.path.islink(dst):
        try:
            if Path(os.readlink(dst)) == src:
                return
        except OSError:
            pass
        dst.unlink()
    elif dst.exists():
        if dst.is_dir():
            if dst.resolve() == src.resolve():
                return
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    dst.symlink_to(src)


def ensure_agy_profile_shared_history(
    hub: Hub, profile: dict[str, Any], *, home: Path | None = None
) -> str:
    """Merge local AGY history into the canonical store and symlink profile artifacts."""
    prof_path = Path(profile["path"]).expanduser().resolve()
    prof_base = prof_path / ".gemini" / "antigravity-cli"
    shared_base = (resolve_real_home(hub, home) / ".gemini" / "antigravity-cli").resolve()
    _ensure_shared_base(shared_base)
    if prof_base.resolve() == shared_base:
        return "canonical"

    prof_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _is_agy_profile_busy(hub, prof_path):
        _merge_profile_history_into_shared(prof_base, shared_base)
        return "deferred_busy"

    try:
        with FileLock(resource_lock(prof_path, hub.lock_root), shared=False):
            _merge_profile_history_into_shared(prof_base, shared_base)
            for sub in SHARED_HISTORY_DIRS:
                _ensure_symlink(shared_base / sub, prof_base / sub)

            _ensure_symlink(shared_base / "history.jsonl", prof_base / "history.jsonl")

            for fn in ("conversation_summaries.db", "jetbox_summaries_proto.pb"):
                src = shared_base / fn
                dst = prof_base / fn
                if fn == "conversation_summaries.db":
                    for wal_suffix in ("-wal", "-shm"):
                        side = prof_base / f"{fn}{wal_suffix}"
                        if side.exists() or side.is_symlink():
                            side.unlink()
                if src.exists():
                    _ensure_symlink(src, dst)

            shared_meta = shared_base / "cache" / "conversation_metadata.json"
            if shared_meta.is_file():
                (prof_base / "cache").mkdir(mode=0o700, parents=True, exist_ok=True)
                _ensure_symlink(shared_meta, prof_base / "cache" / "conversation_metadata.json")
    except BusyError:
        return "deferred_busy"
    return "linked"


def agy_history_status(hub: Hub, *, home: Path | None = None) -> dict[str, Any]:
    state = hub.store.read()
    shared_home = resolve_real_home(hub, home)
    shared_base = (shared_home / ".gemini" / "antigravity-cli").resolve()
    rows: list[dict[str, str]] = []
    for prof in hub.profiles("agy"):
        prof_path = Path(prof["path"]).expanduser().resolve()
        prof_base = prof_path / ".gemini" / "antigravity-cli"
        if prof_base.resolve() == shared_base:
            mode = "canonical"
        else:
            links = []
            for sub in SHARED_HISTORY_DIRS:
                target = prof_base / sub
                if target.is_symlink() and os.path.realpath(target) == str(shared_base / sub):
                    links.append(True)
                elif target.exists() or target.is_symlink():
                    links.append(False)
            hist = prof_base / "history.jsonl"
            if hist.is_symlink() and os.path.realpath(hist) == str(shared_base / "history.jsonl"):
                links.append(True)
            elif hist.exists() or hist.is_symlink():
                links.append(False)

            if links and all(links):
                mode = "shared"
            elif any(links):
                mode = "partial"
            else:
                mode = "isolated"
        rows.append({"profile": prof["name"], "path": str(prof_path), "mode": mode})

    return {
        "enabled": bool(state["settings"].get("agy_shared_history", False)),
        "shared_home": str(shared_home),
        "shared_base": str(shared_base),
        "automode": hub.automode_enabled("agy"),
        "profiles": rows,
    }


def set_agy_shared_history(
    hub: Hub, enabled: bool, *, home: Path | None = None, copy_local: bool = False
) -> dict[str, Any]:
    real_home = resolve_real_home(hub, home)
    shared_base = (real_home / ".gemini" / "antigravity-cli").resolve()

    def change(state: dict[str, Any]) -> None:
        state["settings"]["agy_shared_history"] = enabled
        if home is not None:
            state["settings"]["agy_shared_home"] = str(real_home)

    hub.store.update(change)

    if enabled:
        linked: list[str] = []
        canonical: list[str] = []
        deferred_busy: list[str] = []
        for prof in hub.profiles("agy"):
            res = ensure_agy_profile_shared_history(hub, prof, home=real_home)
            if res == "linked":
                linked.append(prof["name"])
            elif res == "canonical":
                canonical.append(prof["name"])
            else:
                deferred_busy.append(prof["name"])
        return {
            "enabled": True,
            "shared_base": str(shared_base),
            "linked": linked,
            "canonical": canonical,
            "deferred_busy": deferred_busy,
        }

    unlinked: list[str] = []
    canonical = []
    deferred_busy = []
    for prof in hub.profiles("agy"):
        prof_path = Path(prof["path"]).expanduser().resolve()
        prof_base = prof_path / ".gemini" / "antigravity-cli"
        if prof_base.resolve() == shared_base:
            canonical.append(prof["name"])
            continue
        if _is_agy_profile_busy(hub, prof_path):
            deferred_busy.append(prof["name"])
            continue
        try:
            with FileLock(resource_lock(prof_path, hub.lock_root), shared=False):
                for sub in SHARED_HISTORY_DIRS:
                    dst = prof_base / sub
                    src = shared_base / sub
                    if dst.is_symlink():
                        dst.unlink()
                        if copy_local and src.is_dir():
                            shutil.copytree(src, dst, dirs_exist_ok=True)
                        else:
                            dst.mkdir(mode=0o700, parents=True, exist_ok=True)
                for fn in SHARED_HISTORY_FILES:
                    dst = prof_base / fn
                    src = shared_base / fn
                    if dst.is_symlink():
                        dst.unlink()
                        if copy_local and src.is_file():
                            shutil.copy2(src, dst)
                        elif fn == "history.jsonl":
                            fd = os.open(dst, os.O_WRONLY | os.O_CREAT, 0o600)
                            os.close(fd)
                meta_dst = prof_base / "cache" / "conversation_metadata.json"
                meta_src = shared_base / "cache" / "conversation_metadata.json"
                if meta_dst.is_symlink():
                    meta_dst.unlink()
                    if copy_local and meta_src.is_file():
                        shutil.copy2(meta_src, meta_dst)
                unlinked.append(prof["name"])
        except BusyError:
            deferred_busy.append(prof["name"])

    return {
        "enabled": False,
        "shared_base": str(shared_base),
        "unlinked": unlinked,
        "canonical": canonical,
        "deferred_busy": deferred_busy,
    }


def _adapted_settings_for_profile(
    src_settings: Path, prof_base: Path, tier: str
) -> tuple[bool, dict[str, Any] | str]:
    raw_text = src_settings.read_text(encoding="utf-8", errors="replace")
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return False, raw_text
    if not isinstance(parsed, dict):
        return False, raw_text
    cfg = dict(parsed)
    if tier in ("3.8+4.6", "g4h+4.6", "g4-hydro"):
        existing = safe_json_read(prof_base / "settings.json")
        existing_model = existing.get("model") if isinstance(existing, dict) else None
        if isinstance(existing_model, str) and existing_model and "Gemini 4" not in existing_model:
            cfg["model"] = existing_model
        elif isinstance(cfg.get("model"), str) and "Gemini 4" in cfg["model"]:
            cfg["model"] = "Gemini 3.8 Flash (High)"
        orch = cfg.get("orchestrator")
        if orch is not None and "Gemini 4" in json.dumps(orch, ensure_ascii=False):
            cfg.pop("orchestrator", None)
    return True, cfg


def sync_agy_profiles(hub: Hub, *, home: Path | None = None) -> dict[str, Any]:
    """Sync AGY settings.json, GEMINI.md rules, .agents skills and shared caches to profiles."""
    src_home = resolve_real_home(hub, home)
    src_base = src_home / ".gemini" / "antigravity-cli"
    src_settings = src_base / "settings.json"
    src_rules = src_home / ".gemini" / "config" / "GEMINI.md"
    src_agents = src_home / ".agents"
    src_gitconfig = src_home / ".gitconfig"
    src_playwright = src_home / ".cache" / "ms-playwright-go"

    shared_enabled = bool(hub.store.read()["settings"].get("agy_shared_history", False))
    synced: list[str] = []
    canonical: list[str] = []
    deferred_busy: list[str] = []

    for prof in hub.profiles("agy"):
        prof_path = Path(prof["path"]).expanduser().resolve()
        if prof_path == src_home:
            canonical.append(prof["name"])
            continue
        if _is_agy_profile_busy(hub, prof_path):
            deferred_busy.append(prof["name"])
            continue
        try:
            with FileLock(resource_lock(prof_path, hub.lock_root), shared=False):
                prof_base = prof_path / ".gemini" / "antigravity-cli"
                prof_base.mkdir(mode=0o700, parents=True, exist_ok=True)
                (prof_path / ".gemini" / "config").mkdir(mode=0o700, parents=True, exist_ok=True)
                (prof_path / ".cache").mkdir(mode=0o700, parents=True, exist_ok=True)

                if src_settings.is_file():
                    _, tier = read_agy_profile_models(prof_path)
                    is_json, payload = _adapted_settings_for_profile(src_settings, prof_base, tier)
                    if is_json and isinstance(payload, dict):
                        atomic_write_json(prof_base / "settings.json", payload)
                    else:
                        atomic_write_text(prof_base / "settings.json", str(payload), mode=0o600)

                link_targets = (
                    (src_rules, prof_path / ".gemini" / "config" / "GEMINI.md"),
                    (src_agents, prof_path / ".agents"),
                    (src_gitconfig, prof_path / ".gitconfig"),
                    (src_playwright, prof_path / ".cache" / "ms-playwright-go"),
                )
                for src_item, dst_item in link_targets:
                    if src_item.exists() and (not dst_item.exists() or dst_item.is_symlink()):
                        _ensure_symlink(src_item, dst_item)
                synced.append(prof["name"])
        except BusyError:
            deferred_busy.append(prof["name"])
            continue

        if shared_enabled:
            ensure_agy_profile_shared_history(hub, prof, home=src_home)

    return {
        "source_home": str(src_home),
        "synced": synced,
        "canonical": canonical,
        "deferred_busy": deferred_busy,
        "shared_history": shared_enabled,
    }
