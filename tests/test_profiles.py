import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harnesshub.errors import BusyError, HubError
from harnesshub.locking import FileLock, resource_lock
from harnesshub.service import Hub


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path / "hub", lock_root=tmp_path / "locks")


def test_profile_lifecycle_keeps_native_data(hub):
    profile = hub.create("codex", "work")
    path = Path(profile["path"])
    (path / "session.txt").write_text("conversation")
    assert 'cli_auth_credentials_store = "file"' in (path / "config.toml").read_text()
    assert hub.profile("codex")["name"] == "work"
    hub.rename("codex", "work", "office")
    assert hub.profile("codex")["path"] == str(path)
    hub.remove("codex", "office")
    assert hub.profiles() == []
    assert (path / "session.txt").read_text() == "conversation"


def test_interruption_after_metadata_commit_keeps_registered_profile(hub, monkeypatch):
    update = hub.store.update

    def interrupt_after_commit(change):
        update(change)
        raise KeyboardInterrupt

    monkeypatch.setattr(hub.store, "update", interrupt_after_commit)
    with pytest.raises(KeyboardInterrupt):
        hub.create("codex", "work")
    assert Path(hub.profile("codex", "work")["path"]).is_dir()


def test_import_is_metadata_only_and_rejects_duplicate_paths(hub, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "native-auth-placeholder"
    sentinel.write_text("unchanged")
    hub.import_profile("kimi", "work", external)
    with pytest.raises(HubError, match="другим именем"):
        hub.import_profile("kimi", "other", external)
    hub.remove("kimi", "work")
    assert sentinel.read_text() == "unchanged"


@pytest.mark.parametrize("name", ["../escape", "/absolute", ".", "", "bad name", "a" * 33])
def test_profile_names_cannot_escape_root(hub, name):
    with pytest.raises(HubError):
        hub.create("codex", name)
    assert not hub.store.path.exists()


def test_credentials_are_scoped_in_child_env_only(hub, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-placeholder")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "shared-keychain")
    profile = hub.create("claude", "work")
    env = hub.environment("claude", profile)
    assert env["CLAUDE_CONFIG_DIR"] == profile["path"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in env
    assert os.environ["ANTHROPIC_API_KEY"] == "inherited-placeholder"


def test_codex_workload_identity_cannot_override_subscription_profile(hub, monkeypatch):
    variables = (
        "CODEX_ACCESS_TOKEN",
        "OPENAI_FEDERATION_RULE_ID",
        "OPENAI_IDENTITY_TOKEN_FILE",
        "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
        "CODEX_SQLITE_HOME",
    )
    for variable in variables:
        monkeypatch.setenv(variable, "inherited-placeholder")
    profile = hub.create("codex", "work")
    env = hub.environment("codex", profile)
    assert not any(variable in env for variable in variables)
    assert env["CODEX_HOME"] == profile["path"]


def test_legacy_discovery_does_not_read_or_import_credentials(hub, tmp_path, monkeypatch):
    home = tmp_path / "fake-home"
    (home / "agy-profiles" / "acc7").mkdir(parents=True)
    (home / "codex-profiles" / "acc9").mkdir(parents=True)
    monkeypatch.delenv("AGY_PROFILES", raising=False)
    monkeypatch.delenv("CODEX_PROFILES", raising=False)
    found = hub.existing_profiles(home)
    assert {(p["provider"], p["name"]) for p in found} == {("agy", "acc7"), ("codex", "acc9")}
    assert not hub.store.path.exists()


@pytest.mark.parametrize(
    "payload", ["broken json", "[]", '{"version": 999}', '{"version": 1, "profiles": "bad"}']
)
def test_corrupted_metadata_is_never_overwritten(hub, payload):
    hub.store.root.mkdir(parents=True)
    hub.store.path.write_text(payload)
    with pytest.raises(HubError):
        hub.create("codex", "work")
    assert hub.store.path.read_text() == payload


def test_resource_locks_contend_across_processes_and_release(hub, tmp_path):
    identity = tmp_path / "external-profile"
    path = resource_lock(identity, hub.lock_root)
    code = (
        "from pathlib import Path; from harnesshub.locking import FileLock; "
        f"lock=FileLock(Path({str(path)!r})); lock.__enter__()"
    )
    with FileLock(path):
        result = subprocess.run([sys.executable, "-c", code], capture_output=True)
        assert result.returncode != 0
        assert b"BusyError" in result.stderr
    with FileLock(path):
        pass


def test_active_profile_cannot_be_removed(hub):
    profile = hub.create("codex", "work")
    with FileLock(resource_lock(Path(profile["path"]), hub.lock_root)):
        with pytest.raises(BusyError):
            hub.remove("codex", "work")
    assert hub.profile("codex", "work")


def test_native_launch_obeys_inherited_profile_lock(hub, monkeypatch):
    profile = hub.create("codex", "work")
    monkeypatch.setenv("CODEX_HOME", profile["path"])
    monkeypatch.setattr(hub, "command", lambda *a, **kw: [sys.executable, "-c", "pass"])
    with FileLock(resource_lock(Path(profile["path"]), hub.lock_root)):
        with pytest.raises(BusyError):
            hub.launch("codex", native=True)


def test_native_and_named_profile_cannot_be_combined(hub):
    with pytest.raises(HubError, match="--native"):
        hub.launch("codex", "work", native=True)


def test_native_launch_argv_cwd_env_and_exit_code(hub, tmp_path):
    hub.register_harness("fake", "Fake CLI", sys.executable, "FAKE_CONFIG_DIR")
    profile = hub.create("fake", "work")
    script = tmp_path / "fake.py"
    output = tmp_path / "observed.json"
    script.write_text(
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps([os.environ['FAKE_CONFIG_DIR'], "
        "os.getcwd(), sys.argv[2:]]))\n"
        "raise SystemExit(7)\n"
    )
    args = (str(script), str(output), "space in arg", "$(literal)")
    assert hub.launch("fake", "work", args=args, cwd=tmp_path) == 7
    assert json.loads(output.read_text()) == [
        profile["path"],
        str(tmp_path),
        ["space in arg", "$(literal)"],
    ]


def test_legacy_kimi_adapter_does_not_launch_new_kimi_code(hub, tmp_path, monkeypatch):
    command = tmp_path / "kimi"
    command.write_text("#!/usr/bin/env node\nimport 'kimi-code';\n")
    monkeypatch.setattr("harnesshub.registry.shutil.which", lambda name: str(command))
    assert hub.harness("kimi").executable() is None
    command.write_text("#!/usr/bin/python3\nfrom kimi_cli.cli import cli\ncli()\n")
    assert hub.harness("kimi").executable() == str(command)
