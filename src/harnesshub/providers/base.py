from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from platformdirs import user_data_path, user_state_path

from harnesshub.catalog_loader import MODEL_ALIASES
from harnesshub.locking import inspect_lock_state, resource_lock
from harnesshub.proc import hidden_popen_kwargs

if TYPE_CHECKING:
    from harnesshub.service import Hub

_K = TypeVar("_K")
_V = TypeVar("_V")

SYNTHETIC_CODEX_PREFIXES = (
    "# AGENTS.md",
    "<environment_context",
    "<permissions",
    "<subagent_notification",
    "<collaboration_mode",
)
MAX_FULL_TRANSCRIPT_BYTES = 2 * 1024 * 1024


class BoundedCache(Generic[_K, _V]):
    """Thread-safe bounded LRU cache."""

    def __init__(self, max_entries: int = 512):
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._data: OrderedDict[_K, _V] = OrderedDict()

    def get(self, key: _K) -> _V | None:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def set(self, key: _K, value: _V) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


@dataclass
class QuotaWindow:
    window: str
    label: str
    remaining_percent: float
    used_percent: float | None = None
    minutes: int | None = None
    resets_at: int | float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TokenStats:
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    recent_7d_tokens: int | None = None
    peak_daily_tokens: int | None = None
    sessions_count: int | None = None
    messages_count: int | None = None
    by_model: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AccountTelemetry:
    provider: str
    profile: str
    path: str
    locked: bool
    active: bool
    logged_in: bool
    account_label: str | None
    plan: str | None
    plan_detail: str
    plan_expires_at: float | None
    plan_expires_iso: str | None
    plan_expired: bool
    tier: str
    quota_source: str
    checked_at: float | None
    in_cooldown: bool
    spent_buckets: dict[str, float]
    remaining_percent: float | None
    resets_at: float | None
    windows: list[dict[str, Any]]
    model_quotas: dict[str, dict[str, Any]]
    reset_credits: int
    models: list[dict[str, Any]]
    locked_models: list[dict[str, Any]]
    models_source: str
    models_cached: bool
    tokens: dict[str, Any]
    rate_limit_tier: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.provider != "claude":
            data.pop("rate_limit_tier", None)
        return data


@dataclass
class SessionSummary:
    session_id: str
    provider: str
    profile: str
    cwd: str
    project_name: str
    model: str
    title: str
    turns: int | None
    tokens_used: int | None
    updated_at: float
    updated_at_iso: str
    transcript_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderAdapter(Protocol):
    provider_id: str

    def build_telemetry(self, hub: Hub, profile: dict[str, Any], now: float) -> dict[str, Any]: ...

    def probe_telemetry(
        self, hub: Hub, profile_name: str, timeout: float = ...
    ) -> dict[str, Any]: ...

    def parse_messages(self, transcript_path: Path) -> list[dict[str, Any]]: ...


def model_family(slug: str) -> str:
    cleaned = slug.strip().lower()
    if cleaned.startswith("gemini-"):
        return "gemini"
    if cleaned.startswith("claude-"):
        return "claude"
    if cleaned.startswith(("gpt-", "codex-", "o1", "o3", "o4")):
        return "gpt"
    return "other"


def quota_bucket(slug: str) -> str:
    cleaned = slug.strip().lower()
    cleaned = MODEL_ALIASES.get(cleaned, cleaned)
    if cleaned.startswith(("gemini-4-neon", "gemini-pro-orchestrator-neon")):
        return "neon"
    if cleaned in ("gemini-4-carbon", "gemini-pro-orchestrator") or cleaned.startswith(
        "gemini-4-carbon"
    ):
        return "carbon"
    if cleaned.startswith(("gemini-4-argon", "gemini-pro-orchestrator")):
        return "argon"
    if cleaned.startswith("gemini-4-hydrogen"):
        return "hydrogen"
    if cleaned.startswith("gemini-3.8-flash"):
        return "flash"
    if cleaned.startswith("claude-opus"):
        return "opus"
    if cleaned.startswith("claude-sonnet"):
        return "sonnet"
    if cleaned.startswith("gpt-oss"):
        return "gpt-oss"
    if cleaned.startswith(("gpt-", "codex-")):
        return "codex"
    return cleaned.rsplit("-", 1)[0]


def classify_agy_tier(models: list[str]) -> str:
    if not models:
        return "unknown"
    model_set = set(models)
    if "gemini-4-hydrogen-high" in model_set and "claude-opus-5-5-high" in model_set:
        return "g4+5.5"
    if "gemini-4-argon-high" in model_set and "claude-opus-5-5-high" in model_set:
        return "argon+5.5"
    if "gemini-4-neon-high" in model_set or "gemini-4-argon-high" in model_set:
        return "g4"
    if "gemini-4-hydrogen-high" in model_set:
        return "g4h+4.6" if "claude-opus-4-6-thinking" in model_set else "g4-hydro"
    if "gemini-3.8-flash-high" in model_set:
        return "3.8+4.6"
    return "custom"


def ai_ask_state_dirs(hub: Hub) -> list[Path]:
    dirs = [hub.store.root / "quotas"]
    default_root = user_data_path("harnesshub", appauthor=False).expanduser().resolve()
    if hub.store.root == default_root:
        xdg = os.environ.get("XDG_STATE_HOME")
        if xdg:
            dirs.append(Path(xdg).expanduser() / "ai-ask")
        dirs.append(Path.home() / ".local" / "state" / "ai-ask")
        dirs.append(user_state_path("ai-ask", appauthor=False))
    seen: set[str] = set()
    unique: list[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def profile_lock_state(hub: Hub, profile: dict[str, Any]) -> tuple[bool, bool]:
    """Return (exclusive_locked, active_session) for a profile directory."""
    profile_path = Path(profile["path"])
    res_locked, res_active = inspect_lock_state(resource_lock(profile_path, hub.lock_root))
    leg_locked, leg_active = inspect_lock_state(profile_path / ".lock")
    return res_locked or leg_locked, res_active or leg_active


def is_profile_locked(hub: Hub, profile: dict[str, Any]) -> bool:
    return profile_lock_state(hub, profile)[0]


def iso_to_epoch(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num / 1000.0 if num > 1e11 else num
    text = str(value).strip()
    if not text:
        return None
    try:
        num = float(text)
        return num / 1000.0 if num > 1e11 else num
    except ValueError:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def iso_to_epoch_or_zero(value: str | int | float | None) -> float:
    return iso_to_epoch(value) or 0.0


def epoch_to_date(ts: float | None) -> str | None:
    if not ts or ts <= 0:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def epoch_to_iso(ts: float) -> str:
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_json_read(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, (dict, list)):
            return data
        return None
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None


def atomic_replace(src: Path, dst: Path, *, retries: int = 4, os_name: str | None = None) -> None:
    """Atomically replace dst with src, retrying briefly on Windows Defender/sharing locks."""
    is_nt = (os_name or os.name) == "nt"
    attempts = retries if is_nt else 1
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not is_nt or attempt >= attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        tmp_path.chmod(mode)
        atomic_replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def is_link_or_junction(path: Path, *, os_name: str | None = None) -> bool:
    """Return True for POSIX/Windows symlinks as well as Windows NTFS directory junctions."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    if (os_name or os.name) == "nt":
        isjunction = getattr(os.path, "isjunction", None)
        if callable(isjunction):
            try:
                if isjunction(path):
                    return True
            except OSError:
                return False
        try:
            st = os.lstat(path)
            attrs = getattr(st, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
            return bool(attrs & reparse)
        except OSError:
            return False
    return False


def unlink_link(path: Path, *, os_name: str | None = None) -> None:
    """Safely remove a symlink or NTFS junction without touching target directory contents."""
    if (os_name or os.name) == "nt" and path.is_dir() and is_link_or_junction(path, os_name="nt"):
        os.rmdir(path)
        return
    path.unlink()


def same_path_or_file(a: Path, b: Path) -> bool:
    """Return True if two paths refer to the same file/dir (symlink, junction, or hardlink)."""
    try:
        if os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b)):
            return True
    except OSError:
        pass
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def create_link(
    src: Path,
    dst: Path,
    *,
    os_name: str | None = None,
    subprocess_mod: Any = subprocess,
) -> str:
    """Link dst -> src cross-platform (symlink; Windows falls back to NTFS junction/hardlink)."""
    is_nt = (os_name or os.name) == "nt"
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    is_dir = src.is_dir()
    if not is_nt:
        dst.symlink_to(src)
        return "symlink"
    try:
        dst.symlink_to(src, target_is_directory=is_dir)
        return "symlink"
    except OSError:
        if is_dir:
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            completed = subprocess_mod.run(
                [comspec, "/d", "/c", "mklink", "/J", str(dst), str(src)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                **hidden_popen_kwargs(os_name="nt"),
            )
            if completed.returncode == 0:
                return "junction"
            raise OSError(
                f"Не удалось создать NTFS junction {dst} -> {src}: "
                f"{(completed.stderr or completed.stdout or '').strip()}"
            ) from None
        os.link(src, dst)
        return "hardlink"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def read_label_file(profile_path: Path) -> str | None:
    lf = profile_path / "label"
    if not lf.is_file():
        return None
    try:
        val = lf.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not val or val.startswith("("):
        return None
    return val


def decode_jwt_claims(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or "." not in token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        data = json.loads(decoded.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def empty_tokens() -> dict[str, Any]:
    return TokenStats().to_dict()


def strip_file_uri(uri: str) -> str:
    if not uri.startswith("file://"):
        return uri
    parsed = urlsplit(uri)
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        return url2pathname(f"//{parsed.netloc}{path}")
    if len(path) >= 3 and path[0] == "/" and path[1].isalpha() and path[2] == ":":
        if os.name == "nt":
            return url2pathname(path)
        return path[1:]
    return url2pathname(path)


def _normalize_cwd(raw: str) -> Path | None:
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return Path(raw)


def cwd_matches(session_cwd: str, target_cwd: str) -> bool:
    if not session_cwd or not target_cwd:
        return False
    s_path = _normalize_cwd(session_cwd)
    t_path = _normalize_cwd(target_cwd)
    if s_path is None or t_path is None:
        return False
    if s_path == t_path or s_path.is_relative_to(t_path) or t_path.is_relative_to(s_path):
        return True
    s_norm = os.path.normcase(str(s_path)).rstrip("\\/")
    t_norm = os.path.normcase(str(t_path)).rstrip("\\/")
    if s_norm == t_norm:
        return True
    sep = os.sep
    return s_norm.startswith(t_norm + sep) or t_norm.startswith(s_norm + sep)


def project_name(cwd: str) -> str:
    if not cwd:
        return "—"
    return Path(cwd).name or cwd


def shorten(text: str, width: int = 80) -> str:
    single = " ".join(text.split())
    if len(single) <= width:
        return single
    return single[: width - 1] + "…"


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in ("text", "input_text", "output_text") and isinstance(
                    item.get("text"), str
                ):
                    parts.append(item["text"])
        return "\n".join(p for p in parts if p)
    return ""


def read_head_and_count(
    path: Path,
    markers: tuple[bytes, ...],
    size: int,
    head_bytes: int = 65536,
    max_lines: int = 25,
    scan_bytes: int = 131072,
) -> tuple[list[str], int]:
    if size <= 0:
        return [], 0
    try:
        with path.open("rb") as stream:
            buf = stream.read(min(size, scan_bytes))
    except OSError:
        return [], 0
    if not buf:
        return [], 0
    count = sum(buf.count(marker) for marker in markers)
    head = buf[:head_bytes]
    raw_lines = head.split(b"\n")
    if len(head) == head_bytes and not head.endswith(b"\n") and len(raw_lines) > 1:
        raw_lines = raw_lines[:-1]
    lines: list[str] = []
    for raw in raw_lines:
        if not raw.strip():
            continue
        lines.append(raw.decode("utf-8", errors="replace"))
        if len(lines) >= max_lines:
            break
    return lines, count


def fast_head_lines_large(path: Path, max_bytes: int = 262144, max_lines: int = 10) -> list[str]:
    lines: list[str] = []
    read_bytes = 0
    try:
        with path.open("rb") as stream:
            for _ in range(max_lines * 3):
                raw = stream.readline(max_bytes)
                if not raw:
                    break
                read_bytes += len(raw)
                if raw.strip():
                    lines.append(raw.decode("utf-8", errors="replace"))
                    if len(lines) >= max_lines:
                        break
                if read_bytes >= max_bytes:
                    break
    except OSError:
        return []
    return lines
