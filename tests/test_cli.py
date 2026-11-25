import json
from pathlib import Path

import pytest

from harnesshub.cli import main


def test_cli_profile_flow_and_machine_output(tmp_path, capsys):
    args = ["--data-dir", str(tmp_path / "hub"), "--json"]
    assert main(args + ["profiles", "new", "codex", "work"]) == 0
    profile = json.loads(capsys.readouterr().out)
    assert Path(profile["path"]).is_dir()
    assert main(args + ["profiles", "list", "codex"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["default"] is True
    assert main(args + ["profiles", "rename", "codex", "work", "personal"]) == 0
    capsys.readouterr()
    assert main(args + ["profiles", "remove", "codex", "personal"]) == 0
    assert Path(profile["path"]).is_dir()


def test_cli_unknown_harness_returns_error_json(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path), "--json", "run", "missing"])
    assert code == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_cli_rejects_ambiguous_native_profile_selection():
    with pytest.raises(SystemExit) as exc:
        main(["login", "codex", "--native", "-p", "work"])
    assert exc.value.code == 2


def test_proxy_cli_hides_credentials_and_supports_profile_override(tmp_path, capsys):
    args = ["--data-dir", str(tmp_path / "hub"), "--json"]
    assert main(args + ["profiles", "new", "codex", "work"]) == 0
    capsys.readouterr()
    assert main(args + ["proxy", "global", "set", "http://secret-user:secret-pass@host:8080"]) == 0
    output = capsys.readouterr().out
    assert "secret-user" not in output and "secret-pass" not in output
    assert main(args + ["proxy", "profile", "codex", "work", "direct"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["effective"]["mode"] == "direct"


def test_install_dry_run_cli_downloads_nothing(tmp_path, capsys):
    root = tmp_path / "hub"
    assert main(["--data-dir", str(root), "--json", "install", "qoder", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["source"].startswith("https://registry.npmjs.org/")
    assert not root.exists()


def test_cli_parser_prog_uses_hhub_alias_when_invoked_as_hhub(monkeypatch):
    from harnesshub.cli import parser

    monkeypatch.setattr("sys.argv", ["/usr/local/bin/hhub", "--help"])
    assert parser().prog == "hhub"

    monkeypatch.setattr("sys.argv", ["/usr/local/bin/harnesshub", "--help"])
    assert parser().prog == "harnesshub"

    monkeypatch.setattr("sys.argv", ["pytest"])
    assert parser().prog == "harnesshub"
