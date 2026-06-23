"""Tests for unified Config."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from core.config import Config


# ---------------------------------------------------------------------------
# from_env() — basic construction
# ---------------------------------------------------------------------------

def test_from_env_defaults():
    """All fields have their documented defaults when no env vars are set."""
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    assert config.default_provider == "openai"
    assert config.default_model == "gpt-3.5-turbo"
    assert config.temperature == 0.7
    assert config.max_tokens is None
    assert config.debug is False
    assert config.log_level == "INFO"
    assert config.show_react_steps is True
    assert config.show_progress is True
    assert config.agent_interactive is True
    assert config.enable_agent_teams is False
    assert config.agent_teams_store_dir == ".teams"
    assert config.agent_tasks_store_dir == ".tasks"
    assert config.teammate_mode == "auto"
    assert config.delegate_mode is False
    assert config.context_window == 128000
    assert config.compression_threshold == 0.8
    assert config.min_retain_rounds == 10
    assert config.summary_timeout == 120
    assert config.tool_message_format == "strict"


# ---------------------------------------------------------------------------
# Tool Output Truncation
# ---------------------------------------------------------------------------

def test_tool_output_defaults():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    assert config.tool_output_max_lines == 2000
    assert config.tool_output_max_bytes == 51200
    assert config.tool_output_truncate_direction == "head"
    assert config.tool_output_head_tail_lines == 40
    assert config.tool_output_dir == "tool-output"
    assert config.tool_output_retention_days == 7


def test_tool_output_from_env():
    env = {
        "TOOL_OUTPUT_MAX_LINES": "500",
        "TOOL_OUTPUT_MAX_BYTES": "10240",
        "TOOL_OUTPUT_TRUNCATE_DIRECTION": "tail",
        "TOOL_OUTPUT_HEAD_TAIL_LINES": "20",
        "TOOL_OUTPUT_DIR": "custom-output",
        "TOOL_OUTPUT_RETENTION_DAYS": "14",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.tool_output_max_lines == 500
    assert config.tool_output_max_bytes == 10240
    assert config.tool_output_truncate_direction == "tail"
    assert config.tool_output_head_tail_lines == 20
    assert config.tool_output_dir == "custom-output"
    assert config.tool_output_retention_days == 14


# ---------------------------------------------------------------------------
# Subagent
# ---------------------------------------------------------------------------

def test_subagent_defaults():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    assert config.subagent_max_steps == 15
    assert config.light_llm_model_id == ""
    assert config.light_llm_api_key == ""
    assert config.light_llm_base_url == ""
    assert config.light_llm_provider == "auto"
    assert config.light_llm_temperature == 0.5


def test_subagent_from_env():
    env = {
        "SUBAGENT_MAX_STEPS": "30",
        "LIGHT_LLM_MODEL_ID": "gpt-4o-mini",
        "LIGHT_LLM_API_KEY": "sk-light",
        "LIGHT_LLM_BASE_URL": "https://api.openai.com/v1",
        "LIGHT_LLM_PROVIDER": "openai",
        "LIGHT_LLM_TEMPERATURE": "0.3",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.subagent_max_steps == 30
    assert config.light_llm_model_id == "gpt-4o-mini"
    assert config.light_llm_api_key == "sk-light"
    assert config.light_llm_base_url == "https://api.openai.com/v1"
    assert config.light_llm_provider == "openai"
    assert config.light_llm_temperature == 0.3


# ---------------------------------------------------------------------------
# Worktree
# ---------------------------------------------------------------------------

def test_worktree_defaults():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    assert config.worktree_store_dir == ".worktrees"
    assert config.worktree_base_ref == "fresh"


def test_worktree_from_env():
    env = {"WORKTREE_STORE_DIR": "custom-worktrees", "WORKTREE_BASE_REF": "main"}
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.worktree_store_dir == "custom-worktrees"
    assert config.worktree_base_ref == "main"


# ---------------------------------------------------------------------------
# VCR
# ---------------------------------------------------------------------------

def test_vcr_defaults():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    assert config.vcr_enabled is False
    assert config.vcr_record_mode == "new_episodes"
    assert config.vcr_fixture_dir == "tests/fixtures/vcr"


def test_vcr_from_env():
    env = {
        "VCR_ENABLED": "true",
        "VCR_RECORD_MODE": "once",
        "VCR_FIXTURE_DIR": "custom/fixtures",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.vcr_enabled is True
    assert config.vcr_record_mode == "once"
    assert config.vcr_fixture_dir == "custom/fixtures"


# ---------------------------------------------------------------------------
# Output Style
# ---------------------------------------------------------------------------

def test_output_style_default():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()
    assert config.output_style == "default"


def test_output_style_from_env():
    with patch.dict(os.environ, {"AGENT_OUTPUT_STYLE": "explanatory"}, clear=True):
        config = Config.from_env()
    assert config.output_style == "explanatory"


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------

def test_trace_defaults():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    assert config.trace_enabled is True
    assert config.trace_dir == "memory/traces"
    assert config.trace_sanitize is True


def test_trace_from_env():
    env = {"TRACE_ENABLED": "false", "TRACE_DIR": "custom/traces", "TRACE_SANITIZE": "false"}
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.trace_enabled is False
    assert config.trace_dir == "custom/traces"
    assert config.trace_sanitize is False


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

def test_circuit_breaker_defaults():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    assert config.circuit_failure_threshold == 3
    assert config.circuit_recovery_timeout == 300


def test_circuit_breaker_from_env():
    env = {"CIRCUIT_FAILURE_THRESHOLD": "5", "CIRCUIT_RECOVERY_TIMEOUT": "600"}
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.circuit_failure_threshold == 5
    assert config.circuit_recovery_timeout == 600


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def test_skills_defaults():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    assert config.skills_refresh_on_call is True
    assert config.skills_prompt_char_budget == 12000


def test_skills_from_env():
    env = {"SKILLS_REFRESH_ON_CALL": "false", "SKILLS_PROMPT_CHAR_BUDGET": "8000"}
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.skills_refresh_on_call is False
    assert config.skills_prompt_char_budget == 8000


# ---------------------------------------------------------------------------
# Background Task
# ---------------------------------------------------------------------------

def test_bg_task_default():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()
    assert config.bg_task_output_dir == ".tasks/output"


def test_bg_task_from_env():
    with patch.dict(os.environ, {"BG_TASK_OUTPUT_DIR": "custom/tasks"}, clear=True):
        config = Config.from_env()
    assert config.bg_task_output_dir == "custom/tasks"


# ---------------------------------------------------------------------------
# Team Advanced
# ---------------------------------------------------------------------------

def test_team_advanced_defaults():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    assert config.team_worker_max_steps == 8
    assert config.team_llm_max_concurrency == 4
    assert config.team_max_inbox_size == 10000
    assert config.team_max_work_items == 5000


def test_team_advanced_from_env():
    env = {
        "TEAM_WORKER_MAX_STEPS": "16",
        "TEAM_LLM_MAX_CONCURRENCY": "8",
        "TEAM_MAX_INBOX_SIZE": "5000",
        "TEAM_MAX_WORK_ITEMS": "2000",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.team_worker_max_steps == 16
    assert config.team_llm_max_concurrency == 8
    assert config.team_max_inbox_size == 5000
    assert config.team_max_work_items == 2000


# ---------------------------------------------------------------------------
# AgentTeams flags
# ---------------------------------------------------------------------------

def test_agent_teams_disabled_by_default():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()
    assert config.enable_agent_teams is False


def test_agent_teams_enabled():
    with patch.dict(os.environ, {"ENABLE_AGENT_TEAMS": "true"}, clear=True):
        config = Config.from_env()
    assert config.enable_agent_teams is True


def test_agent_teams_claude_code_compat():
    """CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS acts as fallback."""
    with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}, clear=True):
        config = Config.from_env()
    assert config.enable_agent_teams is True


def test_teammate_mode_validates():
    with patch.dict(os.environ, {"TEAMMATE_MODE": "invalid"}, clear=True):
        config = Config.from_env()
    assert config.teammate_mode == "auto"  # falls back to auto


def test_delegate_mode():
    with patch.dict(os.environ, {"TEAM_DELEGATE_MODE": "on"}, clear=True):
        config = Config.from_env()
    assert config.delegate_mode is True


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

def test_agent_interactive_default():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()
    assert config.agent_interactive is True


def test_agent_interactive_disabled():
    with patch.dict(os.environ, {"AGENT_INTERACTIVE": "false"}, clear=True):
        config = Config.from_env()
    assert config.agent_interactive is False


# ---------------------------------------------------------------------------
# LLM env var overrides
# ---------------------------------------------------------------------------

def test_llm_from_env():
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_MODEL_ID": "deepseek-chat",
        "TEMPERATURE": "0.3",
        "MAX_TOKENS": "2048",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.default_provider == "deepseek"
    assert config.default_model == "deepseek-chat"
    assert config.temperature == 0.3
    assert config.max_tokens == 2048


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def test_context_from_env():
    env = {
        "CONTEXT_WINDOW": "64000",
        "COMPRESSION_THRESHOLD": "0.7",
        "MIN_RETAIN_ROUNDS": "5",
        "SUMMARY_TIMEOUT": "60",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.context_window == 64000
    assert config.compression_threshold == 0.7
    assert config.min_retain_rounds == 5
    assert config.summary_timeout == 60


# ---------------------------------------------------------------------------
# to_dict()
# ---------------------------------------------------------------------------

def test_to_dict_returns_all_fields():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    d = config.to_dict()
    assert isinstance(d, dict)
    assert d["context_window"] == 128000
    assert d["enable_agent_teams"] is False
    assert d["output_style"] == "default"
    assert d["vcr_enabled"] is False
    assert d["subagent_max_steps"] == 15
    assert d["circuit_failure_threshold"] == 3
