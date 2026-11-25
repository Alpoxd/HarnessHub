import hashlib
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnesshub.agy import AgyMaintenance, inspect
from harnesshub.errors import HubError
from harnesshub.service import Hub


def elf(gates=2):
    data = bytearray(512)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", data, 18, 0x3E)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HH", data, 54, 56, 1)
    struct.pack_into("<IIQ", data, 64, 1, 5, 128)
    struct.pack_into("<Q", data, 96, 384)
    for index in range(gates):
        start, register = 160 + index * 64, index
        first = 400 - (start + 9)
        sequence = bytes((0x48, 0x85, 0xC0 | register << 3 | register))
        sequence += b"\x0f\x84" + struct.pack("<i", first)
        sequence += bytes((0x80, 0x78 + register, 8, 0))
        sequence += b"\x0f\x85" + struct.pack("<i", first - 10)
        data[start : start + len(sequence)] = sequence
    return bytes(data)


@pytest.fixture
def maintenance(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Builtin patch transaction currently targets Linux")
    target = tmp_path / "agy-fixture"
    target.write_bytes(elf())
    target.chmod(0o755)
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    maintenance = AgyMaintenance(hub)
    maintenance.configure(binary=str(target))
    return maintenance, target


def test_configured_binary_is_also_the_launch_target(maintenance):
    manager, target = maintenance
    profile = manager.hub.create("agy", "work")
    command = manager.hub.command("agy", profile)
    assert command[0] == str(target)


def test_apply_idempotence_backup_and_restore(maintenance, monkeypatch):
    manager, target = maintenance
    original = target.read_bytes()
    monkeypatch.setattr(
        "harnesshub.agy.subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0)
    )
    assert manager.status()["status"] == "unpatched"
    result = manager.heal()
    assert result["changed"] is True
    assert Path(result["backup"]).read_bytes() == original
    assert manager.status()["status"] == "patched"
    assert manager.heal()["changed"] is False
    manager.restore()
    assert target.read_bytes() == original


def test_failed_smoke_test_rolls_back(maintenance, monkeypatch):
    manager, target = maintenance
    original = target.read_bytes()
    monkeypatch.setattr(
        "harnesshub.agy.subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=1)
    )
    with pytest.raises(HubError, match="--version"):
        manager.heal()
    assert target.read_bytes() == original


def test_post_write_corruption_rolls_back_own_installed_file(maintenance, monkeypatch):
    import harnesshub.agy as agy

    manager, target = maintenance
    original = target.read_bytes()
    atomic_write = agy._atomic_write
    corrupted_once = False

    def corrupt_install(path, data, mode=0o600):
        nonlocal corrupted_once
        identity = atomic_write(path, data, mode)
        if path == target and not corrupted_once:
            corrupted_once = True
            with path.open("r+b") as stream:
                stream.write(b"bad")
        return identity

    monkeypatch.setattr(agy, "_atomic_write", corrupt_install)
    with pytest.raises(HubError, match="Контрольная сумма"):
        manager.heal()
    assert target.read_bytes() == original


def test_corrupt_backup_refuses_write(maintenance):
    manager, target = maintenance
    original = target.read_bytes()
    digest = hashlib.sha256(original).hexdigest()[:16]
    target.with_name(f"{target.name}.hh-{digest}.bak").write_bytes(b"corrupted")
    with pytest.raises(HubError, match="SHA-256"):
        manager.heal()
    assert target.read_bytes() == original


def test_restore_does_not_overwrite_new_version(maintenance, monkeypatch):
    manager, target = maintenance
    monkeypatch.setattr(
        "harnesshub.agy.subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0)
    )
    manager.heal()
    target.write_bytes(b"new-version")
    with pytest.raises(HubError, match="обновлён"):
        manager.restore()
    assert target.read_bytes() == b"new-version"


def test_unknown_signature_leaves_binary_unchanged(maintenance):
    manager, target = maintenance
    target.write_bytes(elf(gates=1))
    before = target.read_bytes()
    with pytest.raises(HubError, match="сигнатура"):
        manager.heal()
    assert target.read_bytes() == before


def test_non_executable_segments_are_not_scanned():
    data = bytearray(elf())
    struct.pack_into("<Q", data, 96, 16)
    assert inspect(data)["status"] == "unknown"


def test_invalid_elf_segment_is_rejected():
    data = bytearray(elf())
    struct.pack_into("<Q", data, 96, 99999)
    with pytest.raises(HubError, match="границы"):
        inspect(data)
