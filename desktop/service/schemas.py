"""Request / response schemas for the MyCodeAgent Web Service."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Sessions ─────────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    title: str = ""


class SessionInfo(BaseModel):
    id: str
    title: str = ""
    busy: bool = False
    message_count: int = 0


# ── Messages ─────────────────────────────────────────────────────────

class MessageSend(BaseModel):
    content: str = Field(..., min_length=1, description="User message text")


# ── Permissions ──────────────────────────────────────────────────────

class PermissionResolve(BaseModel):
    decision: str = Field(..., pattern="^(granted|denied)$")


# ── AskUser ──────────────────────────────────────────────────────────

class AskUserAnswer(BaseModel):
    answer: str


# ── Files ────────────────────────────────────────────────────────────

class FileEntry(BaseModel):
    name: str
    path: str
    type: str  # "file" | "directory"


class FileTreeResponse(BaseModel):
    path: str
    entries: list[FileEntry]
    truncated: bool = False


class FileContentResponse(BaseModel):
    path: str
    content: str
    truncated: bool = False


# ── Models ───────────────────────────────────────────────────────────

class ModelInfo(BaseModel):
    name: str
    model: str
    provider: str
    base_url: str = ""


# ── Tools ────────────────────────────────────────────────────────────

class ToolParam(BaseModel):
    name: str
    type: str
    description: str
    required: bool = True


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: list[ToolParam] = []


# ── MCP ──────────────────────────────────────────────────────────────

class McpServerStatus(BaseModel):
    name: str
    connected: bool
    tool_count: int = 0


class McpStatusResponse(BaseModel):
    servers: list[McpServerStatus]
    pending: list[str] = []
    connect_mode: str = "manual"
