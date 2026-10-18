"""Canonical command interface is deterministic, safe and local-only."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from ridgecode import cli


def test_cli_help_and_subcommand_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for name in ("setup", "doctor", "serve", "web", "tui", "eval", "soak"):
        assert name in output


def test_server_default_is_loopback_and_factory(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(
        run=lambda *args, **kw: calls.append((args, kw)),
    ))
    assert cli.main(["serve"]) == 0
    assert calls == [(("desktop.service.app:create_app",), {
        "factory": True, "host": "127.0.0.1",
        "port": 8000, "reload": False,
    })]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.0.1", "public.example"])
def test_public_bind_never_reaches_server(host, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(
        run=lambda *args, **kw: pytest.fail("Untrusted public bind"),
    ))
    assert cli.main(["serve", "--host", host]) == 2
    assert "loopback" in capsys.readouterr().err


def test_invalid_port_refused_before_server(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(
        run=lambda *args, **kw: pytest.fail("Invalid bind"),
    ))
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--port", "65536"])
    assert exc.value.code == 2


def test_setup_refuses_overwriting_existing_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("KEEP_ME_SECRET=1\n")
    from scripts import first_run_wizard
    monkeypatch.setattr(first_run_wizard, "run_wizard",
                        lambda: pytest.fail("Existing config must be guarded"))
    assert cli.main(["setup"]) == 2
    assert Path(".env").read_text() == "KEEP_ME_SECRET=1\n"


def test_setup_explicit_force_runs_interactive_wizard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("old\n")
    from scripts import first_run_wizard
    calls = []
    monkeypatch.setattr(first_run_wizard, "run_wizard", lambda: calls.append(True) or True)
    assert cli.main(["setup", "--force"]) == 0
    assert len(calls) == 1


def test_subcommands_route_without_shell_interpolation(monkeypatch):
    import scripts.v01_preflight
    import scripts.agent_eval
    import scripts.agent_soak
    calls = []
    monkeypatch.setattr(scripts.v01_preflight, "main",
                        lambda a: calls.append(("doctor", a)) or 0)
    monkeypatch.setattr(scripts.agent_eval, "main",
                        lambda a: calls.append(("eval", a)) or 0)
    monkeypatch.setattr(scripts.agent_soak, "main",
                        lambda a: calls.append(("soak", a)) or 0)
    assert cli.main(["doctor", "--target", "all", "--json"]) == 0
    assert cli.main(["eval", "--case", "outside_workspace_denial", "--json"]) == 0
    assert cli.main(["soak", "--iterations", "2"]) == 0
    assert calls == [
        ("doctor", ["--target", "all", "--json"]),
        ("eval", ["--json", "--case", "outside_workspace_denial"]),
        ("soak", ["--duration-seconds", "1800", "--iterations", "2"]),
    ]


def test_ui_uses_fixed_project_paths_and_no_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, **kw: calls.append((argv, kw)) or
                        SimpleNamespace(returncode=0))
    assert cli.main(["web"]) == 0
    assert cli.main(["tui"]) == 0
    assert calls == [
        (["/usr/bin/npm", "run", "dev"], {"cwd": cli.ROOT / "desktop/web", "check": False}),
        (["/usr/bin/bun", "run", "dev"], {"cwd": cli.ROOT / "tui-v2", "check": False}),
    ]


def test_missing_ui_binary_refuses_launch(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["web"]) == 2
    assert cli.main(["tui"]) == 2


def test_ui_launches_the_executable_path_that_was_resolved(monkeypatch):
    """Windows resolves ``npm`` to ``npm.CMD``, which CreateProcess cannot find
    from the bare name, so the launcher must run the path ``which`` validated."""
    resolved = {"npm": "C:/node/npm.CMD", "bun": "C:/bun/bun.exe"}
    monkeypatch.setattr(cli.shutil, "which", lambda name: resolved[name])
    calls = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or
                        SimpleNamespace(returncode=0))
    assert cli.main(["web"]) == 0
    assert cli.main(["tui"]) == 0
    assert calls == [
        ["C:/node/npm.CMD", "run", "dev"],
        ["C:/bun/bun.exe", "run", "dev"],
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file ACLs differ from POSIX modes")
def test_setup_publishes_secrets_with_restricted_file_permissions(tmp_path, monkeypatch):
    import stat
    from scripts.first_run_wizard import _write_env

    monkeypatch.chdir(tmp_path)
    provider = {
        "name": "OpenAI", "key": "openai",
        "env_prefix": "OPENAI",
        "default_model": "test-model",
        "base_url": "https://example.invalid/v1",
    }
    _write_env(provider, "TEST_ONLY_FAKE_KEY", False)
    config = tmp_path / ".env"
    assert config.is_file()
    assert stat.S_IMODE(config.stat().st_mode) & 0o077 == 0
    assert "LLM_API_KEY=TEST_ONLY_FAKE_KEY" in config.read_text()
    assert list(tmp_path.glob(".ridge-env-*")) == []
