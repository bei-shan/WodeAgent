"""One-shot LLM session titles.

Behaviour mirrors DeepSeek Harness' ``session-title-llm``: a single bounded
helper request, a strict output contract, and no replacement of a usable title
by an empty or late result. Generation is advisory - any failure leaves the
existing placeholder in place.
"""
from __future__ import annotations

import re
from typing import Any, Optional

MAX_TITLE_CHARS = 40
PROMPT_MESSAGE_LIMIT = 12
PROMPT_CHARS_PER_MESSAGE = 600

PLACEHOLDER_PREFIXES = ("new session", "新会话", "untitled", "新任务")
_PLACEHOLDER_ID = re.compile(r"[0-9a-f]{8}\Z")
_TITLE_NOISE = re.compile(r"[\r\n\t]+")


def is_placeholder_title(title: Optional[str]) -> bool:
    """True when *title* still looks machine-generated and worth replacing."""
    text = (title or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if any(lowered.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES):
        return True
    return bool(_PLACEHOLDER_ID.fullmatch(text))


def _truncate(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def build_title_prompt(messages: list[dict[str, Any]]) -> str:
    """Render the first exchange into a compact, bounded title request."""
    lines: list[str] = []
    for message in messages[:PROMPT_MESSAGE_LIMIT]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        lines.append(f"{role}: {_truncate(content, PROMPT_CHARS_PER_MESSAGE)}")
    transcript = "\n".join(lines) if lines else "(empty conversation)"
    return (
        "Write a title for this coding-agent session.\n"
        "Rules:\n"
        "- At most 24 Chinese characters or 8 English words.\n"
        "- Concrete and specific: name the task, not the mood.\n"
        "- No quotes, no Markdown, no trailing period, single line.\n"
        "- Reply with the title only.\n\n"
        f"<conversation>\n{transcript}\n</conversation>"
    )


def clean_title(raw: Any, max_chars: int = MAX_TITLE_CHARS) -> Optional[str]:
    """Normalize a model reply into a usable title, or ``None``."""
    if not isinstance(raw, str):
        raw = getattr(raw, "content", None)
    if not isinstance(raw, str):
        return None
    text = _TITLE_NOISE.sub(" ", raw).strip()
    text = re.sub(r"^[#*\-\s]+", "", text)
    # Models add decoration around or inside the quotes ("Title."), so peel
    # quotes and trailing punctuation in a couple of passes.
    for _ in range(3):
        before = text
        text = text.strip().strip("\"'`“”‘’《》").strip()
        text = text.rstrip("。.!！?？:：;；*#").strip()
        if text == before:
            break
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    # A title that merely echoes the instruction is not a title.
    if text.lower().startswith(("title:", "标题：", "标题:")):
        text = re.split(r"[:：]", text, maxsplit=1)[-1].strip()
    return text or None


def generate_title(
    llm: Any,
    messages: list[dict[str, Any]],
    *,
    max_chars: int = MAX_TITLE_CHARS,
    timeout_s: float = 20.0,
) -> Optional[str]:
    """Ask *llm* for a title; return ``None`` on any failure."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    prompt = build_title_prompt(messages)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-title")
    try:
        future = executor.submit(llm.invoke, [{"role": "user", "content": prompt}])
        raw = future.result(timeout=timeout_s)
    except FuturesTimeout:
        future.cancel() if "future" in locals() else None
        return None
    except Exception:
        return None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return clean_title(raw, max_chars)
