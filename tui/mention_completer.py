"""@-mention autocompletion for prompt_toolkit.

Supports @agent, @model, @file completions in the input prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from prompt_toolkit.completion import Completer, Completion, CompleteEvent
from prompt_toolkit.document import Document


class MentionCompleter(Completer):
    """prompt_toolkit Completer that activates on @ for agent/model/file mentions.

    Parameters
    ----------
    get_agents:
        Callable returning a list of agent names.
    get_models:
        Callable returning a list of model names/profiles.
    project_root:
        Root directory for @file path completion.
    """

    def __init__(
        self,
        get_agents: Optional[Callable[[], list[str]]] = None,
        get_models: Optional[Callable[[], list[str]]] = None,
        project_root: Optional[Path] = None,
    ):
        self._get_agents = get_agents or (lambda: [])
        self._get_models = get_models or (lambda: [])
        self._project_root = Path(project_root) if project_root else None

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        """Yield Completion objects for @-mentions."""
        text_before = document.text_before_cursor
        # Find the last @ position
        at_pos = text_before.rfind("@")
        if at_pos == -1:
            return

        # Text after @ (what user has typed so far)
        prefix = text_before[at_pos + 1:].lower()

        # Determine category
        if prefix.startswith("agent:"):
            search = prefix[6:]
            for name in self._get_agents():
                if search in name.lower():
                    yield Completion(
                        name,
                        start_position=-len(prefix),
                        display=f"@{name}",
                        display_meta="agent",
                    )
        elif prefix.startswith("model:"):
            search = prefix[6:]
            for name in self._get_models():
                if search in name.lower():
                    yield Completion(
                        name,
                        start_position=-len(prefix),
                        display=f"@{name}",
                        display_meta="model",
                    )
        elif prefix.startswith("file:"):
            search = prefix[5:]
            if self._project_root:
                for p in sorted(self._project_root.rglob("*")):
                    if p.name.startswith("."):
                        continue
                    rel = str(p.relative_to(self._project_root))
                    if search in rel.lower():
                        yield Completion(
                            rel,
                            start_position=-len(prefix),
                            display=f"@{rel}",
                            display_meta="file",
                        )
        else:
            # No prefix — show all categories
            for name in self._get_agents():
                if prefix in name.lower():
                    yield Completion(name, start_position=-len(prefix), display=f"@{name}", display_meta="agent")
            for name in self._get_models():
                if prefix in name.lower():
                    yield Completion(name, start_position=-len(prefix), display=f"@{name}", display_meta="model")
