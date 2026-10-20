"""Transient, session-scoped attachment parts for model invocation.

Persist filename/path references in history, never base64/media bytes. Only
explicitly supplied image bytes are passed to multimodal provider once, and
only files opened by WorkspaceService's descriptor-anchored path API qualify.
"""
from __future__ import annotations

import base64
import mimetypes
from typing import Any

MAX_ATTACHMENTS = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 96 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024

_TEXT_EXT = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json",
    ".csv", ".yaml", ".yml", ".html", ".css", ".xml", ".toml", ".sh",
}


def detect_media(name: str, payload: bytes) -> str | None:
    """Magic signature beats untrusted multipart Content-Type and extension."""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def make_attachment_part(name: str, payload: bytes) -> dict[str, Any]:
    """The name/path stays session relative, and all untrusted text is bounded."""
    if not isinstance(name, str) or not name or len(name) > 240:
        raise ValueError("Invalid attachment name")
    image_type = detect_media(name, payload)
    if image_type:
        if len(payload) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds 4 MiB model-input cap")
        return {
            "path": name, "kind": "image", "mime_type": image_type,
            "content": {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_type};base64,"
                    + base64.b64encode(payload).decode("ascii"),
                    "detail": "auto",
                },
            },
        }
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix in _TEXT_EXT:
        if len(payload) > MAX_TEXT_BYTES:
            raise ValueError("Text attachment exceeds 96 KiB model-input cap")
        if b"\x00" in payload:
            raise ValueError("Binary content is not a text attachment")
        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Text attachment is not valid UTF-8") from exc
        return {
            "path": name, "kind": "text", "mime_type": "text/plain",
            "content": {"type": "text", "text": f"[Attached file {name}]\n{decoded}"},
        }
    # PDF/Office/unknown binary is deliberately path-only until a dedicated
    # document parsing service extracts text or virtual sandbox file tools
    # can access it. Do not pretend an image model can see arbitrary binaries.
    return {"path": name, "kind": "file", "mime_type": "application/octet-stream", "content": None}


def apply_transient_attachments(
    messages: list[dict], parts: list[dict], *, first_step: bool,
) -> list[dict]:
    """Change only the last user input, on step 1, without mutating history."""
    if not first_step or not parts:
        return messages
    copied = [dict(message) for message in messages]
    last = next((i for i in reversed(range(len(copied)))
                 if copied[i].get("role") == "user"), None)
    if last is None:
        return copied
    raw = copied[last].get("content") or ""
    if not isinstance(raw, str):
        return copied
    blocks: list[dict] = [{"type": "text", "text": raw}]
    for part in parts[:MAX_ATTACHMENTS]:
        block = part.get("content")
        if isinstance(block, dict) and block.get("type") in {"text", "image_url"}:
            blocks.append(block)
    copied[last]["content"] = blocks
    return copied
