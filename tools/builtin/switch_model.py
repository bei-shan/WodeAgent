"""SwitchModel tool — change the active LLM model mid-conversation.

Aligns with Claude Code's /model feature.  Both the user (/model command)
and the LLM (SwitchModel tool) can trigger a model switch.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from prompts.tools_prompts.switch_model_prompt import switch_model_prompt
from ..base import ErrorCode, Tool, ToolParameter


class SwitchModelTool(Tool):
    """Switch the active LLM model during a conversation."""
    usage_notes = 'SwitchModel: Switch the active LLM model mid-conversation. Use model name from available profiles.'

    def __init__(
        self,
        name: str = "SwitchModel",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        code_agent: Optional[Any] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        super().__init__(
            name=name,
            description=switch_model_prompt,
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        if code_agent is None:
            raise ValueError("code_agent is required")
        self._code_agent = code_agent

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="model",
                type="string",
                description="Model identifier to switch to (e.g. 'gpt-4o', 'deepseek-v3', 'claude-sonnet-4-6').",
                required=True,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)
        model = parameters.get("model")
        if not isinstance(model, str) or not model.strip():
            # No model specified — list available profiles + current model.
            from core.model_profiles import load_model_profiles, list_model_profiles
            profiles = load_model_profiles()
            profile_list = list_model_profiles(profiles)
            current = self._code_agent.llm.model
            return self.create_success_response(
                data={
                    "current_model": current,
                    "available_profiles": profile_list,
                },
                text=(
                    f"Current model: {current}\n"
                    f"Available profiles: {', '.join(p['name'] for p in profile_list) or '(none configured)'}"
                ),
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )

        try:
            previous = self._code_agent.llm.model
            self._code_agent.switch_model(model=model.strip())
            return self.create_success_response(
                data={
                    "previous_model": previous,
                    "current_model": self._code_agent.llm.model,
                },
                text=f"Switched model from '{previous}' to '{self._code_agent.llm.model}'.",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
        except Exception as exc:  # pragma: no cover
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"Model switch failed: {exc}",
                params_input=params_input,
                time_ms=int((time.monotonic() - start_time) * 1000),
            )
