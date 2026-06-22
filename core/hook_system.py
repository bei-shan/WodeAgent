"""Hook System — lifecycle hooks for workflow automation.

Hooks are user-defined scripts that run at key lifecycle points of the Agent.
Configured via ``.mycode/hooks.json`` in the project root.

Supported events:
- ``SessionStart`` — after agent initialisation
- ``PreToolUse`` — before a tool is executed (can block / modify)
- ``PostToolUse`` — after a tool completes (can inject messages)
- ``SessionEnd`` — before agent shutdown

Usage::

    manager = HookManager(project_root=".")
    manager.run_session_start()
    pre = manager.run_pre_tool_use("Bash", {"command": "ls"})
    if pre.blocked:
        return error_response(pre.reason)
    # ... execute tool ...
    post = manager.run_post_tool_use("Bash", {"command": "ls"}, result)
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CONFIG_PATH = ".mycode/hooks.json"
_DEFAULT_TIMEOUT = 30  # seconds
_SESSION_ID = str(uuid.uuid4())[:8]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Hook:
    """A single hook definition."""

    type: str  # "command"
    command: str
    timeout: int = _DEFAULT_TIMEOUT


@dataclass
class HookMatcher:
    """A matcher with its associated hooks."""

    matcher: str  # "*" | "Bash" | "Team*"
    hooks: list[Hook] = field(default_factory=list)


@dataclass
class HookConfig:
    """Parsed hook configuration."""

    session_start: list[HookMatcher] = field(default_factory=list)
    pre_tool_use: list[HookMatcher] = field(default_factory=list)
    post_tool_use: list[HookMatcher] = field(default_factory=list)
    session_end: list[HookMatcher] = field(default_factory=list)


@dataclass
class HookResult:
    """Aggregated result from running one or more hooks."""

    blocked: bool = False
    reason: str = ""
    system_messages: list[str] = field(default_factory=list)
    additional_context: list[str] = field(default_factory=list)
    updated_input: Optional[dict[str, Any]] = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HookManager
# ---------------------------------------------------------------------------


class HookManager:
    """Manages hook configuration loading and execution.

    Parameters
    ----------
    project_root:
        Project root directory.  Hooks are loaded from
        ``<project_root>/.mycode/hooks.json``.
    """

    def __init__(self, project_root: str):
        self._project_root = Path(project_root)
        self._config: HookConfig = HookConfig()
        self._env_file: Optional[Path] = None
        self._session_id = _SESSION_ID
        self._load_config()

    # ------------------------------------------------------------------
    # Public API — event runners
    # ------------------------------------------------------------------

    def run_session_start(self) -> HookResult:
        """Run all SessionStart hooks.

        Returns aggregated :class:`HookResult` with ``additional_context``
        and ``system_messages``.
        """
        if not self._config.session_start:
            return HookResult()

        input_data = {
            "hook_event_name": "SessionStart",
            "session_id": self._session_id,
            "project_root": str(self._project_root),
            "cwd": str(self._project_root),
        }

        results = self._run_hooks(
            self._config.session_start,
            input_data,
            tool_name="",
            tool_input={},
        )

        # Apply env file if any hook wrote to it.
        if self._env_file and self._env_file.exists():
            self._apply_env_file()

        return results

    def run_pre_tool_use(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> HookResult:
        """Run PreToolUse hooks matching *tool_name*.

        Returns a :class:`HookResult`.  If ``blocked`` is ``True`` the
        caller should abort the tool call and return an error.
        """
        matchers = self._match(self._config.pre_tool_use, tool_name)
        if not matchers:
            return HookResult()

        input_data = {
            "hook_event_name": "PreToolUse",
            "session_id": self._session_id,
            "project_root": str(self._project_root),
            "cwd": str(self._project_root),
            "tool_name": tool_name,
            "tool_input": tool_input,
        }

        return self._run_hooks(matchers, input_data, tool_name, tool_input)

    def run_post_tool_use(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: str,
    ) -> HookResult:
        """Run PostToolUse hooks matching *tool_name*.

        PostToolUse hooks cannot block — they can only inject messages.
        """
        matchers = self._match(self._config.post_tool_use, tool_name)
        if not matchers:
            return HookResult()

        input_data = {
            "hook_event_name": "PostToolUse",
            "session_id": self._session_id,
            "project_root": str(self._project_root),
            "cwd": str(self._project_root),
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_result": tool_result[:2000],  # truncate for hook input
        }

        result = self._run_hooks(matchers, input_data, tool_name, tool_input)
        # PostToolUse cannot block.
        result.blocked = False
        return result

    def run_session_end(self) -> HookResult:
        """Run all SessionEnd hooks.

        SessionEnd hooks cannot block agent shutdown.
        """
        if not self._config.session_end:
            return HookResult()

        input_data = {
            "hook_event_name": "SessionEnd",
            "session_id": self._session_id,
            "project_root": str(self._project_root),
            "cwd": str(self._project_root),
            "reason": "exit",
        }

        result = self._run_hooks(
            self._config.session_end,
            input_data,
            tool_name="",
            tool_input={},
        )
        # SessionEnd cannot block.
        result.blocked = False
        return result

    # ------------------------------------------------------------------
    # Public API — configuration
    # ------------------------------------------------------------------

    @property
    def has_any_hooks(self) -> bool:
        """Return ``True`` if any hooks are configured."""
        return bool(
            self._config.session_start
            or self._config.pre_tool_use
            or self._config.post_tool_use
            or self._config.session_end
        )

    def reload(self) -> None:
        """Re-read hook configuration from disk."""
        self._load_config()

    # ------------------------------------------------------------------
    # Internal — configuration loading
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """Parse ``.mycode/hooks.json`` into a :class:`HookConfig`."""
        config_path = self._project_root / _CONFIG_PATH
        if not config_path.is_file():
            self._config = HookConfig()
            return

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to parse %s: %s", config_path, exc)
            self._config = HookConfig()
            return

        hooks_section = raw.get("hooks", {})
        if not isinstance(hooks_section, dict):
            self._config = HookConfig()
            return

        self._config = HookConfig(
            session_start=self._parse_matchers(hooks_section.get("SessionStart", [])),
            pre_tool_use=self._parse_matchers(hooks_section.get("PreToolUse", [])),
            post_tool_use=self._parse_matchers(hooks_section.get("PostToolUse", [])),
            session_end=self._parse_matchers(hooks_section.get("SessionEnd", [])),
        )
        logger.info(
            "Loaded hooks: SessionStart=%d PreToolUse=%d PostToolUse=%d SessionEnd=%d",
            len(self._config.session_start),
            len(self._config.pre_tool_use),
            len(self._config.post_tool_use),
            len(self._config.session_end),
        )

    @staticmethod
    def _parse_matchers(raw_matchers: list[dict[str, Any]]) -> list[HookMatcher]:
        """Parse a list of raw matcher dicts into :class:`HookMatcher` objects."""
        result: list[HookMatcher] = []
        for entry in raw_matchers:
            if not isinstance(entry, dict):
                continue
            matcher = str(entry.get("matcher", "*"))
            hooks_data = entry.get("hooks", [])
            if not isinstance(hooks_data, list):
                continue
            hooks = []
            for h in hooks_data:
                if not isinstance(h, dict):
                    continue
                hooks.append(Hook(
                    type=str(h.get("type", "command")),
                    command=str(h.get("command", "")),
                    timeout=int(h.get("timeout", _DEFAULT_TIMEOUT)),
                ))
            if hooks:
                result.append(HookMatcher(matcher=matcher, hooks=hooks))
        return result

    # ------------------------------------------------------------------
    # Internal — matching
    # ------------------------------------------------------------------

    @staticmethod
    def _match(matchers: list[HookMatcher], tool_name: str) -> list[HookMatcher]:
        """Return matchers whose pattern matches *tool_name*."""
        if not matchers:
            return []
        matched: list[HookMatcher] = []
        for m in matchers:
            if HookManager._matcher_matches(m.matcher, tool_name):
                matched.append(m)
        return matched

    @staticmethod
    def _matcher_matches(pattern: str, tool_name: str) -> bool:
        """Check whether *pattern* matches *tool_name*.

        Supports:
        - ``"*"`` — matches everything
        - ``"Bash"`` — exact match
        - ``"Team*"`` — simple glob (``*`` wildcard only)
        """
        if pattern == "*":
            return True
        if pattern == tool_name:
            return True
        # Simple glob: convert * to .*
        if "*" in pattern:
            regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
            try:
                return bool(re.match(regex, tool_name))
            except re.error:
                return False
        return False

    # ------------------------------------------------------------------
    # Internal — execution
    # ------------------------------------------------------------------

    def _run_hooks(
        self,
        matchers: list[HookMatcher],
        input_data: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> HookResult:
        """Execute all hooks across *matchers* concurrently.

        Returns an aggregated :class:`HookResult`.  If any hook returns
        ``decision: "block"``, the result is marked as blocked.
        """
        # Flatten all hooks
        all_hooks: list[tuple[Hook, HookMatcher]] = []
        for m in matchers:
            for h in m.hooks:
                all_hooks.append((h, m))

        if not all_hooks:
            return HookResult()

        aggregated = HookResult()

        # Concurrent execution
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(all_hooks), 8)
        ) as executor:
            futures = {
                executor.submit(self._execute_single_hook, h, input_data): (h, m)
                for h, m in all_hooks
            }
            for future in concurrent.futures.as_completed(futures):
                h, m = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:
                    logger.warning("Hook %s raised: %s", h.command, exc)
                    aggregated.warnings.append(f"Hook error: {exc}")
                    continue

                if outcome is None:
                    continue

                # Merge outcomes
                if outcome.get("decision") == "block":
                    aggregated.blocked = True
                    aggregated.reason = str(
                        outcome.get("reason") or f"Blocked by hook: {h.command}"
                    )
                if outcome.get("system_message"):
                    aggregated.system_messages.append(
                        str(outcome["system_message"])
                    )
                if outcome.get("additional_context"):
                    aggregated.additional_context.append(
                        str(outcome["additional_context"])
                    )
                if outcome.get("updated_input") and isinstance(
                    outcome["updated_input"], dict
                ):
                    if aggregated.updated_input is None:
                        aggregated.updated_input = {}
                    aggregated.updated_input.update(outcome["updated_input"])
                if outcome.get("warning"):
                    aggregated.warnings.append(str(outcome["warning"]))

        return aggregated

    def _execute_single_hook(
        self, hook: Hook, input_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Execute a single command hook and parse its output.

        Returns the parsed JSON output dict, or ``None`` on failure.
        """
        if hook.type != "command":
            logger.warning("Unsupported hook type: %s", hook.type)
            return None

        if not hook.command.strip():
            return None

        # Prepare environment
        env = os.environ.copy()
        env["MYCODE_PROJECT_DIR"] = str(self._project_root)
        env["MYCODE_HOOK_EVENT"] = str(input_data.get("hook_event_name", ""))
        env["MYCODE_SESSION_ID"] = self._session_id

        # SessionStart: provide env file for hook to write into.
        if input_data.get("hook_event_name") == "SessionStart":
            import tempfile
            env_dir = Path(tempfile.mkdtemp(prefix="mycode-env-"))
            self._env_file = env_dir / "env"
            env["MYCODE_ENV_FILE"] = str(self._env_file)

        input_json = json.dumps(input_data, ensure_ascii=False)

        try:
            proc = subprocess.run(
                hook.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=hook.timeout,
                input=input_json,
                env=env,
                cwd=str(self._project_root),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Hook timed out after %ds: %s", hook.timeout, hook.command
            )
            return {"warning": f"Hook timed out: {hook.command}"}
        except Exception as exc:
            logger.warning("Hook failed: %s — %s", hook.command, exc)
            return None

        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()

        if proc.returncode == 2:
            # Hard block
            message = stderr or stdout or f"Blocked by hook (exit 2): {hook.command}"
            logger.info("Hook blocked (exit 2): %s", hook.command)
            return {"decision": "block", "reason": message}

        if proc.returncode == 1:
            # Warning
            message = stderr or stdout or f"Hook warning: {hook.command}"
            logger.warning("Hook warning (exit 1): %s — %s", hook.command, message)
            return {"warning": message}

        # Exit 0: parse JSON output
        if not stdout:
            return None

        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            # Non-JSON stdout — treat as system_message
            return {"system_message": stdout}

        if not isinstance(parsed, dict):
            return {"system_message": str(parsed)}

        return parsed

    def _apply_env_file(self) -> None:
        """Read ``MYCODE_ENV_FILE`` and merge exports into ``os.environ``."""
        if not self._env_file or not self._env_file.exists():
            return

        try:
            lines = self._env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Strip "export " prefix if present.
            if stripped.startswith("export "):
                stripped = stripped[7:].strip()
            if "=" in stripped:
                key, _, value = stripped.partition("=")
                key = key.strip()
                # Strip surrounding quotes
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ[key] = value
                    logger.debug("Hook env: %s=%s", key, value)
