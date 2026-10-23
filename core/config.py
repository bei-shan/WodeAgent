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
    tool_parallel_reads_enabled: bool = True
    tool_max_concurrency: int = 4
    # Opt-in bounded deferred catalog; default keeps all existing tool schemas.
    tool_deferred_discovery_enabled: bool = False
    tool_discovery_max_promoted: int = 18

    # ===== Sandboxed Tool Execution (explicit opt-in) =====
    sandbox_mode: str = "local_trusted"  # local_trusted | docker | boxlite | e2b
    require_isolated_execution: bool = False  # opt-in hard policy for untrusted repositories
    # Experimental trusted-local browser; never enabled in hosted or shell-only isolation.
    browser_enabled: bool = False
    browser_allowed_origins: str = ""  # explicit comma-separated scheme://host:port
    browser_allow_loopback: bool = False  # explicit trusted local-development override
    sandbox_docker_image: str = "python:3.12-alpine"
    sandbox_workspace_snapshot_enabled: bool = False  # explicit opt-in until snapshot/exchange acceptance
    sandbox_memory: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = 64
    # Optional BoxLite hardware micro-VM; never falls back to host execution.
    sandbox_boxlite_image: str = "python:3.12-slim"
    sandbox_boxlite_memory_mib: int = 1024
    sandbox_boxlite_cpus: int = 2
    sandbox_boxlite_stdin_enabled: bool = False  # opt in after SDK/HVF/KVM acceptance
    sandbox_boxlite_warm_enabled: bool = False  # opt in after actual HVF/KVM validation
    sandbox_boxlite_warm_idle_seconds: int = 60
    # Remote E2B VM requires explicit single-gateway risk acceptance.
    # E2B_API_KEY is read ONLY by the provider, not copied into config/logs.
    sandbox_e2b_template: str = "code-interpreter-v1"
    sandbox_e2b_ttl_seconds: int = 300
    sandbox_e2b_capacity: int = 2
    sandbox_e2b_single_gateway_ack: bool = False
    sandbox_e2b_redis_ledger_enabled: bool = False
    sandbox_e2b_deployment: str = "default"

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
    summary_input_max_chars: int = 24000
    summary_output_max_chars: int = 10000
    context_anchor_max_chars: int = 1200
    context_response_reserve_tokens: int = 4096
    context_compaction_max_failures: int = 3
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

    # ===== Budget =====
    # When True, BudgetFeature.pre_tool_use blocks tool calls once
    # is_exceeded() returns True. Default False — existing behavior is
    # "track only", flipping enforcement on without warning would break
    # users who set generous budgets just for visibility.
    budget_enforce: bool = False

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
        teams_enabled = enable_teams_raw.lower() in {"1", "true", "yes", "y", "on"}

        # Team tools add ~5.6k chars of function schemas per request. Defer them
        # behind ToolSearch by default when teams are on, unless the operator
        # explicitly chose a discovery setting.
        discovery_raw = env_str("TOOL_DEFERRED_DISCOVERY_ENABLED", "")
        if discovery_raw:
            discovery_enabled = discovery_raw.lower() in {"1", "true", "yes", "y", "on"}
        else:
            discovery_enabled = teams_enabled

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
            tool_parallel_reads_enabled=env_bool("TOOL_PARALLEL_READS_ENABLED", True),
            tool_max_concurrency=max(1, min(8, env_int("TOOL_MAX_CONCURRENCY", 4))),
            tool_deferred_discovery_enabled=discovery_enabled,
            tool_discovery_max_promoted=max(1, min(48, env_int("TOOL_DISCOVERY_MAX_PROMOTED", 18))),
            sandbox_mode=env_str("AGENT_SANDBOX_MODE", "local_trusted"),
            require_isolated_execution=env_bool("AGENT_REQUIRE_ISOLATED_EXECUTION", False),
            browser_enabled=env_bool("AGENT_BROWSER_ENABLED", False),
            browser_allowed_origins=env_str("AGENT_BROWSER_ALLOWED_ORIGINS", ""),
            browser_allow_loopback=env_bool("AGENT_BROWSER_ALLOW_LOOPBACK", False),
            sandbox_docker_image=env_str("AGENT_SANDBOX_DOCKER_IMAGE", "python:3.12-alpine"),
            sandbox_workspace_snapshot_enabled=env_bool("AGENT_SANDBOX_WORKSPACE_SNAPSHOT", False),
            sandbox_memory=env_str("AGENT_SANDBOX_MEMORY", "512m"),
            sandbox_cpus=env_float("AGENT_SANDBOX_CPUS", 1.0),
            sandbox_pids_limit=env_int("AGENT_SANDBOX_PIDS_LIMIT", 64),
            sandbox_boxlite_image=env_str("AGENT_SANDBOX_BOXLITE_IMAGE", "python:3.12-slim"),
            sandbox_boxlite_memory_mib=env_int("AGENT_SANDBOX_BOXLITE_MEMORY_MIB", 1024),
            sandbox_boxlite_cpus=env_int("AGENT_SANDBOX_BOXLITE_CPUS", 2),
            sandbox_boxlite_stdin_enabled=env_bool("AGENT_SANDBOX_BOXLITE_STDIN_ENABLED", False),
            sandbox_boxlite_warm_enabled=env_bool("AGENT_SANDBOX_BOXLITE_WARM_ENABLED", False),
            sandbox_boxlite_warm_idle_seconds=env_int("AGENT_SANDBOX_BOXLITE_WARM_IDLE_SECONDS", 60),
            sandbox_e2b_template=env_str("AGENT_SANDBOX_E2B_TEMPLATE", "code-interpreter-v1"),
            sandbox_e2b_ttl_seconds=env_int("AGENT_SANDBOX_E2B_TTL_SECONDS", 300),
            sandbox_e2b_capacity=env_int("AGENT_SANDBOX_E2B_CAPACITY", 2),
            sandbox_e2b_single_gateway_ack=env_bool("AGENT_SANDBOX_E2B_SINGLE_GATEWAY_ACK", False),
            sandbox_e2b_redis_ledger_enabled=env_bool("AGENT_SANDBOX_E2B_REDIS_LEDGER_ENABLED", False),
            sandbox_e2b_deployment=env_str("AGENT_SANDBOX_E2B_DEPLOYMENT", "default"),
            # AgentTeams
            enable_agent_teams=teams_enabled,
            agent_teams_store_dir=env_str("AGENT_TEAMS_STORE_DIR", ".teams"),
            agent_tasks_store_dir=env_str("AGENT_TASKS_STORE_DIR", ".tasks"),
            teammate_mode=teammate_mode_raw,
            delegate_mode=env_bool("TEAM_DELEGATE_MODE", False),
            # Context
            context_window=env_int("CONTEXT_WINDOW", 128000),
            compression_threshold=env_float("COMPRESSION_THRESHOLD", 0.8),
            min_retain_rounds=env_int("MIN_RETAIN_ROUNDS", 10),
            summary_timeout=env_int("SUMMARY_TIMEOUT", 120),
            summary_input_max_chars=env_int("SUMMARY_INPUT_MAX_CHARS", 24000),
            summary_output_max_chars=env_int("SUMMARY_OUTPUT_MAX_CHARS", 10000),
            context_anchor_max_chars=env_int("CONTEXT_ANCHOR_MAX_CHARS", 1200),
            context_response_reserve_tokens=env_int("CONTEXT_RESPONSE_RESERVE_TOKENS", 4096),
            context_compaction_max_failures=env_int("CONTEXT_COMPACTION_MAX_FAILURES", 3),
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
            # Budget
            budget_enforce=env_bool("BUDGET_ENFORCE", False),
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
