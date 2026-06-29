"""Streaming response display — Rich Live-based real-time LLM output."""

from __future__ import annotations

import threading
import time
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


class StreamingResponse:
    """Displays LLM content and reasoning as they stream, using Rich Live.

    Provides separate channels for content and reasoning text, token-rate
    counting, and dynamic title updates (e.g. step labelling).  Thread-safe.

    Usage::

        stream = StreamingResponse(console)
        stream.start("Agent")
        # As deltas arrive:
        stream.append("He")          # content token
        stream.append_reasoning("…") # reasoning token (shown in collapsible panel)
        # After all deltas consumed:
        final = stream.finish()
        # final = (content_text, reasoning_text, elapsed_s, total_tokens)
    """

    def __init__(self, console: Console):
        self._console = console
        self._buffer: list[str] = []
        self._reasoning_buffer: list[str] = []
        self._lock = threading.Lock()
        self._live: Optional[Live] = None
        self._started_at: float = 0.0
        self._title: str = ""

    @property
    def elapsed(self) -> float:
        if self._started_at == 0.0:
            return 0.0
        return time.monotonic() - self._started_at

    def start(self, title: str = "") -> None:
        """Begin streaming display."""
        self._title = title
        self._buffer = []
        self._reasoning_buffer = []
        self._started_at = time.monotonic()
        renderable = self._render()
        self._live = Live(
            renderable,
            console=self._console,
            refresh_per_second=10,
            transient=True,
        )
        self._live.start()

    def append(self, chunk: str) -> None:
        """Add a content token.  Call from any thread."""
        if not chunk:
            return
        with self._lock:
            self._buffer.append(str(chunk))
        self._refresh()

    def append_reasoning(self, chunk: str) -> None:
        """Add a reasoning token (rendered in a magenta-bordered panel)."""
        if not chunk:
            return
        with self._lock:
            self._reasoning_buffer.append(str(chunk))
        self._refresh()

    def update_title(self, title: str) -> None:
        """Change the panel title mid-stream (e.g. step label update)."""
        self._title = title
        self._refresh()

    def append_text(self, text: str, style: str = "") -> None:
        """Add styled text to the console — not for streaming, for one-off."""
        self._console.print(text, style=style)

    def finish(self):
        """Stop streaming and return ``(content, reasoning, elapsed, total_tokens)``.

        Clears the Live area.  Does **not** print — the caller controls
        display ordering.
        """
        if self._live:
            self._live.stop()
            self._live = None
        with self._lock:
            content = "".join(self._buffer)
            reasoning = "".join(self._reasoning_buffer)
        total = len(self._buffer) + len(self._reasoning_buffer)
        return content, reasoning, self.elapsed, total

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._render(), refresh=True)

    def _render(self):
        with self._lock:
            content_text = "".join(self._buffer)
            reasoning_text = "".join(self._reasoning_buffer)

        has_content = bool(content_text)
        has_reasoning = bool(reasoning_text)

        if not has_content and not has_reasoning:
            return Text("…", style="dim")

        # Title decoration: append token count and rate when measurable.
        title = self._title
        total_tokens = len(self._buffer) + len(self._reasoning_buffer)
        if total_tokens and self._started_at > 0.0 and self.elapsed > 0.0:
            rate = total_tokens / self.elapsed
            suffix = f"  {total_tokens} tok @ {rate:.0f} tok/s"
        else:
            suffix = ""

        # Content panel (blue border).
        if has_content:
            content_md = Markdown(content_text + "█")  # cursor for streaming
        else:
            content_md = Text("…", style="dim")
        content_panel = Panel(
            content_md,
            title=f"{title} Response{suffix}" if title else f"Response{suffix}",
            border_style="blue",
            title_align="left",
        )

        # Reasoning panel (magenta border, collapsed when empty).
        if has_reasoning:
            reasoning_md = Text(reasoning_text, style="dim")
            reasoning_panel = Panel(
                reasoning_md,
                title="🧠 Thinking",
                border_style="magenta",
                title_align="left",
            )
            # Stack: reasoning above, response below.
            from rich.console import Group
            return Group(reasoning_panel, content_panel)

        return content_panel
