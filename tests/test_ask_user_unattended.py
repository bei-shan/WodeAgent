"""AskUser must not hang unattended API/hosted workers on built-in input()."""
from __future__ import annotations

import json
from unittest.mock import Mock

from tools.builtin.ask_user import AskUserTool


def test_ask_user_without_tty_or_adapter_fails_closed(tmp_path, monkeypatch):
    tool = AskUserTool(project_root=tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    out = json.loads(tool.run({
        "questions": [{"id": "q1", "text": "Choose an action"}]
    }))
    assert out["status"] == "error"
    assert out["error"]["code"] == "ASK_USER_UNAVAILABLE"


def test_ask_user_broker_is_used_instead_of_terminal_input(tmp_path, monkeypatch):
    tool = AskUserTool(project_root=tmp_path)
    tool._input_func = Mock(return_value="approved")
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("no terminal input")))
    out = json.loads(tool.run({
        "questions": [{"id": "q1", "text": "Choose an action"}]
    }))
    assert out["status"] == "success"
    assert out["data"]["answers"] == [{"id": "q1", "answer": "approved"}]
    tool._input_func.assert_called_once()


def test_ask_user_bad_questions_are_rejected_before_broker(tmp_path):
    tool = AskUserTool(project_root=tmp_path)
    tool._input_func = Mock(return_value="no")
    for questions in (
        [],
        [{}],
        [{"text": "x" * 1300}],
        [{"text": "valid"}, {"bad": "invalid"}],
        [{"text": "valid"}] * 5,
    ):
        out = json.loads(tool.run({"questions": questions}))
        assert out["status"] == "error"
        assert out["error"]["code"] == "INVALID_PARAM"
    tool._input_func.assert_not_called()
