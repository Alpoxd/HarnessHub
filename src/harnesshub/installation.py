from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from harnesshub.errors import HubError
from harnesshub.locking import FileLock, resource_lock
from harnesshub.network import proxy_environment, public_proxy, redact_output
from harnesshub.proc import hidden_popen_kwargs, terminate_process_tree, wrap_windows_command

NPM_REGISTRY = "https://registry.npmjs.org/"


@dataclass(frozen=True)
class InstallSource:
    method: str
    source: str
    command: str
    windows_source: str = ""
    node_min: tuple[int, int] = (20, 0)


SOURCES = {
    "claude": InstallSource("npm", "@anthropic-ai/claude-code", "claude", node_min=(22, 0)),
    "codex": InstallSource("npm", "@openai/codex", "codex", node_min=(16, 0)),
    "qoder": InstallSource("npm", "@qoder-ai/qodercli", "qoder"),
    "kimi-code": InstallSource("npm", "@moonshot-ai/kimi-code", "kimi", node_min=(22, 19)),
    "copilot": InstallSource("npm", "@github/copilot", "copilot", node_min=(22, 0)),
    "gemini": InstallSource("npm", "@google/gemini-cli", "gemini"),
    "agy": InstallSource(
        "vendor",
        "https://antigravity.google/cli/install.sh",
        "agy",
        "https://antigravity.google/cli/install.ps1",
    ),
    "cursor": InstallSource(
        "vendor", "https://cursor.com/install", "agent", "https://cursor.com/install?win32=true"
    ),
    "kiro": InstallSource(
        "vendor", "https://cli.kiro.dev/install", "kiro-cli", "https://cli.kiro.dev/install.ps1"
    ),
    "junie": InstallSource(
        "vendor",
        "https://junie.jetbrains.com/install.sh",
        "junie",
        "https://junie.jetbrains.com/install.ps1",
    ),
}


class Installer:
    def __init__(self, hub):
        self.hub = hub

    def plan(self, provider: str, profile: str | None = None) -> dict:
        spec = self.hub.harness(provider)
        source = SOURCES.get(provider)
        if source is None:
            if provider == "kimi":
                raise HubError("Старый kimi-cli архивирован. Для новой установки выбери kimi-code")
            raise HubError("Для этого harness официальный источник установки ещё не подключён")
        selected = self.hub.profile(provider, profile) if profile is not None else None
        policy, scope = self.hub.proxy_policy(provider, selected)
        url = (
            (NPM_REGISTRY + source.source)
            if source.method == "npm"
            else (source.windows_source if os.name == "nt" else source.source)
        )
        download_only = source.method == "vendor" and os.name == "nt"
        return {
            "provider": provider,
            "name": spec.name,
            "method": source.method,
            "source": url,
            "profile": profile,
            "proxy": public_proxy(policy),
            "proxy_source": scope,
            "directory": str(self.hub.store.root / "tools" / provider),
            "download_only": download_only,
            "requires": [f"Node.js ≥{source.node_min[0]}.{source.node_min[1]} + npm"]
            if source.method == "npm"
            else (["curl"] if download_only else ["curl", "bash"]),
        }

    @staticmethod
    def _run(command: list[str], env: dict[str, str], cwd: Path, timeout=900) -> str:
        extra_kwargs = {"start_new_session": True} if os.name != "nt" else hidden_popen_kwargs()
        process = subprocess.Popen(
            wrap_windows_command(command),
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **extra_kwargs,
        )
        try:
            output, _ = process.communicate(timeout=timeout)
        except BaseException:
            terminate_process_tree(
                process,
                os_mod=os,
                subprocess_mod=subprocess,
                shutil_mod=shutil,
            )
            raise
        if process.returncode:
            message = redact_output(output, env)[-3000:].strip()
            raise HubError(f"Установщик завершился с кодом {process.returncode}. {message}")
        return output

    @staticmethod
    def _npm_launcher() -> tuple[str, str]:
        node = shutil.which("node")
        npm = shutil.which("npm")
        if not node or not npm:
            raise HubError("Для установки этого harness нужны Node.js и npm")
        real = Path(npm).resolve()
        candidates = [
            real,
            Path(npm).parent / "node_modules/npm/bin/npm-cli.js",
            real.parent / "node_modules/npm/bin/npm-cli.js",
        ]
        script = next(
            (path for path in candidates if path.name == "npm-cli.js" and path.is_file()), None
        )
        if script is None:
            raise HubError("Не найден npm-cli.js; установи официальный Node.js с npm")
        return node, str(script)

    @staticmethod
    def _check_node(node: str, minimum: tuple[int, int], env: dict[str, str]) -> None:
        result = subprocess.run(
            [node, "--version"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        match = re.fullmatch(r"v(\d+)\.(\d+)\.\d+(?:[-+].*)?", result.stdout.strip())
        if result.returncode or not match or (int(match[1]), int(match[2])) < minimum:
            raise HubError(f"Для этого клиента нужен Node.js ≥{minimum[0]}.{minimum[1]}")

    def _environment(self, provider: str, profile: str | None) -> dict[str, str]:
        selected = self.hub.profile(provider, profile) if profile is not None else None
        policy, _ = self.hub.proxy_policy(provider, selected)
        env = proxy_environment(dict(os.environ), policy)
        for spec in self.hub.harnesses().values():
            for key in spec.auth_env:
                env.pop(key, None)
        for key in list(env):
            if key.lower().startswith(("npm_config_", "pip_", "uv_index", "uv_default_index")):
                env.pop(key)
        return env

    def _install_npm(
        self, source: InstallSource, directory: Path, env: dict[str, str]
    ) -> tuple[Path, list[str], str]:
        node, npm = self._npm_launcher()
        self._check_node(node, source.node_min, env)
        for name in ("npm-user.rc", "npm-global.rc"):
            path = directory / name
            path.write_text("", encoding="utf-8")
            path.chmod(0o600)
        env = dict(env)
        env["NPM_CONFIG_REGISTRY"] = NPM_REGISTRY
        command = [
            node,
            npm,
            "install",
            "--global",
            "--prefix",
            str(directory),
            "--registry",
            NPM_REGISTRY,
            "--userconfig",
            str(directory / "npm-user.rc"),
            "--globalconfig",
            str(directory / "npm-global.rc"),
            "--engine-strict",
            "--no-audit",
            "--no-fund",
            f"{source.source}@latest",
        ]
        self._run(command, env, directory)
        packages = directory / ("node_modules" if os.name == "nt" else "lib/node_modules")
        package = packages / source.source
        try:
            manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
            bins = manifest["bin"]
            relative = bins[source.command] if isinstance(bins, dict) else bins
            executable = (package / relative).resolve(strict=True)
            if not executable.is_relative_to(directory.resolve()):
                raise ValueError
            if executable.suffix in {".js", ".mjs", ".cjs"}:
                launcher = [node, str(executable)]
            else:
                with executable.open("rb") as stream:
                    header = stream.read(128)
                magic = header[:4]
                first_line = header.splitlines()[0] if header else b""
                if first_line.startswith(b"#!") and b"node" in first_line.lower():
                    launcher = [node, str(executable)]
                elif magic[:2] == b"MZ" or magic in {
                    b"\x7fELF",
                    b"\xcf\xfa\xed\xfe",
                    b"\xfe\xed\xfa\xcf",
                    b"\xca\xfe\xba\xbe",
                    b"\xbe\xba\xfe\xca",
                }:
                    launcher = [str(executable)]
                else:
                    raise HubError("Формат npm launcher изменился; установка не активирована")
            return executable, launcher, str(manifest.get("version", "unknown"))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise HubError("Установленный пакет не содержит ожидаемую команду") from exc

    @staticmethod
    def _download_script_urllib(url: str, path: Path, env: dict[str, str]) -> None:
        if urllib.parse.urlsplit(url).scheme.lower() != "https":
            raise HubError("Загрузка официального установщика разрешена только по HTTPS")
        proxies: dict[str, str] = {}
        for scheme, keys in (
            ("https", ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")),
            ("http", ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")),
        ):
            for key in keys:
                val = env.get(key)
                if val:
                    proxies[scheme] = val
                    break
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        req = urllib.request.Request(url, headers={"User-Agent": "HarnessHub/0.2"})
        try:
            with opener.open(req, timeout=30) as resp:
                final_url = resp.geturl()
                if urllib.parse.urlsplit(final_url).scheme.lower() != "https":
                    raise HubError("Официальный установщик перенаправил на небезопасный протокол")
                data = resp.read(4 * 1024 * 1024 + 1)
        except (urllib.error.URLError, OSError) as exc:
            msg = redact_output(str(exc), env)
            raise HubError(f"Не удалось скачать официальный установщик: {msg}") from exc
        if not 0 < len(data) <= 4 * 1024 * 1024:
            raise HubError("Официальный сервер не вернул установщик допустимого размера")
        path.write_bytes(data)

    def _download_script(self, url: str, path: Path, env: dict[str, str]) -> None:
        curl = shutil.which("curl")
        if curl is None:
            if os.name == "nt":
                self._download_script_urllib(url, path, env)
                return
            raise HubError("Для загрузки официального установщика нужен curl")
        command = [
            curl,
            "--disable",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--connect-timeout",
            "20",
            "--max-time",
            "120",
            "--max-filesize",
            str(4 * 1024 * 1024),
            "--output",
            str(path),
            url,
        ]
        self._run(command, env, path.parent, timeout=130)
        if not path.is_file() or not 0 < path.stat().st_size <= 4 * 1024 * 1024:
            raise HubError("Официальный сервер не вернул установщик допустимого размера")

    def _install_vendor(
        self, source: InstallSource, directory: Path, env: dict[str, str]
    ) -> tuple[Path, list[str], str]:
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            raise HubError("Запускай установку vendor-клиента от обычного пользователя")
        bash = shutil.which("bash")
        if not bash:
            raise HubError("Для официального установщика нужен bash")
        script = directory / "official-install.sh"
        self._download_script(source.source, script, env)
        home = directory / "home"
        home.mkdir(mode=0o700)
        env = dict(env)
        for key in (
            "INSTALL_DIR",
            "PREFIX",
            "XDG_BIN_HOME",
            "AGY_INSTALL_DIR",
            "CURSOR_INSTALL_DIR",
            "JUNIE_INSTALL_DIR",
            "KIRO_INSTALL_DIR",
        ):
            env.pop(key, None)
        env.update(
            HOME=str(home),
            USERPROFILE=str(home),
            XDG_CONFIG_HOME=str(home / ".config"),
            XDG_CACHE_HOME=str(home / ".cache"),
            XDG_DATA_HOME=str(home / ".local/share"),
        )
        self._run([bash, str(script)], env, directory)
        candidate = home / ".local/bin" / source.command
        if not candidate.is_file() and source.command == "agent":
            candidate = home / ".local/bin/cursor-agent"
        executable = candidate.resolve(strict=True)
        if not executable.is_relative_to(directory.resolve()) or not os.access(executable, os.X_OK):
            raise HubError("Установщик не создал ожидаемый исполняемый файл в каталоге менеджера")
        return executable, [str(executable)], "vendor-latest"

    def _cleanup_unpublished(self, directory: Path) -> None:
        try:
            with FileLock(self.hub.store.root / "state.lock"):
                state = self.hub.store.read()
                if not any(
                    i["directory"] == str(directory) for i in state["installations"].values()
                ):
                    shutil.rmtree(directory)
        except (HubError, OSError):
            pass

    def install(self, provider: str, profile: str | None = None) -> dict:
        plan = self.plan(provider, profile)
        source = SOURCES[provider]
        if (
            provider == "qoder"
            and os.name == "nt"
            and platform.machine().lower() in {"arm64", "aarch64"}
        ):
            raise HubError("Официальный Qoder CLI пока не поддерживает Windows ARM64")
        parent = self.hub.store.root / "tools" / provider
        with FileLock(resource_lock(parent / ".installation", self.hub.lock_root)):
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="generation-", dir=parent))
            try:
                env = self._environment(provider, profile)
                if plan["download_only"]:
                    path = directory / "official-install.ps1"
                    self._download_script(plan["source"], path, env)
                    return {
                        "status": "downloaded",
                        "provider": provider,
                        "path": str(path),
                        "source": plan["source"],
                        "note": (
                            "Официальный Windows-установщик скачан; запусти: "
                            f'powershell -ExecutionPolicy Bypass -File "{path}"'
                        ),
                    }
                if source.method == "npm":
                    executable, launcher, version = self._install_npm(source, directory, env)
                else:
                    executable, launcher, version = self._install_vendor(source, directory, env)
                if not executable.is_file():
                    raise HubError("Установка не создала ожидаемый исполняемый файл")
                installation = {
                    "directory": str(directory),
                    "executable": str(executable),
                    "launcher": launcher,
                    "source": plan["source"],
                    "method": source.method,
                    "version": version,
                    "installed_at": datetime.now(UTC).isoformat(),
                    "env": {"JUNIE_DATA": str(directory / "home/.local/share/junie")}
                    if provider == "junie"
                    else {},
                }

                def publish(state):
                    state["installations"][provider] = installation
                    if provider == "agy":
                        state["settings"].pop("agy_binary", None)

                self.hub.store.update(publish)
                return {"status": "installed", "provider": provider, **installation}
            except BaseException:
                self._cleanup_unpublished(directory)
                raise
