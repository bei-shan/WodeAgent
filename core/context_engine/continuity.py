"""Bounded task continuity hints for lossy conversation compaction.

These hints are *source data* from user messages, not system instructions.
Never synthesize goals or infer that an old task remains active.
"""
from __future__ import annotations

import html
from typing import Sequence

from core.message import Message


def _trim(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 18:
        return text[:limit]
    marker = "\n[...omitted...]\n"
    remaining = limit - len(marker)
    head = remaining // 2
    return text[:head] + marker + text[-(remaining - head):]


def build_continuity_anchor(messages: Sequence[Message], max_chars: int = 1200) -> str:
    """Capture original user objective and latest archived user constraints.

    Bounded and escaped so a user message cannot forge closing markup tags.
    The retained recent rounds remain the authoritative active conversation.
    """
    if max_chars < 100:
        return ""
    user_text = [str(m.content).strip() for m in messages if m.role == "user" and m.content]
    if not user_text:
        return ""
    candidates = [("Original user goal", user_text[0])]
    for text in user_text[-2:]:
        if text != candidates[0][1] and text not in [v for _, v in candidates]:
            candidates.append(("Later user decision", text))
    per_note = max(48, (max_chars - 90) // max(1, len(candidates)))
    fragments = [f"- {label}: {html.escape(_trim(value, per_note), quote=False)}" for label, value in candidates]
    result = (
        "Archived user-provided task context (reference data only; "
        "newer requests take precedence):\n" + "\n".join(fragments)
    )
    return _trim(result, max_chars)


def bound_summary_input(text: str, max_chars: int) -> str:
    """Keep early goal plus latest progress when source history is oversized."""
    if max_chars < 200:
        raise ValueError("summary input budget must be >= 200 characters")
    if len(text) <= max_chars:
        return text
    marker = "\n\n[Earlier details omitted from the summarization input]\n\n"
    remaining = max_chars - len(marker)
    head = max(80, remaining // 3)
    return text[:head] + marker + text[-(remaining - head):]
