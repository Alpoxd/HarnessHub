from __future__ import annotations

import errno
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

from platformdirs import user_state_path

from harnesshub.errors import BusyError


def resource_lock(path: Path, root: Path | None = None) -> Path:
    identity = os.path.normcase(str(path.resolve()))
    digest = hashlib.sha256(os.fsencode(identity)).hexdigest()
    return (root or user_state_path("harnesshub", appauthor=False) / "locks") / f"{digest}.lock"


def _win32_lock_helpers() -> tuple[Any, Any, Any, Any]:
    import ctypes
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    lock_file_ex = kernel32.LockFileEx
    lock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    lock_file_ex.restype = wintypes.BOOL

    unlock_file_ex = kernel32.UnlockFileEx
    unlock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    unlock_file_ex.restype = wintypes.BOOL
    return ctypes, _Overlapped, lock_file_ex, unlock_file_ex


def _win32_try_lock(fd: int, *, shared: bool) -> bool:
    import msvcrt

    ctypes, overlapped_cls, lock_file_ex, _ = _win32_lock_helpers()
    handle = msvcrt.get_osfhandle(fd)  # type: ignore[attr-defined]
    flags = 0x00000001  # LOCKFILE_FAIL_IMMEDIATELY
    if not shared:
        flags |= 0x00000002  # LOCKFILE_EXCLUSIVE_LOCK
    ov = overlapped_cls()
    ok = lock_file_ex(handle, flags, 0, 1, 0, ctypes.byref(ov))
    if ok:
        return True
    err = ctypes.get_last_error()
    if err in (33, 32):  # ERROR_LOCK_VIOLATION / ERROR_SHARING_VIOLATION
        return False
    raise ctypes.WinError(err)


def _win32_unlock(fd: int) -> None:
    import msvcrt

    ctypes, overlapped_cls, _, unlock_file_ex = _win32_lock_helpers()
    handle = msvcrt.get_osfhandle(fd)  # type: ignore[attr-defined]
    ov = overlapped_cls()
    unlock_file_ex(handle, 0, 1, 0, ctypes.byref(ov))


def inspect_lock_state(path: Path) -> tuple[bool, bool]:
    """Return (exclusive_locked, active_session) without creating missing lock files."""
    if not path.is_file():
        return False, False
    flags = os.O_RDWR if sys.platform == "win32" else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return False, False
    try:
        os.set_inheritable(fd, False)
        if sys.platform == "win32":
            if os.fstat(fd).st_size == 0:
                return False, False
            if not _win32_try_lock(fd, shared=True):
                return True, True
            _win32_unlock(fd)
            if not _win32_try_lock(fd, shared=False):
                return False, True
            _win32_unlock(fd)
            return False, False

        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return True, True
            return False, False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False, True
            return False, False
        return False, False
    finally:
        os.close(fd)


class FileLock:
    def __init__(self, path: Path, *, shared: bool = False):
        self.path = path
        self.shared = shared
        self.fd: int | None = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.set_inheritable(fd, False)
            if sys.platform == "win32":
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                if not _win32_try_lock(fd, shared=self.shared):
                    raise BusyError("Ресурс занят другим запуском HarnessHub")
            else:
                import fcntl

                mode = (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
                fcntl.flock(fd, mode)
        except BusyError:
            os.close(fd)
            raise
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BusyError("Ресурс занят другим запуском HarnessHub") from exc
            raise
        self.fd = fd
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            if sys.platform == "win32":
                try:
                    _win32_unlock(self.fd)
                except OSError:
                    pass
            os.close(self.fd)
            self.fd = None
