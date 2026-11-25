import json

import pytest

from harnesshub.errors import HubError
from harnesshub.network import proxy_environment, redact_output, validate_proxy
from harnesshub.service import Hub


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path / "hub", lock_root=tmp_path / "locks")


def test_proxy_precedence_global_profile_direct_and_inherit(hub, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://shell-proxy:8000")
    hub.create("codex", "work")
    profile = hub.profile("codex", "work")
    assert hub.environment("codex", profile)["HTTPS_PROXY"] == "http://shell-proxy:8000"
    hub.set_proxy({"mode": "url", "url": "http://global-proxy:8001"})
    assert hub.environment("codex", profile)["HTTPS_PROXY"] == "http://global-proxy:8001"
    hub.set_proxy({"mode": "url", "url": "http://account-proxy:8002"}, "codex", "work")
    profile = hub.profile("codex", "work")
    assert hub.environment("codex", profile)["HTTPS_PROXY"] == "http://account-proxy:8002"
    hub.set_proxy({"mode": "direct"}, "codex", "work")
    env = hub.environment("codex", hub.profile("codex", "work"))
    assert env["HTTPS_PROXY"] == env["https_proxy"] == ""
    assert env["NO_PROXY"] == env["no_proxy"] == "*"
    hub.set_proxy({"mode": "inherit"}, "codex", "work")
    assert hub.environment("codex", hub.profile("codex", "work"))["HTTPS_PROXY"] == (
        "http://global-proxy:8001"
    )


def test_url_policy_defeats_inherited_wildcard_and_preserves_parent_env():
    parent = {"HtTp_PrOxY": "http://old", "NO_PROXY": "*", "KEEP": "unchanged"}
    result = proxy_environment(parent, {"mode": "url", "url": "https://proxy:443"})
    assert "HtTp_PrOxY" not in result
    assert result["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert result["KEEP"] == "unchanged"
    assert parent["NO_PROXY"] == "*"


def test_global_native_launch_and_profile_bypass(hub):
    hub.set_proxy({"mode": "url", "url": "http://global:80"})
    hub.create("kimi", "work")
    hub.set_proxy({"mode": "inherit", "no_proxy": "localhost,internal.example"}, "kimi", "work")
    assert hub.environment("claude")["HTTP_PROXY"] == "http://global:80"
    assert hub.environment("kimi", hub.profile("kimi", "work"))["NO_PROXY"] == (
        "localhost,internal.example"
    )


@pytest.mark.parametrize(
    "value",
    [
        "socks5://host:1080",
        "http://",
        "http://host:0",
        "http://host:99999",
        "http://host/path",
        "https://host#fragment",
        "http://host?query=1",
        "http://user:pass\n@host",
        "http://user:pass%0d%0a@host",
        "http://host port:80",
    ],
)
def test_invalid_proxy_is_rejected_without_echoing_credentials(value):
    with pytest.raises(HubError) as error:
        validate_proxy({"mode": "url", "url": value})
    assert value not in str(error.value)


def test_proxy_credentials_hidden_in_public_results_and_installer_errors(hub):
    url = "http://private-user:sensitive-password@proxy.example:3128"
    hub.create("codex", "work")
    global_info = hub.set_proxy({"mode": "url", "url": url})
    profile_info = hub.set_proxy({"mode": "url", "url": url}, "codex", "work")
    output = json.dumps([global_info, profile_info, hub.profiles()])
    assert "private-user" not in output and "sensitive-password" not in output
    assert "***:***@proxy.example:3128" in output
    assert hub.environment("codex", hub.profile("codex", "work"))["HTTPS_PROXY"] == url
    sanitized = redact_output(f"failed {url}, password=sensitive-password", {"HTTPS_PROXY": url})
    assert "sensitive-password" not in sanitized and "private-user" not in sanitized


def test_v1_migration_is_read_only_until_atomic_mutation(hub):
    hub.store.root.mkdir()
    legacy = {
        "version": 1,
        "profiles": [],
        "custom": [],
        "defaults": {},
        "settings": {"agy_binary": "/some/existing/binary"},
    }
    text = json.dumps(legacy)
    hub.store.path.write_text(text)
    assert hub.store.read()["version"] == 2
    assert hub.store.path.read_text() == text
    hub.set_proxy({"mode": "direct"})
    persisted = json.loads(hub.store.path.read_text())
    assert persisted["version"] == 2
    assert persisted["settings"] == legacy["settings"]


def test_invalid_v2_proxy_does_not_overwrite_state(hub):
    hub.set_proxy({"mode": "direct"})
    state = json.loads(hub.store.path.read_text())
    state["network"]["global"] = {"mode": "url", "url": "invalid"}
    text = json.dumps(state)
    hub.store.path.write_text(text)
    with pytest.raises(HubError):
        hub.set_proxy({"mode": "inherit"})
    assert hub.store.path.read_text() == text
