"""Command approvals are command-specific; no path grant authorizes a shell."""
from __future__ import annotations

import json
from unittest.mock import Mock

from core.runtime.command_approval import classify_command, authorize_bash_command
from tools.builtin.bash import BashTool
from tools.permission_gate import PermissionGate


def test_command_classification_is_conservative():
    assert not classify_command("git status").approval_required
    assert classify_command("pwd").effect == "read"
    for command in [
        "git status && echo changed", "git -c alias.st='!rm -rf /' st",
        "python -c 'print(1)'", "npm install", "curl https://example.com",
        "echo yes > result.txt", "git status | tee out.txt",
    ]:
        assert classify_command(command).approval_required, command


def test_approval_binds_exact_command_not_cached_path():
    broker = Mock(return_value="granted")
    assert authorize_bash_command("python -V", broker)
    assert authorize_bash_command("python --version", broker)
    assert broker.call_count == 2
    first = broker.call_args_list[0].args[0]
    second = broker.call_args_list[1].args[0]
    assert "SHA256:" in first and first != second


def test_bash_denied_by_broker_never_starts_child(tmp_path, monkeypatch):
    tool = BashTool(project_root=tmp_path)
    gate = PermissionGate(project_root=tmp_path)
    gate._broker = Mock(return_value="denied")
    tool._permission_gate = gate
    from unittest.mock import patch
    with patch("tools.builtin.bash.run_process") as runner:
        out = json.loads(tool.run({"command": "python -V"}))
    assert out["status"] == "error"
    assert out["error"]["code"] == "ACCESS_DENIED"
    runner.assert_not_called()
    gate._broker.assert_called_once()


def test_bash_strict_mode_without_broker_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("RIDGE_BASH_APPROVAL_MODE", "strict")
    tool = BashTool(project_root=tmp_path)
    out = json.loads(tool.run({"command": "python -V"}))
    assert out["status"] == "error"
    assert out["error"]["code"] == "ACCESS_DENIED"
