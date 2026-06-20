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
    """Displays LLM output as it streams in, using Rich Live.

    Usage::

        stream = StreamingResponse(console)
        stream.start("Thinking...")
        for chunk in llm.think(messages):
            stream.append(chunk)
        result = stream.finish()

    Thread-safe — chunks can arrive from any thread.
    """

    def __init__(self, console: Console):
        self._console = console
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._live: Optional[Live] = None
        self._started_at: float = 0.0
        self._title: str = ""

    def start(self, title: str = "") -> None:
        """Begin streaming display."""
        self._title = title
        self._buffer = []
        self._started_at = time.monotonic()
        renderable = self._render()
        self._live = Live(
            renderable,
            console=self._console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

    def append(self, chunk: str) -> None:
        """Add a chunk of text.  Call from any thread."""
        with self._lock:
            self._buffer.append(str(chunk))

    def append_text(self, text: str, style: str = "") -> None:
        """Add styled text.  For non-streaming use."""
        self._console.print(text, style=style)

    def finish(self) -> str:
        """Stop streaming, return the full accumulated text."""
        if self._live:
            self._live.stop()
            self._live = None
        with self._lock:
            result = "".join(self._buffer)
        elapsed = time.monotonic() - self._started_at
        # Print final rendered version
        if self._title:
            self._console.print(
                Panel(Markdown(result), title=self._title, border_style="blue", title_align="left")
            )
        else:
            self._console.print(Markdown(result))
        self._console.print(Text(f"  ({elapsed:.1f}s)", style="dim"))
        return result

    def _render(self):
        """Build the current renderable."""
        with self._lock:
            text = "".join(self._buffer)
        if not text:
            return Text("…", style="dim")
        elapsed = time.monotonic() - self._started_at
        content = Markdown(text + "█")  # cursor
        if self._title:
            return Panel(content, title=self._title, border_style="blue", title_align="left")
        return Panel(content, border_style="blue")
