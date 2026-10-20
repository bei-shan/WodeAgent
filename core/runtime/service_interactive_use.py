"""Own lifecycle of a single Agent's optional interactive tool adapters."""
from __future__ import annotations


class InteractiveUseService:
    def __init__(self) -> None:
        self._tools = ()

    def start(self, agent) -> None:
        # Bootstrap has already registered only explicitly enabled local tools.
        # Do not create or discover arbitrary tools at service startup.
        self._tools = tuple(
            tool for name in ("BrowserUse", "ComputerUse")
            if (tool := agent.tool_registry.get_tool(name)) is not None
        )

    def close(self, agent) -> None:
        for tool in reversed(self._tools):
            close = getattr(tool, "close", None)
            if callable(close):
                close()
        self._tools = ()
