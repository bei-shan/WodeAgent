"""Tests for the Hook System."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.hook_system import (
    HookManager,
    Hook,
    HookMatcher,
    HookConfig,
    HookResult,
    _SESSION_ID,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_hooks_config(project_root: str, hooks_dict: dict) -> Path:
    """Write a .mycode/hooks.json file and return its path."""
    mycode_dir = Path(project_root) / ".mycode"
    mycode_dir.mkdir(parents=True, exist_ok=True)
    config_path = mycode_dir / "hooks.json"
    config_path.write_text(json.dumps(hooks_dict, ensure_ascii=False, indent=2))
    return config_path


def _make_echo_hook_command(message: str) -> str:
    """Return a shell command that echoes a JSON result.

    Uses a temp file to avoid shell quoting issues with JSON on Windows.
    """
    import tempfile as _tmp
    tmp = _tmp.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(message, tmp)
    tmp.close()
    # Use forward slashes to avoid Windows backslash escaping in python -c.
    safe_path = tmp.name.replace("\\", "/")
    return (
        f'python -c "import json;'
        f" print(json.dumps(json.load(open(r'{safe_path}', encoding='utf-8'))))\""
    )


def _make_block_hook_command(reason: str = "blocked by test") -> str:
    """Return a shell command that exits with code 2."""
    if os.name == "nt":
        return f'python -c "import sys; sys.stderr.write(\'{reason}\'); sys.exit(2)"'
    return f"(echo '{reason}' >&2; exit 2)"


def _make_warning_hook_command(message: str = "warning message") -> str:
    """Return a shell command that exits with code 1."""
    if os.name == "nt":
        return f'python -c "import sys; sys.stderr.write(\'{message}\'); sys.exit(1)"'
    return f"(echo '{message}' >&2; exit 1)"


# ---------------------------------------------------------------------------
# Matcher tests
# ---------------------------------------------------------------------------

class TestMatcher:
    """Test the hook matcher logic."""

    def test_star_matches_everything(self):
        assert HookManager._matcher_matches("*", "Bash") is True
        assert HookManager._matcher_matches("*", "Write") is True
        assert HookManager._matcher_matches("*", "") is True

    def test_exact_match(self):
        assert HookManager._matcher_matches("Bash", "Bash") is True
        assert HookManager._matcher_matches("Bash", "bash") is False
        assert HookManager._matcher_matches("Bash", "Write") is False

    def test_glob_match(self):
        assert HookManager._matcher_matches("Team*", "TeamCreate") is True
        assert HookManager._matcher_matches("Team*", "TeamFanout") is True
        assert HookManager._matcher_matches("Team*", "SendMessage") is False
        assert HookManager._matcher_matches("*Write*", "TodoWrite") is True
        assert HookManager._matcher_matches("*Write*", "Write") is True
        assert HookManager._matcher_matches("*Write*", "Read") is False

    def test_match_method(self):
        matchers = [
            HookMatcher(matcher="Bash", hooks=[]),
            HookMatcher(matcher="Write", hooks=[]),
            HookMatcher(matcher="Team*", hooks=[]),
        ]
        result = HookManager._match(matchers, "Bash")
        assert len(result) == 1
        assert result[0].matcher == "Bash"

        result = HookManager._match(matchers, "TeamFanout")
        assert len(result) == 1
        assert result[0].matcher == "Team*"

        result = HookManager._match(matchers, "Read")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestConfigLoading:
    """Test hook configuration loading."""

    def test_no_config_file_means_no_hooks(self, tmp_path):
        mgr = HookManager(project_root=str(tmp_path))
        assert mgr.has_any_hooks is False

    def test_empty_hooks_json(self, tmp_path):
        _write_hooks_config(str(tmp_path), {"hooks": {}})
        mgr = HookManager(project_root=str(tmp_path))
        assert mgr.has_any_hooks is False

    def test_pre_tool_use_config(self, tmp_path):
        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{
                        "type": "command",
                        "command": "echo ok",
                        "timeout": 10,
                    }],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        assert mgr.has_any_hooks is True
        assert len(mgr._config.pre_tool_use) == 1
        assert mgr._config.pre_tool_use[0].matcher == "Bash"
        assert mgr._config.pre_tool_use[0].hooks[0].command == "echo ok"
        assert mgr._config.pre_tool_use[0].hooks[0].timeout == 10

    def test_invalid_json_falls_back_gracefully(self, tmp_path):
        mycode_dir = Path(tmp_path) / ".mycode"
        mycode_dir.mkdir(parents=True)
        (mycode_dir / "hooks.json").write_text("not json {{{")
        mgr = HookManager(project_root=str(tmp_path))
        assert mgr.has_any_hooks is False

    def test_reload_discovers_new_config(self, tmp_path):
        mgr = HookManager(project_root=str(tmp_path))
        assert mgr.has_any_hooks is False

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "SessionEnd": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "echo done"}],
                }],
            },
        })
        mgr.reload()
        assert mgr.has_any_hooks is True


# ---------------------------------------------------------------------------
# Hook execution
# ---------------------------------------------------------------------------

class TestHookExecution:
    """Test actual hook command execution."""

    def test_pre_tool_use_allow(self, tmp_path):
        """A hook that returns JSON with no decision → tool is allowed."""
        cmd = _make_echo_hook_command({"system_message": "audit passed"})

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": cmd}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_pre_tool_use("Bash", {"command": "ls"})
        assert result.blocked is False
        assert len(result.system_messages) == 1
        assert "audit passed" in result.system_messages[0]

    def test_pre_tool_use_block_exit_2(self, tmp_path):
        """Exit code 2 should block the tool."""
        cmd = _make_block_hook_command("dangerous command")
        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": cmd}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_pre_tool_use("Bash", {"command": "rm -rf /"})
        assert result.blocked is True
        assert "dangerous" in result.reason.lower()

    def test_pre_tool_use_block_json(self, tmp_path):
        """JSON output with decision: block should block the tool."""
        cmd = _make_echo_hook_command({"decision": "block", "reason": "not allowed"})

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Write",
                    "hooks": [{"type": "command", "command": cmd}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_pre_tool_use("Write", {"file_path": "/tmp/test.py"})
        assert result.blocked is True
        assert result.reason == "not allowed"

    def test_pre_tool_use_updated_input(self, tmp_path):
        """Hook can modify tool input via updated_input."""
        cmd = _make_echo_hook_command({"updated_input": {"command": "echo safe"}})

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": cmd}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_pre_tool_use("Bash", {"command": "rm -rf /"})
        assert result.blocked is False
        assert result.updated_input == {"command": "echo safe"}

    def test_post_tool_use_cannot_block(self, tmp_path):
        """PostToolUse hooks should never block."""
        cmd = _make_echo_hook_command({"decision": "block", "reason": "tried to block"})

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "PostToolUse": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": cmd}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_post_tool_use("Bash", {"command": "ls"}, "file1\nfile2")
        assert result.blocked is False

    def test_no_matching_matcher_returns_empty(self, tmp_path):
        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo nope"}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_pre_tool_use("Write", {"file_path": "/x"})
        assert result.blocked is False
        assert result.system_messages == []


class TestSessionHooks:
    """Test SessionStart and SessionEnd hooks."""

    def test_session_start_hook(self, tmp_path):
        cmd = _make_echo_hook_command({"additional_context": "project: my-project v2.0"})

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "SessionStart": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": cmd}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_session_start()
        assert result.blocked is False
        assert len(result.additional_context) == 1
        assert "my-project" in result.additional_context[0]

    def test_session_end_hook(self, tmp_path):
        cmd = _make_echo_hook_command({"system_message": "session finished"})

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "SessionEnd": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": cmd}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_session_end()
        assert result.blocked is False
        assert len(result.system_messages) == 1

    def test_session_end_cannot_block(self, tmp_path):
        cmd = _make_echo_hook_command({"decision": "block", "reason": "cannot exit"})

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "SessionEnd": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": cmd}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_session_end()
        assert result.blocked is False


class TestConcurrentHooks:
    """Test that multiple hooks run concurrently."""

    def test_multiple_hooks_merge_results(self, tmp_path):
        cmd1 = _make_echo_hook_command({"system_message": "msg from hook 1"})
        cmd2 = _make_echo_hook_command({"system_message": "msg from hook 2"})

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": cmd1},
                        {"type": "command", "command": cmd2},
                    ],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_pre_tool_use("Bash", {"command": "ls"})
        assert result.blocked is False
        assert len(result.system_messages) == 2

    def test_one_block_blocks_all(self, tmp_path):
        block_cmd = _make_block_hook_command("security violation")
        allow_cmd = _make_echo_hook_command({"system_message": "allowed"})

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": allow_cmd},
                        {"type": "command", "command": block_cmd},
                    ],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_pre_tool_use("Bash", {"command": "ls"})
        assert result.blocked is True


class TestHookResult:
    """Test HookResult dataclass."""

    def test_default_result(self):
        r = HookResult()
        assert r.blocked is False
        assert r.reason == ""
        assert r.system_messages == []
        assert r.updated_input is None

    def test_blocked_result(self):
        r = HookResult(blocked=True, reason="test block")
        assert r.blocked is True
        assert r.reason == "test block"

    def test_merged_messages(self):
        r = HookResult(system_messages=["a", "b"], warnings=["w1"])
        assert len(r.system_messages) == 2
        assert len(r.warnings) == 1


class TestHookTimeout:
    """Test hook timeout handling."""

    def test_timeout_returns_warning(self, tmp_path):
        """A hook that sleeps longer than timeout should return warning."""
        if os.name == "nt":
            cmd = 'python -c "import time; time.sleep(5)"'
        else:
            cmd = "sleep 5"

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": cmd, "timeout": 1}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_pre_tool_use("Bash", {"command": "ls"})
        assert result.blocked is False
        assert len(result.warnings) >= 1


class TestEnvFile:
    """Test SessionStart env file handling."""

    def test_env_file_injection(self, tmp_path):
        """SessionStart hook writes to MYCODE_ENV_FILE."""
        if os.name == "nt":
            cmd = (
                'python -c "import os; '
                'p = os.environ.get(\'MYCODE_ENV_FILE\',\'\'); '
                'open(p, \'w\').write(\'export MY_VAR=hello\\n\') if p else None; '
                'print(\'{\\\"additional_context\\\": \\\"done\\\"}\')"'
            )
        else:
            cmd = (
                'python3 -c "'
                'import os; '
                'p = os.environ.get(\'MYCODE_ENV_FILE\',\'\'); '
                'open(p, \'w\').write(\'export MY_VAR=hello\\n\') if p else None; '
                'print(\'{\\\"additional_context\\\": \\\"done\\\"}\')"'
            )

        _write_hooks_config(str(tmp_path), {
            "hooks": {
                "SessionStart": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": cmd}],
                }],
            },
        })
        mgr = HookManager(project_root=str(tmp_path))
        result = mgr.run_session_start()
        assert result.blocked is False
        # MY_VAR should be set if env file was processed.
        # Note: env file processing happens inside run_session_start.
        assert os.environ.get("MY_VAR") == "hello"
