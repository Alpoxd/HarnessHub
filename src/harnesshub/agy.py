from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesshub.errors import HubError
from harnesshub.locking import FileLock, resource_lock
from harnesshub.network import proxy_environment

if TYPE_CHECKING:
    from harnesshub.service import Hub


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> tuple[int, int]:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        info = temporary.stat()
        os.replace(temporary, path)
        return info.st_dev, info.st_ino
    finally:
        temporary.unlink(missing_ok=True)


def executable_ranges(data: bytes) -> tuple[str, list[tuple[int, int]]]:
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
        raise HubError("Встроенный автохил поддерживает ELF64 little-endian (Linux)")
    machine = struct.unpack_from("<H", data, 18)[0]
    arch = {0x3E: "x64", 0xB7: "arm64"}.get(machine)
    if arch is None:
        raise HubError("Архитектура ELF не поддерживается")
    table = struct.unpack_from("<Q", data, 32)[0]
    entry_size, count = struct.unpack_from("<HH", data, 54)
    if entry_size < 56 or count == 0 or table + entry_size * count > len(data):
        raise HubError("Некорректная таблица ELF-сегментов")
    ranges = []
    for index in range(count):
        off = table + entry_size * index
        kind, flags, start = struct.unpack_from("<IIQ", data, off)
        size = struct.unpack_from("<Q", data, off + 32)[0]
        if start + size > len(data):
            raise HubError("ELF-сегмент выходит за границы файла")
        if kind == 1 and flags & 1 and size:
            ranges.append((start, start + size))
    if not ranges:
        raise HubError("Исполняемые сегменты ELF не найдены")
    return arch, ranges


def find_gates(
    data: bytes, ranges: list[tuple[int, int]], patched: bool
) -> list[tuple[int, bytes]]:
    found = []
    for register in (0, 1, 2, 3, 6, 7):
        test = bytes((0x48, 0x85, 0xC0 | register << 3 | register))
        replacement = test + b"\x90"
        compare = replacement if patched else bytes((0x80, 0x78 + register, 8, 0))
        pattern = re.compile(
            re.escape(test + b"\x0f\x84") + b"(.{4})" + re.escape(compare) + b"\x0f\x85(.{4})", re.S
        )
        for start, end in ranges:
            for match in pattern.finditer(data, start, end):
                first, second = struct.unpack("<i", match[1])[0], struct.unpack("<i", match[2])[0]
                target = match.start() + 9 + first
                if first == second + 10 and start <= target < end:
                    found.append((match.start() + 9, replacement))
    return sorted(set(found))


def _inspect_with_gates(
    data: bytes,
) -> tuple[dict[str, Any], list[tuple[int, bytes]], list[tuple[int, int]]]:
    arch, ranges = executable_ranges(data)
    if arch != "x64":
        return {"status": "unsupported", "arch": arch, "original": 0, "patched": 0}, [], ranges
    original, patched = find_gates(data, ranges, False), find_gates(data, ranges, True)
    status = "unknown"
    if len(original) == 2 and not patched:
        status = "unpatched"
    elif len(patched) == 2 and not original:
        status = "patched"
    elif original and patched:
        status = "partial"
    return (
        {"status": status, "arch": arch, "original": len(original), "patched": len(patched)},
        original,
        ranges,
    )


def inspect(data: bytes) -> dict[str, Any]:
    info, _, _ = _inspect_with_gates(data)
    return info


class AgyMaintenance:
    def __init__(self, hub: Hub):
        self.hub = hub

    def target(self) -> Path:
        configured = self.hub.store.read()["settings"].get("agy_binary")
        value = configured or self.hub.harness("agy").executable()
        if not value:
            raise HubError("agy не найден; укажи путь: agy configure --binary ПУТЬ")
        target = Path(value).expanduser().resolve(strict=True)
        if not target.is_file():
            raise HubError("Бинарник agy должен быть обычным файлом")
        return target

    def status(self) -> dict[str, Any]:
        target = self.target()
        result = inspect(target.read_bytes())
        return {**result, "path": str(target)}

    def configure(self, binary: str | None = None, helper: str | None = None) -> None:
        values = {}
        for key, value in (("agy_binary", binary), ("agy_helper", helper)):
            if value is not None:
                path = Path(value).expanduser().resolve(strict=True)
                if not path.is_file():
                    raise HubError(f"Файл не найден: {path}")
                values[key] = str(path)
        self.hub.store.update(lambda state: state["settings"].update(values))

    def autoheal(self, name: str, enabled: bool) -> None:
        self.hub.profile("agy", name)

        def change(state: dict[str, Any]) -> None:
            for profile in state["profiles"]:
                if profile["provider"] == "agy" and profile["name"] == name:
                    profile["autoheal"] = enabled
                    return
            raise HubError("Профиль был удалён другим процессом")

        self.hub.store.update(change)

    def heal(
        self, env: dict[str, str] | None = None, *, fast_check: bool = False
    ) -> dict[str, Any]:
        target = self.target()
        if env is None:
            policy, _ = self.hub.proxy_policy()
            env = proxy_environment(dict(os.environ), policy)
        with FileLock(resource_lock(target, self.hub.lock_root)):
            helper = self.hub.store.read()["settings"].get("agy_helper")
            if helper:
                result = subprocess.run(
                    [helper, str(target)],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    env=env,
                )
                if result.returncode:
                    raise HubError("Внешний автохил завершился с ошибкой; смотри его журнал")
                smoke = subprocess.run(
                    [str(target), "--version"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=10,
                    env=env,
                )
                if smoke.returncode:
                    raise HubError(
                        "Проверка agy --version после внешнего автохила завершилась с ошибкой"
                    )
                try:
                    state = self.status()
                except HubError:
                    state = {
                        "status": "unknown",
                        "arch": "unknown",
                        "original": 0,
                        "patched": 0,
                        "path": str(target),
                    }
                if state["status"] in ("unpatched", "partial"):
                    raise HubError("Внешний автохил не подтвердил ожидаемое состояние")
                verified_by = "builtin" if state["status"] == "patched" else "helper"
                return {**state, "status": "patched", "verified_by": verified_by}
            return self._heal_builtin(target, env, fast_check=fast_check)

    @staticmethod
    def _matches_manifest_fast(target: Path, manifest_path: Path) -> bool:
        if not manifest_path.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            st = target.stat()
            return (
                isinstance(manifest, dict)
                and manifest.get("version") == 1
                and manifest.get("patched_dev") == st.st_dev
                and manifest.get("patched_ino") == st.st_ino
                and manifest.get("patched_size") == st.st_size
                and manifest.get("patched_mtime_ns") == st.st_mtime_ns
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False

    def _heal_builtin(
        self, target: Path, env: dict[str, str] | None = None, *, fast_check: bool = False
    ) -> dict[str, Any]:
        if not sys.platform.startswith("linux"):
            raise HubError("Встроенный автохил пока проверен на Linux x64")
        manifest_path = target.with_name(target.name + ".harnesshub.json")
        if fast_check and self._matches_manifest_fast(target, manifest_path):
            return {
                "status": "patched",
                "arch": "x64",
                "original": 0,
                "patched": 2,
                "path": str(target),
                "changed": False,
            }
        original = target.read_bytes()
        status, original_gates, ranges = _inspect_with_gates(original)
        if status["status"] == "patched":
            return {**status, "path": str(target), "changed": False}
        if status["status"] != "unpatched":
            raise HubError(f"Неподдерживаемая сигнатура ({status['status']}); файл не изменён")
        mode = stat.S_IMODE(target.stat().st_mode)
        before_hash = _sha(original)
        backup = target.with_name(f"{target.name}.hh-{before_hash[:16]}.bak")
        if backup.exists():
            if _sha(backup.read_bytes()) != before_hash:
                raise HubError("Резервная копия не прошла проверку SHA-256")
        else:
            _atomic_write(backup, original, mode)
        patched_buf = bytearray(original)
        for offset, replacement in original_gates:
            patched_buf[offset : offset + len(replacement)] = replacement
        patched = bytes(patched_buf)
        if len(find_gates(patched, ranges, True)) != 2 or find_gates(patched, ranges, False):
            raise HubError("Проверка патча в памяти не прошла")
        after_hash = _sha(patched)
        if _sha(target.read_bytes()) != before_hash:
            raise HubError("Бинарник изменён обновлятором; повтори проверку")
        prev_manifest: bytes | None = None
        if manifest_path.is_file():
            try:
                prev_manifest = manifest_path.read_bytes()
            except OSError:
                prev_manifest = None
        manifest: dict[str, Any] = {
            "version": 1,
            "before": before_hash,
            "after": after_hash,
            "backup": backup.name,
            "mode": mode,
            "patched_size": len(patched),
        }
        _atomic_write(manifest_path, json.dumps(manifest).encode("utf-8"))
        installed_identity: tuple[int, int] | None = None
        try:
            installed_identity = _atomic_write(target, patched, mode)
            if _sha(target.read_bytes()) != manifest["after"]:
                raise HubError("Контрольная сумма после записи не совпала")
            result = subprocess.run(
                [str(target), "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                env=env,
            )
            if result.returncode:
                raise HubError("Проверка agy --version завершилась с ошибкой")
            if _sha(target.read_bytes()) != manifest["after"]:
                raise HubError("Бинарник изменился во время проверки запуска")
            st_after = target.stat()
            manifest["patched_dev"] = st_after.st_dev
            manifest["patched_ino"] = st_after.st_ino
            manifest["patched_size"] = st_after.st_size
            manifest["patched_mtime_ns"] = st_after.st_mtime_ns
            _atomic_write(manifest_path, json.dumps(manifest).encode("utf-8"))
        except BaseException:
            if installed_identity is not None:
                try:
                    current = target.stat()
                except OSError:
                    current = None
                if current is not None and (current.st_dev, current.st_ino) == installed_identity:
                    _atomic_write(target, original, mode)
            if prev_manifest is not None:
                _atomic_write(manifest_path, prev_manifest)
            else:
                manifest_path.unlink(missing_ok=True)
            raise
        return {"status": "patched", "path": str(target), "changed": True, "backup": str(backup)}

    def restore(self) -> dict[str, Any]:
        target = self.target()
        with FileLock(resource_lock(target, self.hub.lock_root)):
            path = target.with_name(target.name + ".harnesshub.json")
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                expected_name = f"{target.name}.hh-{manifest['before'][:16]}.bak"
                if manifest["version"] != 1 or manifest["backup"] != expected_name:
                    raise ValueError
                backup = target.with_name(expected_name)
                original = backup.read_bytes()
                if _sha(original) != manifest["before"]:
                    raise ValueError
                if _sha(target.read_bytes()) != manifest["after"]:
                    raise HubError("Бинарник обновлён или изменён; откат старой версии остановлен")
                mode = manifest["mode"]
                if type(mode) is not int or mode & ~0o777:
                    raise ValueError
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise HubError("Проверенная резервная копия HarnessHub не найдена") from exc
            _atomic_write(target, original, mode)
            path.unlink(missing_ok=True)
            return {"status": "restored", "path": str(target)}

    def automode(self, enabled: bool | None = None, name: str | None = None) -> dict[str, Any]:
        if enabled is None and name is None:
            return self.hub.automode_info("agy")
        return self.hub.set_automode(enabled, "agy", name)

    def history_status(self, *, home: Path | None = None) -> dict[str, Any]:
        from harnesshub.agy_workspace import agy_history_status

        return agy_history_status(self.hub, home=home)

    def share_history(
        self, enabled: bool, *, home: Path | None = None, copy_local: bool = False
    ) -> dict[str, Any]:
        from harnesshub.agy_workspace import set_agy_shared_history

        return set_agy_shared_history(self.hub, enabled, home=home, copy_local=copy_local)

    def sync_profiles(self, *, home: Path | None = None) -> dict[str, Any]:
        from harnesshub.agy_workspace import sync_agy_profiles

        return sync_agy_profiles(self.hub, home=home)
