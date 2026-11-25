import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnesshub.errors import BusyError, HubError
from harnesshub.installation import NPM_REGISTRY, SOURCES, Installer
from harnesshub.locking import FileLock, resource_lock
from harnesshub.service import Hub


@pytest.fixture
def installer(tmp_path, monkeypatch):
    hub = Hub(tmp_path / "hub", lock_root=tmp_path / "locks")
    installer = Installer(hub)
    monkeypatch.setattr(installer, "_npm_launcher", lambda: (sys.executable, "mock-npm-cli.js"))
    monkeypatch.setattr(installer, "_check_node", lambda *args: None)
    return installer


def npm_result(directory: Path, provider="@openai/codex", command="codex", native=False):
    packages = directory / ("node_modules" if os.name == "nt" else "lib/node_modules")
    package = packages / provider
    executable = package / ("bin/cli.exe" if native else "bin/cli.js")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"\x7fELFfake-native" if native else b"console.log('original')\n")
    executable.chmod(0o755)
    (package / "package.json").write_text(
        json.dumps({"version": "1.2.3", "bin": {command: str(executable.relative_to(package))}})
    )
    return executable


def test_dry_run_has_official_source_and_creates_nothing(installer):
    plan = installer.plan("codex")
    assert plan["source"] == NPM_REGISTRY + "@openai/codex"
    assert not installer.hub.store.root.exists()


def test_install_uses_official_registry_generation_and_node_launcher(installer, monkeypatch):
    observed = []

    def run(command, env, cwd, timeout=900):
        observed.append((command, env))
        assert command[2] == "install"
        assert command[command.index("--registry") + 1] == NPM_REGISTRY
        assert command[-1] == "@openai/codex@latest"
        npm_result(cwd)
        return ""

    monkeypatch.setenv("npm_config_registry", "https://untrusted.invalid")
    monkeypatch.setattr(installer, "_run", run)
    result = installer.install("codex")
    assert result["status"] == "installed"
    assert observed[0][1]["NPM_CONFIG_REGISTRY"] == NPM_REGISTRY
    assert "npm_config_registry" not in observed[0][1]
    assert installer.hub.command("codex", None) == [sys.executable, result["executable"]]
    assert installer.hub.harness("codex").executable() == result["executable"]


def test_native_claude_launcher_is_not_passed_to_node(installer, monkeypatch):
    monkeypatch.setattr(
        installer,
        "_run",
        lambda command, env, cwd: npm_result(
            cwd, "@anthropic-ai/claude-code", "claude", native=True
        ),
    )
    result = installer.install("claude")
    assert result["launcher"] == [result["executable"]]


def test_install_uses_global_proxy_unless_profile_is_explicit(installer):
    hub = installer.hub
    hub.create("codex", "work")
    hub.set_proxy({"mode": "url", "url": "http://global:80"})
    hub.set_proxy({"mode": "url", "url": "http://account:81"}, "codex", "work")
    assert installer._environment("codex", None)["HTTPS_PROXY"] == "http://global:80"
    assert installer._environment("codex", "work")["HTTPS_PROXY"] == "http://account:81"


def test_failed_reinstall_preserves_previous_generation(installer, monkeypatch):
    monkeypatch.setattr(installer, "_run", lambda command, env, cwd: npm_result(cwd))
    previous = installer.install("codex")

    def fail(*args, **kwargs):
        raise HubError("download failed")

    monkeypatch.setattr(installer, "_run", fail)
    with pytest.raises(HubError, match="download failed"):
        installer.install("codex")
    assert installer.hub.harness("codex").executable() == previous["executable"]
    assert Path(previous["directory"]).is_dir()
    assert len(list(Path(previous["directory"]).parent.iterdir())) == 1


def test_interrupt_after_publish_keeps_registered_install(installer, monkeypatch):
    monkeypatch.setattr(installer, "_run", lambda command, env, cwd: npm_result(cwd))
    update = installer.hub.store.update

    def interrupt(change):
        update(change)
        raise KeyboardInterrupt

    monkeypatch.setattr(installer.hub.store, "update", interrupt)
    with pytest.raises(KeyboardInterrupt):
        installer.install("codex")
    assert Path(installer.hub.store.read()["installations"]["codex"]["executable"]).is_file()


def test_concurrent_installation_is_rejected(installer):
    parent = installer.hub.store.root / "tools" / "codex"
    with FileLock(resource_lock(parent / ".installation", installer.hub.lock_root)):
        with pytest.raises(BusyError):
            installer.install("codex")


def test_managed_node_entrypoint_does_not_use_windows_cmd_shim(installer, tmp_path, monkeypatch):
    directory = tmp_path / "prefix"
    directory.mkdir()
    monkeypatch.setattr(installer, "_run", lambda command, env, cwd: npm_result(cwd))
    executable, command, _ = installer._install_npm(SOURCES["codex"], directory, {})
    assert command == [sys.executable, str(executable)]
    assert not any(arg.endswith(".cmd") for arg in command)


def test_windows_npm_shim_is_resolved_to_npm_js(tmp_path, monkeypatch):
    npm = tmp_path / "npm.cmd"
    npm.write_text("fixture cmd shim")
    script = tmp_path / "node_modules/npm/bin/npm-cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("fixture npm")
    monkeypatch.setattr(
        "harnesshub.installation.shutil.which",
        lambda name: sys.executable if name == "node" else str(npm),
    )
    assert Installer._npm_launcher() == (sys.executable, str(script))


def test_installing_legacy_kimi_is_rejected(installer):
    with pytest.raises(HubError, match="kimi-code"):
        installer.plan("kimi")


def test_copilot_requires_node22_even_without_package_engine_metadata(monkeypatch):
    monkeypatch.setattr(
        "harnesshub.installation.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="v20.19.0\n"),
    )
    with pytest.raises(HubError, match="22.0"):
        Installer._check_node("node", SOURCES["copilot"].node_min, {})


@pytest.mark.parametrize(
    "taskkill_error", [OSError("unavailable"), subprocess.TimeoutExpired("taskkill", 10)]
)
def test_windows_taskkill_failure_still_terminates_and_reaps_installer(
    tmp_path, monkeypatch, taskkill_error
):
    import harnesshub.installation as installation

    class Process:
        pid = 12345
        killed = False
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if not self.killed:
                raise subprocess.TimeoutExpired("installer", timeout)
            return "", None

        def poll(self):
            return -1 if self.killed else None

        def kill(self):
            self.killed = True

    process = Process()

    def fail_taskkill(*args, **kwargs):
        raise taskkill_error

    monkeypatch.setattr(installation, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(installation.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(installation.subprocess, "run", fail_taskkill)
    monkeypatch.setattr(installation.shutil, "which", lambda name: "taskkill.exe")
    with pytest.raises(subprocess.TimeoutExpired, match="installer"):
        Installer._run(["installer"], {}, tmp_path, timeout=1)
    assert process.killed and process.calls == 2


@pytest.mark.skipif(sys.platform != "linux", reason="Linux child process state inspection")
def test_timeout_terminates_children_after_installer_leader_exits(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess,sys; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(pidfile)!r}).write_text(str(child.pid))"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        Installer._run([sys.executable, "-c", code], dict(os.environ), tmp_path, timeout=0.7)
    pid = int(pidfile.read_text())
    stat = Path(f"/proc/{pid}/stat")
    try:
        state = stat.read_text().split()[2]
    except FileNotFoundError:
        return
    assert state == "Z"


def test_junie_managed_data_is_separate_from_account_home(installer, monkeypatch):
    def install_vendor(source, directory, env):
        executable = directory / "junie"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        return executable, [str(executable)], "fixture-version"

    if os.name == "nt":
        pytest.skip("Managed POSIX vendor installation")
    monkeypatch.setattr(installer, "_install_vendor", install_vendor)
    previous_home = os.environ.get("HOME")
    result = installer.install("junie")
    env = installer.hub.environment("junie")
    assert env.get("HOME") == previous_home
    assert env["JUNIE_DATA"] == str(Path(result["directory"]) / "home/.local/share/junie")


@pytest.mark.skipif(os.name == "nt", reason="POSIX vendor isolation")
def test_vendor_install_uses_isolated_home_and_never_patches_agy(installer, monkeypatch):
    previous = installer.hub.store.root.parent / "previous-agy"
    previous.write_bytes(b"previous-version")
    previous.chmod(0o755)
    installer.hub.store.update(lambda state: state["settings"].update(agy_binary=str(previous)))

    def download(url, path, env):
        assert url == "https://antigravity.google/cli/install.sh"
        path.write_text("official fixture")

    def run(command, env, cwd, timeout=900):
        home = Path(env["HOME"])
        assert home.is_relative_to(cwd)
        executable = home / ".local/bin/agy"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"clean-vendor-binary")
        executable.chmod(0o755)
        return ""

    monkeypatch.setattr(installer, "_download_script", download)
    monkeypatch.setattr(installer, "_run", run)
    result = installer.install("agy")
    assert Path(result["executable"]).read_bytes() == b"clean-vendor-binary"
    assert installer.hub.command("agy", None) == [result["executable"]]
    assert "agy_binary" not in installer.hub.store.read()["settings"]
