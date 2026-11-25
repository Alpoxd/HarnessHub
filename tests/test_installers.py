from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"


def _find_pwsh() -> str | None:
    found = shutil.which("pwsh")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "pwsh"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return None


def _make_fake_uv(bin_dir: Path, log_file: Path, tool_bin_dir: Path) -> Path:
    uv_bin = bin_dir / "uv"
    uv_bin.write_text(
        f"""#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "{log_file}"
case "${{1:-}} ${{2:-}}" in
    "tool install")
        mkdir -p "{tool_bin_dir}"
        cat > "{tool_bin_dir}/harnesshub" <<'BIN'
#!/bin/sh
if [ "${{1:-}}" = "--version" ]; then
    echo "0.2.0"
    exit 0
fi
exit 0
BIN
        chmod +x "{tool_bin_dir}/harnesshub"
        exit 0
        ;;
    "tool dir")
        mkdir -p "{tool_bin_dir.parent}/share"
        printf '%s/share/../%s\\n' "{tool_bin_dir.parent}" "{tool_bin_dir.name}"
        exit 0
        ;;
    "tool update-shell")
        exit 0
        ;;
    "tool uninstall")
        rm -f "{tool_bin_dir}/harnesshub"
        exit 0
        ;;
esac
exit 1
""",
        encoding="utf-8",
    )
    uv_bin.chmod(0o755)
    return uv_bin


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell test")
def test_install_sh_syntax_and_executable() -> None:
    assert INSTALL_SH.is_file()
    mode = INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "install.sh should be executable"

    for shell in ("sh", "dash", "bash"):
        check = subprocess.run(["which", shell], capture_output=True, text=True)
        if check.returncode != 0:
            continue
        res = subprocess.run([shell, "-n", str(INSTALL_SH)], capture_output=True, text=True)
        assert res.returncode == 0, f"{shell} -n failed: {res.stderr}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell test")
def test_install_sh_help() -> None:
    res = subprocess.run(["sh", str(INSTALL_SH), "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "HarnessHub installer" in res.stdout
    assert "--uninstall" in res.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell test")
def test_install_sh_install_update_and_uninstall_with_stub_uv(tmp_path: Path) -> None:
    fake_bin = tmp_path / "uv-bin"
    fake_bin.mkdir()
    tool_bin = tmp_path / "tool-bin"
    log_file = tmp_path / "uv.log"
    _make_fake_uv(fake_bin, log_file, tool_bin)

    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "HARNESSHUB_REF": "v0.2.0",
    }
    Path(env["HOME"]).mkdir()

    # Install (also checks update-shell when tool_bin is not in PATH)
    res = subprocess.run(["sh", str(INSTALL_SH)], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "Installed HarnessHub 0.2.0" in res.stdout
    assert "export PATH=" in res.stdout

    logged = log_file.read_text(encoding="utf-8").splitlines()
    assert any(
        "tool install --force --reinstall-package harnesshub --python >=3.11 "
        "https://github.com/Alpoxd/HarnessHub/archive/v0.2.0.tar.gz" == line
        for line in logged
    ), logged
    assert "tool update-shell" in logged

    # Install with --no-modify-path and explicit --source
    log_file.write_text("", encoding="utf-8")
    res2 = subprocess.run(
        ["sh", str(INSTALL_SH), "--no-modify-path", "--source", "/tmp/custom.whl"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0, res2.stderr
    logged2 = log_file.read_text(encoding="utf-8").splitlines()
    assert "tool update-shell" not in logged2
    assert any(line.endswith("/tmp/custom.whl") for line in logged2), logged2

    # Uninstall via flag
    res3 = subprocess.run(
        ["sh", str(INSTALL_SH), "--uninstall"], env=env, capture_output=True, text=True
    )
    assert res3.returncode == 0, res3.stderr
    assert not (tool_bin / "harnesshub").exists()
    assert "tool uninstall harnesshub" in log_file.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell test")
def test_install_sh_bootstraps_uv_when_missing(tmp_path: Path) -> None:
    tool_bin = tmp_path / "tool-bin"
    log_file = tmp_path / "uv.log"
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    local_bin = home_dir / ".local" / "bin"

    # Fake uv installer script served via stub curl
    uv_install_script = tmp_path / "fake-uv-install.sh"
    uv_install_script.write_text(
        f"""#!/bin/sh
set -eu
mkdir -p "{local_bin}"
cat > "{local_bin}/uv" <<'UVEOF'
#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "{log_file}"
case "${{1:-}} ${{2:-}}" in
    "tool install")
        mkdir -p "{tool_bin}"
        printf '#!/bin/sh\\necho 0.2.0\\n' > "{tool_bin}/harnesshub"
        chmod +x "{tool_bin}/harnesshub"
        ;;
    "tool dir")
        printf '%s\\n' "{tool_bin}"
        ;;
    "tool update-shell")
        ;;
esac
UVEOF
chmod +x "{local_bin}/uv"
""",
        encoding="utf-8",
    )

    stub_curl_dir = tmp_path / "stub-bin"
    stub_curl_dir.mkdir()
    stub_curl = stub_curl_dir / "curl"
    stub_curl.write_text(
        f"""#!/bin/sh
set -eu
dest=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) dest="$2"; shift 2 ;;
        *) shift ;;
    esac
done
cp "{uv_install_script}" "$dest"
""",
        encoding="utf-8",
    )
    stub_curl.chmod(0o755)

    env = {
        "PATH": f"{stub_curl_dir}:/usr/bin:/bin",
        "HOME": str(home_dir),
        "HARNESSHUB_NO_MODIFY_PATH": "1",
        "HARNESSHUB_UV_INSTALL_URL": "https://example.invalid/uv-install.sh",
    }
    res = subprocess.run(["sh", str(INSTALL_SH)], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "uv not found; installing uv" in res.stdout
    assert "Installed HarnessHub 0.2.0" in res.stdout
    assert (local_bin / "uv").is_file()


def test_install_ps1_windows_powershell_51_safety() -> None:
    assert INSTALL_PS1.is_file()
    content = INSTALL_PS1.read_text(encoding="utf-8")

    # Pure ASCII prevents UTF-8 without BOM mojibake under Windows PowerShell 5.1 irm | iex
    assert content.isascii(), "install.ps1 must stay pure ASCII for PowerShell 5.1 irm | iex"

    # Must not call bare exit (which closes the caller's PowerShell window under irm | iex)
    assert not re.search(r"(?m)^\s*exit(\s+\d+)?\s*$", content)

    # Must set TLS 1.2 for PowerShell 5.1
    assert "SecurityProtocolType]::Tls12" in content

    # Must check $LASTEXITCODE for native uv calls
    assert "$LASTEXITCODE" in content

    # Must not use PowerShell 7-only operators (??, &&, ||)
    assert "??" not in content
    assert "&&" not in content
    assert "||" not in content

    # Must stay consistent with install.sh defaults and flags
    assert "Alpoxd/HarnessHub" in content
    assert "--reinstall-package harnesshub" in content
    assert "https://astral.sh/uv/install.ps1" in content

    # Balanced braces
    assert content.count("{") == content.count("}")


@pytest.mark.skipif(_find_pwsh() is None, reason="pwsh not installed")
def test_install_ps1_ast_and_live_execution_with_stub_uv(tmp_path: Path) -> None:
    pwsh = _find_pwsh()
    assert pwsh is not None

    # 1. Real PowerShell AST parse check
    parse_cmd = (
        "$errors = $null; $tokens = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$tokens, [ref]$errors); "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    res_parse = subprocess.run(
        [pwsh, "-NoProfile", "-Command", parse_cmd], capture_output=True, text=True
    )
    assert res_parse.returncode == 0, res_parse.stderr

    # 2. Live execution via Invoke-Expression (simulating `irm ... | iex`) under StrictMode
    fake_bin = tmp_path / "uv-bin"
    fake_bin.mkdir()
    tool_bin = tmp_path / "tool-bin"
    log_file = tmp_path / "uv.log"
    _make_fake_uv(fake_bin, log_file, tool_bin)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}/usr/bin{os.pathsep}/bin"
    env["HARNESSHUB_REF"] = "v0.2.0"
    env.pop("HARNESSHUB_SOURCE", None)
    env.pop("HARNESSHUB_NO_MODIFY_PATH", None)
    env.pop("HARNESSHUB_UNINSTALL", None)

    iex_check = f"""
Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"
(Get-Content -Raw "{INSTALL_PS1}") | Invoke-Expression
if ($ErrorActionPreference -ne "Continue") {{
    throw "ErrorActionPreference leaked: $ErrorActionPreference"
}}
if (Get-Command Find-UvExecutable -ErrorAction SilentlyContinue) {{
    throw "Helper function leaked into caller scope"
}}
if (-not ($env:Path -like "*{tool_bin}*")) {{
    throw "tool_bin not added to session PATH: $env:Path"
}}
Write-Output "IEX_CHECKS_PASSED"
"""
    res_iex = subprocess.run(
        [pwsh, "-NoProfile", "-Command", iex_check], env=env, capture_output=True, text=True
    )
    assert res_iex.returncode == 0, f"STDOUT:\n{res_iex.stdout}\nSTDERR:\n{res_iex.stderr}"
    assert "Installed HarnessHub 0.2.0" in res_iex.stdout
    assert "IEX_CHECKS_PASSED" in res_iex.stdout

    logged = log_file.read_text(encoding="utf-8").splitlines()
    assert (
        "tool install --force --reinstall-package harnesshub --python >=3.11 "
        "https://github.com/Alpoxd/HarnessHub/archive/v0.2.0.tar.gz"
    ) in logged
    assert "tool update-shell" in logged

    # 3. Direct -File invocation with -NoModifyPath, -Source, and -Uninstall
    log_file.write_text("", encoding="utf-8")
    res_file = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(INSTALL_PS1),
            "-NoModifyPath",
            "-Source",
            "/tmp/custom.whl",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_file.returncode == 0, res_file.stderr
    logged2 = log_file.read_text(encoding="utf-8").splitlines()
    assert "tool update-shell" not in logged2
    assert any(line.endswith("/tmp/custom.whl") for line in logged2), logged2

    res_uninst = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(INSTALL_PS1), "-Uninstall"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_uninst.returncode == 0, res_uninst.stderr
    assert not (tool_bin / "harnesshub").exists()
    assert "tool uninstall harnesshub" in log_file.read_text(encoding="utf-8")


@pytest.mark.skipif(_find_pwsh() is None, reason="pwsh not installed")
def test_install_ps1_bootstraps_uv_via_http(tmp_path: Path) -> None:
    pwsh = _find_pwsh()
    assert pwsh is not None

    user_profile = tmp_path / "profile"
    local_bin = user_profile / ".local" / "bin"
    tool_bin = tmp_path / "tool-bin"
    log_file = tmp_path / "uv.log"

    fake_uv_ps1 = f"""
New-Item -ItemType Directory -Force -Path "{local_bin}" | Out-Null
$uvScript = @'
#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "{log_file}"
case "${{1:-}} ${{2:-}}" in
    "tool install")
        mkdir -p "{tool_bin}"
        printf '#!/bin/sh\\necho 0.2.0\\n' > "{tool_bin}/harnesshub"
        chmod +x "{tool_bin}/harnesshub"
        ;;
    "tool dir")
        printf '%s\\n' "{tool_bin}"
        ;;
    "tool update-shell")
        ;;
esac
'@
Set-Content -Path "{local_bin}/uv" -Value $uvScript -Encoding ascii
chmod +x "{local_bin}/uv"
"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = fake_uv_ps1.encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        env["PATH"] = f"/usr/bin{os.pathsep}/bin"
        env["USERPROFILE"] = str(user_profile)
        env["HARNESSHUB_NO_MODIFY_PATH"] = "1"
        env["HARNESSHUB_UV_INSTALL_URL"] = f"http://127.0.0.1:{port}/install.ps1"
        env.pop("UV_INSTALL_DIR", None)
        env.pop("XDG_BIN_HOME", None)
        env.pop("LOCALAPPDATA", None)
        env.pop("HARNESSHUB_UNINSTALL", None)

        res = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(INSTALL_PS1)],
            env=env,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert res.returncode == 0, f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "uv not found; installing uv" in res.stdout
    assert "Installed HarnessHub 0.2.0" in res.stdout
    assert (local_bin / "uv").is_file()
