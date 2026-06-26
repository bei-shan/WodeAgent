"""统一配置管理

所有环境变量读取集中在此类，通过 config.xxx 属性访问。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from core.env import load_env
from core.env_helpers import env_str, env_bool, env_int, env_float, env_int_optional

load_env()


class Config(BaseModel):
    """统一配置类 — 覆盖所有环境变量读取。

    使用方式:
        config = Config.from_env()
        # 访问: config.tool_output_max_lines, config.vcr_enabled, ...
    """

    # ===== LLM =====
    default_model: str = "gpt-3.5-turbo"
    default_provider: str = "openai"
    temperature: float = 0.7
    max_tokens: int | None = None
    llm_streaming: bool = True

    # ===== System =====
    debug: bool = False
    log_level: str = "INFO"
    show_react_steps: bool = True
    show_progress: bool = True

    # ===== Agent =====
    agent_interactive: bool = True
    max_steps: int = 50  # ReAct loop max steps

    # ===== AgentTeams =====
    enable_agent_teams: bool = False
    agent_teams_store_dir: str = ".teams"
    agent_tasks_store_dir: str = ".tasks"
    teammate_mode: str = "auto"
    delegate_mode: bool = False

    # ===== Context =====
    context_window: int = 128000
    compression_threshold: float = 0.8
    min_retain_rounds: int = 10
    summary_timeout: int = 120
    tool_message_format: str = "strict"

    # ===== Tool Output Truncation =====
    tool_output_max_lines: int = 2000
    tool_output_max_bytes: int = 51200
    tool_output_truncate_direction: str = "head"
    tool_output_head_tail_lines: int = 40
    tool_output_dir: str = "tool-output"
    tool_output_retention_days: int = 7

    # ===== Subagent =====
    subagent_max_steps: int = 15
    light_llm_model_id: str = ""
    light_llm_api_key: str = ""
    light_llm_base_url: str = ""
    light_llm_provider: str = "auto"
    light_llm_temperature: float = 0.5

    # ===== Worktree =====
    worktree_store_dir: str = ".worktrees"
    worktree_base_ref: str = "fresh"

    # ===== VCR =====
    vcr_enabled: bool = False
    vcr_record_mode: str = "new_episodes"
    vcr_fixture_dir: str = "tests/fixtures/vcr"

    # ===== Output Style =====
    output_style: str = "default"

    # ===== Trace =====
    trace_enabled: bool = True
    trace_dir: str = "memory/traces"
    trace_sanitize: bool = True

    # ===== MCP =====
    mcp_connect_mode: str = "manual"

    # ===== Circuit Breaker =====
    circuit_failure_threshold: int = 3
    circuit_recovery_timeout: int = 300

    # ===== Skills =====
    skills_refresh_on_call: bool = True
    skills_prompt_char_budget: int = 12000

    # ===== Background Task =====
    bg_task_output_dir: str = ".tasks/output"

    # ===== Team Advanced =====
    team_worker_max_steps: int = 8
    team_llm_max_concurrency: int = 4
    team_max_inbox_size: int = 10000
    team_max_work_items: int = 5000

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量加载所有配置。

        所有环境变量读取通过 env_helpers 统一入口，
        保证默认值一致、类型安全。
        """
        # AgentTeams 开关兼容 Claude Code 环境变量
        enable_teams_raw = env_str("ENABLE_AGENT_TEAMS", "")
        if not enable_teams_raw:
            enable_teams_raw = env_str("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "false")

        teammate_mode_raw = (env_str("TEAMMATE_MODE", "auto") or "auto").strip().lower()
        if teammate_mode_raw not in {"auto", "in-process", "tmux"}:
            teammate_mode_raw = "auto"

        return cls(
            # LLM
            default_model=env_str("LLM_MODEL_ID", "gpt-3.5-turbo"),
            default_provider=env_str("LLM_PROVIDER", "openai"),
            temperature=env_float("TEMPERATURE", 0.7),
            max_tokens=env_int_optional("MAX_TOKENS"),
            llm_streaming=env_bool("LLM_STREAMING", True),
            # System
            debug=env_bool("DEBUG", False),
            log_level=env_str("LOG_LEVEL", "INFO"),
            show_react_steps=env_bool("SHOW_REACT_STEPS", True),
            show_progress=env_bool("SHOW_PROGRESS", True),
            # Agent
            agent_interactive=env_bool("AGENT_INTERACTIVE", True),
            max_steps=env_int("MAX_STEPS", 50),
            # AgentTeams
            enable_agent_teams=enable_teams_raw.lower() in {"1", "true", "yes", "y", "on"},
            agent_teams_store_dir=env_str("AGENT_TEAMS_STORE_DIR", ".teams"),
            agent_tasks_store_dir=env_str("AGENT_TASKS_STORE_DIR", ".tasks"),
            teammate_mode=teammate_mode_raw,
            delegate_mode=env_bool("TEAM_DELEGATE_MODE", False),
            # Context
            context_window=env_int("CONTEXT_WINDOW", 128000),
            compression_threshold=env_float("COMPRESSION_THRESHOLD", 0.8),
            min_retain_rounds=env_int("MIN_RETAIN_ROUNDS", 10),
            summary_timeout=env_int("SUMMARY_TIMEOUT", 120),
            tool_message_format=env_str("TOOL_MESSAGE_FORMAT", "strict"),
            # Tool Output Truncation
            tool_output_max_lines=env_int("TOOL_OUTPUT_MAX_LINES", 2000),
            tool_output_max_bytes=env_int("TOOL_OUTPUT_MAX_BYTES", 51200),
            tool_output_truncate_direction=env_str("TOOL_OUTPUT_TRUNCATE_DIRECTION", "head"),
            tool_output_head_tail_lines=env_int("TOOL_OUTPUT_HEAD_TAIL_LINES", 40),
            tool_output_dir=env_str("TOOL_OUTPUT_DIR", "tool-output"),
            tool_output_retention_days=env_int("TOOL_OUTPUT_RETENTION_DAYS", 7),
            # Subagent
            subagent_max_steps=env_int("SUBAGENT_MAX_STEPS", 15),
            light_llm_model_id=env_str("LIGHT_LLM_MODEL_ID", ""),
            light_llm_api_key=env_str("LIGHT_LLM_API_KEY", ""),
            light_llm_base_url=env_str("LIGHT_LLM_BASE_URL", ""),
            light_llm_provider=env_str("LIGHT_LLM_PROVIDER", "auto"),
            light_llm_temperature=env_float("LIGHT_LLM_TEMPERATURE", 0.5),
            # Worktree
            worktree_store_dir=env_str("WORKTREE_STORE_DIR", ".worktrees"),
            worktree_base_ref=env_str("WORKTREE_BASE_REF", "fresh"),
            # VCR
            vcr_enabled=env_bool("VCR_ENABLED", False),
            vcr_record_mode=env_str("VCR_RECORD_MODE", "new_episodes"),
            vcr_fixture_dir=env_str("VCR_FIXTURE_DIR", "tests/fixtures/vcr"),
            # Output Style
            output_style=env_str("AGENT_OUTPUT_STYLE", "default"),
            # Trace
            trace_enabled=env_bool("TRACE_ENABLED", True),
            trace_dir=env_str("TRACE_DIR", "memory/traces"),
            trace_sanitize=env_bool("TRACE_SANITIZE", True),
            # MCP
            mcp_connect_mode=env_str("MCP_CONNECT_MODE", "manual"),
            # Circuit Breaker
            circuit_failure_threshold=env_int("CIRCUIT_FAILURE_THRESHOLD", 3),
            circuit_recovery_timeout=env_int("CIRCUIT_RECOVERY_TIMEOUT", 300),
            # Skills
            skills_refresh_on_call=env_bool("SKILLS_REFRESH_ON_CALL", True),
            skills_prompt_char_budget=env_int("SKILLS_PROMPT_CHAR_BUDGET", 12000),
            # Background Task
            bg_task_output_dir=env_str("BG_TASK_OUTPUT_DIR", ".tasks/output"),
            # Team Advanced
            team_worker_max_steps=env_int("TEAM_WORKER_MAX_STEPS", 8),
            team_llm_max_concurrency=env_int("TEAM_LLM_MAX_CONCURRENCY", 4),
            team_max_inbox_size=env_int("TEAM_MAX_INBOX_SIZE", 10000),
            team_max_work_items=env_int("TEAM_MAX_WORK_ITEMS", 5000),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（兼容旧接口）。"""
        return self.model_dump()
