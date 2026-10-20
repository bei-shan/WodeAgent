"""Conservative preflight estimate for the full model request, not just chat history.

Do not call this a provider-exact token count. Tools, role framing and
multimodal payloads vary across models. A measured provider usage is preferred.
"""
from __future__ import annotations

import json
from typing import Any


def estimate_request_tokens(messages: list[dict[str, Any]], tools_schema: Any) -> int:
    """Approximate the bytes of the actual outgoing messages and tool schemas.

    UTF-8 bytes / 3 is intentionally cautious for English and CJK-heavy
    coding tasks. Framing overhead prevents undercounting many small messages.
    Native image tokens can differ substantially; a multimodal provider
    tokenizer remains a future requirement.
    """
    tools = tools_schema or []
    serialized = json.dumps(
        {"messages": messages, "tools": tools},
        ensure_ascii=False, separators=(",", ":"), default=str,
    )
    count = (len(serialized.encode("utf-8")) + 2) // 3
    tool_count = len(tools) if isinstance(tools, list) else 1
    return count + 8 * len(messages) + 16 * tool_count
