"""Automatic memory capture, modelled on DeerFlow's DeerMem extraction.

After a turn completes, a cheap model is asked for at most N durable facts from
the exchange. Only reusable project knowledge is kept: one-off task details are
explicitly rejected by the prompt, results are JSON-parsed into strings, and
anything already present in the store is skipped.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .service import LocalMemoryService

MAX_PROMPT_MESSAGES = 8
MAX_PROMPT_CHARS = 800
MIN_FACT_CHARS = 12
MAX_FACT_CHARS = 400

EXTRACTION_PROMPT = (
    "Extract durable project knowledge from this exchange.\n"
    "Keep only facts a future session would need: conventions, commands that "
    "work, architectural decisions, environment quirks, user preferences.\n"
    "Do NOT keep: one-off task details, file listings, transient errors, "
    "secrets, tokens, credentials, or anything already obvious from the code.\n"
    f"Return a JSON array of at most {{max_items}} short strings (one sentence "
    f"each, under {MAX_FACT_CHARS} chars). Return [] when nothing qualifies.\n"
    "Reply with the JSON array only.\n\n"
    "<exchange>\n{transcript}\n</exchange>"
)


def build_prompt(messages: Iterable[dict[str, Any]], max_items: int) -> str:
    lines: list[str] = []
    for message in list(messages)[-MAX_PROMPT_MESSAGES:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if len(content) > MAX_PROMPT_CHARS:
            content = content[:MAX_PROMPT_CHARS] + "…"
        lines.append(f"{role}: {content}")
    transcript = "\n".join(lines) if lines else "(empty exchange)"
    return EXTRACTION_PROMPT.format(max_items=max_items, transcript=transcript)


def parse_facts(raw: Any, max_items: int) -> list[str]:
    """Accept a JSON array (or a ```json fence); reject everything else."""
    if not isinstance(raw, str):
        raw = getattr(raw, "content", None)
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text or text.upper() in {"NONE", "NULL"}:
        return []
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    facts: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            continue
        fact = " ".join(item.split()).strip()
        if MIN_FACT_CHARS <= len(fact) <= MAX_FACT_CHARS:
            facts.append(fact)
        if len(facts) >= max_items:
            break
    return facts


def _already_known(service: LocalMemoryService, fact: str) -> bool:
    normalized = fact.lower()
    for record in service.search(fact[:60], limit=5):
        existing = record.content.lower()
        if existing == normalized or normalized in existing or existing in normalized:
            return True
    return False


def extract_facts(llm: Any, messages: list[dict[str, Any]], max_items: int, *, timeout_s: float = 20.0):
    """Ask the model for facts; ``None`` on any failure."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    prompt = build_prompt(messages, max_items)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-capture")
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
    return parse_facts(raw, max_items)


def capture(
    llm: Any,
    service: LocalMemoryService,
    messages: list[dict[str, Any]],
    *,
    max_items: int | None = None,
    timeout_s: float = 20.0,
) -> list[str]:
    """Extract and store new facts; returns the stored strings."""
    if max_items is None:
        max_items = service.settings.max_per_turn
    if not service.enabled or max_items <= 0:
        return []
    facts = extract_facts(llm, messages, max_items, timeout_s=timeout_s)
    if not facts:
        return []
    stored: list[str] = []
    for fact in facts:
        try:
            if _already_known(service, fact):
                continue
            service.add(fact, metadata={"source": "auto-capture"})
            stored.append(fact)
        except Exception:
            continue
    return stored
