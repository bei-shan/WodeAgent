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
    pinned: bool = False


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


# ── Skills ────────────────────────────────────────────────────────────

class SkillInfo(BaseModel):
    name: str
    description: str
    base_dir: str = ""


class SkillCreate(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    description: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1, description="SKILL.md body (markdown)")


class SkillUpdate(BaseModel):
    description: str | None = None
    content: str | None = None


# ── MCP server config ─────────────────────────────────────────────────

class McpServerConfig(BaseModel):
    name: str
    command: str = ""
    args: list[str] = []


class McpServerCreate(BaseModel):
    name: str = Field(..., min_length=1)
    command: str = Field(..., min_length=1)
    args: list[str] = []


class McpServerUpdate(BaseModel):
    command: str | None = None
    args: list[str] | None = None


# ── Session config ────────────────────────────────────────────────────

class SessionConfig(BaseModel):
    model: str = ""
    provider: str = ""
    enable_agent_teams: bool = False
    plan_mode: bool = False
    thinking_level: str = "medium"  # low | medium | high


class SessionConfigUpdate(BaseModel):
    enable_agent_teams: bool | None = None
    plan_mode: bool | None = None


# ── Teams ─────────────────────────────────────────────────────────────

class TeamCreate(BaseModel):
    team_name: str = Field(..., min_length=1)


class TeamInfo(BaseModel):
    name: str
    queued: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    active: int = 0
    idle: int = 0
    approvals_pending: int = 0
    blocked: int = 0
