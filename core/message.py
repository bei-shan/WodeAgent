"""消息系统 — 支持会话树的消息模型"""

from typing import Optional, Dict, Any, Literal
from datetime import datetime
from pydantic import BaseModel
import uuid

MessageRole = Literal["user", "assistant", "summary", "tool"]


def generate_short_id() -> str:
    """生成短 ID（uuid4 前 8 位），用于消息和树节点标识。"""
    return uuid.uuid4().hex[:8]


class Message(BaseModel):
    """消息类 — 支持会话树结构。

    每个消息有唯一的 message_id 和可选的 parent_id。
    parent_id 为 None 表示根消息，非 None 表示父消息 ID。
    沿 parent_id 链回溯即可构建当前分支的完整上下文。
    """

    content: str
    role: MessageRole
    timestamp: datetime = None
    metadata: Optional[Dict[str, Any]] = None
    message_id: str = ""        # 短 ID，唯一标识
    parent_id: Optional[str] = None  # 父消息 ID (None = 根)

    def __init__(self, content: str, role: MessageRole, **kwargs):
        mid = kwargs.pop("message_id", "") or generate_short_id()
        pid = kwargs.pop("parent_id", None)
        super().__init__(
            content=content,
            role=role,
            timestamp=kwargs.pop("timestamp", datetime.now()),
            metadata=kwargs.pop("metadata", {}),
            message_id=mid,
            parent_id=pid,
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式（OpenAI API 格式）。"""
        return {
            "role": self.role,
            "content": self.content,
        }

    def to_entry(self) -> Dict[str, Any]:
        """转换为会话树条目（用于 JSONL 存储）。"""
        entry: Dict[str, Any] = {
            "type": "message",
            "id": self.message_id,
            "parentId": self.parent_id,
            "role": self.role,
            "content": self.content,
            "timestamp": (self.timestamp.isoformat()
                          if self.timestamp else datetime.now().isoformat()),
        }
        if self.metadata:
            entry["metadata"] = self.metadata
        return entry

    def __str__(self) -> str:
        pid = f"←{self.parent_id}" if self.parent_id else "←root"
        return f"[{self.role}] {pid} {self.content[:80]}"
