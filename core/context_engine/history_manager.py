"""历史记录管理器 — 支持会话树

会话树设计（借鉴 Pi Agent）：
- 每条消息有 message_id 和 parent_id
- cursor_id 指向当前分支末端
- fork(target_id) 将 cursor 移到历史节点 → 后续消息自动形成新分支
- get_current_branch() 沿 parent_id 链构建当前分支的上下文
- compact() 保留 compaction 条目而非删除旧消息
- model_change / thinking_change 记录会话状态变化
"""

import json
import logging
from typing import List, Optional, Callable, Tuple, Dict, Any
from datetime import datetime

from ..message import Message, generate_short_id
from ..config import Config
from .observation_truncator import truncate_observation

logger = logging.getLogger(__name__)

# ── 树条目类型常量 ──
ENTRY_MESSAGE = "message"
ENTRY_COMPACTION = "compaction"
ENTRY_BRANCH_SUMMARY = "branch_summary"
ENTRY_MODEL_CHANGE = "model_change"
ENTRY_THINKING_CHANGE = "thinking_level_change"
ENTRY_LEAF = "leaf"
ENTRY_LABEL = "label"
ENTRY_SESSION_INFO = "session_info"


class HistoryManager:
    """历史记录管理器 — 支持会话树。

    核心新增:
    - _cursor_id: 当前光标位置，新消息的 parent_id 指向这里
    - _id_index: message_id → Message 的 O(1) 索引
    - _entries: 完整树条目列表（含非消息条目）
    - fork() / navigate_to(): 分叉操作
    - get_current_branch(): 沿 parent_id 链构建上下文
    - append_model_change / append_thinking_change: 元数据记录
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        summary_generator: Optional[Callable[[List[Message]], Optional[str]]] = None,
    ):
        self._config = config or Config.from_env()
        self._summary_generator = summary_generator

        # 消息列表（保留向后兼容）
        self._messages: List[Message] = []

        # 会话树索引
        self._id_index: Dict[str, Message] = {}
        self._entries: List[Dict[str, Any]] = []  # 完整条目列表
        self._cursor_id: Optional[str] = None
        self._labels: Dict[str, str] = {}  # entry_id → label

        # 当前会话状态
        self._current_model: Optional[Dict[str, str]] = None
        self._thinking_level: str = "off"

        # Token 追踪
        self._last_usage_tokens: int = 0
        self._total_usage_tokens: int = 0

    # ═════════════════════════════════════════════════════════════════════
    # 公开接口 — 消息写入
    # ═════════════════════════════════════════════════════════════════════

    def append_user(self, content: str, metadata: Optional[dict] = None) -> Message:
        msg = Message(
            content=content,
            role="user",
            metadata=metadata or {},
            parent_id=self._cursor_id,
        )
        self._add_message(msg)
        return msg

    def append_assistant(
        self,
        content: str,
        metadata: Optional[dict] = None,
        reasoning_content: Optional[str] = None,
    ) -> Message:
        msg = Message(
            content=content,
            role="assistant",
            metadata=metadata or {},
            parent_id=self._cursor_id,
        )
        if reasoning_content:
            msg.metadata["reasoning_content"] = reasoning_content
        if self._current_model:
            msg.metadata["model"] = self._current_model.get("modelId", "")
            msg.metadata["provider"] = self._current_model.get("provider", "")
        self._add_message(msg)
        return msg

    def append_tool(
        self,
        tool_name: str,
        raw_result: str,
        metadata: Optional[dict] = None,
        project_root: Optional[str] = None,
    ) -> Message:
        truncated_result = truncate_observation(tool_name, raw_result, project_root)
        msg = Message(
            content=truncated_result,
            role="tool",
            metadata={
                **(metadata or {}),
                "tool_name": tool_name,
            },
            parent_id=self._cursor_id,
        )
        self._add_message(msg)
        return msg

    def append_summary(self, content: str) -> Message:
        msg = Message(
            content=content,
            role="summary",
            metadata={"generated_at": datetime.now().isoformat()},
            parent_id=self._cursor_id,
        )
        self._add_message(msg)
        return msg

    # ═════════════════════════════════════════════════════════════════════
    # 公开接口 — 元数据条目
    # ═════════════════════════════════════════════════════════════════════

    def append_model_change(self, provider: str, model_id: str) -> str:
        """记录模型切换。后续 assistant 消息会自动携带模型信息。"""
        self._current_model = {"provider": provider, "modelId": model_id}
        entry = {
            "type": ENTRY_MODEL_CHANGE,
            "id": generate_short_id(),
            "parentId": self._cursor_id,
            "timestamp": datetime.now().isoformat(),
            "provider": provider,
            "modelId": model_id,
        }
        self._entries.append(entry)
        self._cursor_id = entry["id"]
        return entry["id"]

    def append_thinking_change(self, level: str) -> str:
        """记录思考深度变化。"""
        self._thinking_level = level
        entry = {
            "type": ENTRY_THINKING_CHANGE,
            "id": generate_short_id(),
            "parentId": self._cursor_id,
            "timestamp": datetime.now().isoformat(),
            "thinkingLevel": level,
        }
        self._entries.append(entry)
        self._cursor_id = entry["id"]
        return entry["id"]

    def append_label(self, target_id: str, label: str | None) -> str:
        """给节点打标签（用于 /tree 展示）。"""
        if label:
            self._labels[target_id] = label
        elif target_id in self._labels:
            del self._labels[target_id]
        entry = {
            "type": ENTRY_LABEL,
            "id": generate_short_id(),
            "parentId": self._cursor_id,
            "timestamp": datetime.now().isoformat(),
            "targetId": target_id,
            "label": label,
        }
        self._entries.append(entry)
        self._cursor_id = entry["id"]
        return entry["id"]

    # ═════════════════════════════════════════════════════════════════════
    # 公开接口 — 树操作
    # ═════════════════════════════════════════════════════════════════════

    def fork(self, target_id: str) -> str:
        """从指定消息分叉。cursor 移到 target_id，后续消息形成新分支。

        注意: 不生成 branch_summary（由 navigate_to 负责）。
        """
        if target_id not in self._id_index:
            raise ValueError(f"Target message not found: {target_id}")

        # 记录 leaf 条目
        leaf_entry = {
            "type": ENTRY_LEAF,
            "id": generate_short_id(),
            "parentId": self._cursor_id,
            "timestamp": datetime.now().isoformat(),
            "targetId": target_id,
        }
        self._entries.append(leaf_entry)
        self._cursor_id = target_id
        return target_id

    def navigate_to(self, target_id: str, summarize: bool = False) -> str:
        """移动光标到目标节点。可选生成 branch_summary。

        如果 summarize=True，会收集旧分支上需要总结的消息，
        调用 summary_generator 生成摘要作为 branch_summary 条目。
        """
        old_cursor = self._cursor_id

        # 先 fork
        new_cursor = self.fork(target_id)

        # 生成 branch_summary
        if summarize and old_cursor and self._summary_generator:
            old_branch_msgs = self._collect_branch_messages(old_cursor, target_id)
            if old_branch_msgs:
                try:
                    summary_text = self._summary_generator(old_branch_msgs)
                    if summary_text:
                        bs_entry = {
                            "type": ENTRY_BRANCH_SUMMARY,
                            "id": generate_short_id(),
                            "parentId": target_id,
                            "timestamp": datetime.now().isoformat(),
                            "summary": summary_text,
                            "fromId": old_cursor,
                        }
                        self._entries.append(bs_entry)
                        self._cursor_id = bs_entry["id"]
                except Exception as exc:
                    logger.warning("Failed to generate branch summary: %s", exc)

        return new_cursor

    def get_current_branch(self) -> List[Message]:
        """获取当前分支的消息（从 cursor 沿 parent_id 链到根）。

        用于构建 LLM 上下文 — 只包含当前分支的消息。
        """
        if not self._cursor_id:
            return list(self._messages)

        # 沿 parent_id 链回溯
        chain: List[Message] = []
        current_id = self._cursor_id

        while current_id is not None:
            msg = self._id_index.get(current_id)
            if msg is not None:
                chain.append(msg)
                current_id = msg.parent_id
            else:
                # 可能是非消息条目（leaf/model_change等），查 entries
                entry = self._find_entry(current_id)
                if entry is not None:
                    current_id = entry.get("parentId")
                else:
                    break

        chain.reverse()
        return chain

    def get_tree(self) -> Dict[str, Any]:
        """返回完整树结构（用于 /tree 命令）。"""
        # 构建 id→children 映射
        children: Dict[str, List[str]] = {}
        all_ids: Dict[str, Dict[str, Any]] = {}

        # 从 _messages 收集
        for msg in self._messages:
            all_ids[msg.message_id] = {
                "id": msg.message_id,
                "parentId": msg.parent_id,
                "role": msg.role,
                "content": msg.content[:60],
                "type": "message",
            }
            pid = msg.parent_id or "__root__"
            children.setdefault(pid, []).append(msg.message_id)

        # 从 _entries 收集非消息条目
        for entry in self._entries:
            eid = entry["id"]
            all_ids[eid] = {
                "id": eid,
                "parentId": entry.get("parentId"),
                "type": entry["type"],
                "summary": (entry.get("summary", "")[:60]
                            if entry["type"] == ENTRY_BRANCH_SUMMARY
                            else entry.get("thinkingLevel", "")),
            }
            pid = entry.get("parentId") or "__root__"
            children.setdefault(pid, []).append(eid)

        return {
            "nodes": all_ids,
            "children": children,
            "cursor_id": self._cursor_id,
            "labels": dict(self._labels),
        }

    def get_branches(self) -> List[Dict[str, Any]]:
        """列出所有分支（有多个子节点的节点）。"""
        child_count: Dict[str, int] = {}
        for msg in self._messages:
            pid = msg.parent_id or "__root__"
            child_count[pid] = child_count.get(pid, 0) + 1
        for entry in self._entries:
            if entry["type"] == ENTRY_LEAF:
                continue
            pid = entry.get("parentId") or "__root__"
            child_count[pid] = child_count.get(pid, 0) + 1

        branches = []
        for pid, count in child_count.items():
            if count > 1 and pid != "__root__":
                label = self._labels.get(pid, "")
                msg = self._id_index.get(pid)
                branches.append({
                    "id": pid,
                    "label": label,
                    "preview": str(msg)[:80] if msg else "",
                    "children_count": count,
                })
        return branches

    # ═════════════════════════════════════════════════════════════════════
    # 公开接口 — 会话状态查询
    # ═════════════════════════════════════════════════════════════════════

    def get_cursor_id(self) -> Optional[str]:
        return self._cursor_id

    def get_thinking_level(self) -> str:
        return self._thinking_level

    def get_current_model(self) -> Optional[Dict[str, str]]:
        return self._current_model

    # ═════════════════════════════════════════════════════════════════════
    # 公开接口 — 兼容旧 API
    # ═════════════════════════════════════════════════════════════════════

    def get_messages(self) -> List[Message]:
        """获取所有历史消息（兼容旧接口）。"""
        return self._messages.copy()

    def get_message_count(self) -> int:
        return len(self._messages)

    def clear(self):
        self._messages.clear()
        self._id_index.clear()
        self._entries.clear()
        self._cursor_id = None
        self._labels.clear()
        self._last_usage_tokens = 0
        self._total_usage_tokens = 0

    def update_last_usage(self, total_tokens: int):
        if total_tokens is None:
            return
        self._last_usage_tokens = total_tokens
        self._total_usage_tokens += total_tokens

    def get_total_usage_tokens(self) -> int:
        return self._total_usage_tokens

    def estimate_total_tokens(self, pending_input: str) -> int:
        input_estimate = len(pending_input) // 3
        return self._total_usage_tokens + input_estimate

    def estimate_context_tokens(self, pending_input: str) -> int:
        total_chars = len(pending_input or "")
        for msg in self._messages:
            content = msg.content or ""
            total_chars += len(str(content))
            meta = msg.metadata or {}
            if msg.role == "assistant":
                tool_calls = meta.get("tool_calls")
                if tool_calls:
                    try:
                        total_chars += len(json.dumps(tool_calls, ensure_ascii=False))
                    except Exception:
                        total_chars += len(str(tool_calls))
            elif msg.role == "tool":
                tool_name = meta.get("tool_name")
                if tool_name:
                    total_chars += len(str(tool_name))
        return total_chars // 3

    # ═════════════════════════════════════════════════════════════════════
    # 压缩
    # ═════════════════════════════════════════════════════════════════════

    def should_compress(self, pending_input: str) -> bool:
        if len(self._messages) < 3:
            return False
        usage_estimated = self._last_usage_tokens + len(pending_input or "") // 3
        estimated_total = max(self.estimate_context_tokens(pending_input), usage_estimated)
        threshold = int(self._config.context_window * self._config.compression_threshold)
        return estimated_total >= threshold

    def compact(self, on_event=None, return_info: bool = False):
        """执行历史压缩。树模式下保留 compaction 条目而非删除消息。"""
        def _emit(event: str, payload: dict):
            if on_event:
                try:
                    on_event(event, payload)
                except Exception:
                    pass

        info: dict = {"compressed": False}
        rounds = self._identify_rounds()
        min_rounds = self._config.min_retain_rounds

        if len(rounds) <= min_rounds:
            info.update({"reason": "rounds_not_enough", "rounds_count": len(rounds)})
            _emit("history_compression_plan", info)
            _emit("history_compression_skipped", info)
            return info if return_info else False

        retain_start_round = len(rounds) - min_rounds
        retain_start_idx = rounds[retain_start_round][0]

        info.update({
            "rounds": rounds,
            "rounds_count": len(rounds),
            "min_retain_rounds": min_rounds,
            "retain_start_round": retain_start_round,
            "retain_start_idx": retain_start_idx,
            "messages_before": len(self._messages),
        })
        _emit("history_compression_plan", info)

        messages_to_compress = [
            msg for msg in self._messages[:retain_start_idx]
            if msg.role != "summary"
        ]
        existing_summaries = [
            msg for msg in self._messages[:retain_start_idx]
            if msg.role == "summary"
        ]

        info.update({
            "messages_to_compress": len(messages_to_compress),
            "existing_summaries": len(existing_summaries),
        })
        _emit("history_compression_messages", info)

        if not messages_to_compress:
            info.update({"reason": "no_messages_to_compress"})
            _emit("history_compression_skipped", info)
            return info if return_info else False

        new_summary = None
        if self._summary_generator:
            try:
                new_summary = self._summary_generator(messages_to_compress)
            except Exception:
                new_summary = None

        info.update({
            "summary_generated": new_summary is not None,
            "summary_len": len(new_summary) if isinstance(new_summary, str) else 0,
            "summary_text": new_summary if isinstance(new_summary, str) else "",
        })
        _emit("history_compression_summary", info)

        # 重建消息列表
        new_messages: List[Message] = []
        new_messages.extend(existing_summaries)

        if new_summary is not None:
            summary_msg = Message(
                content=new_summary,
                role="summary",
                metadata={"generated_at": datetime.now().isoformat()},
                parent_id=self._cursor_id,
            )
            new_messages.append(summary_msg)
            # 记录 compaction 条目（树模式）
            first_kept = self._messages[retain_start_idx] if retain_start_idx < len(self._messages) else None
            comp_entry = {
                "type": ENTRY_COMPACTION,
                "id": generate_short_id(),
                "parentId": self._cursor_id,
                "timestamp": datetime.now().isoformat(),
                "summary": new_summary,
                "firstKeptEntryId": first_kept.message_id if first_kept else None,
                "tokensBefore": self._total_usage_tokens,
            }
            self._entries.append(comp_entry)

        new_messages.extend(self._messages[retain_start_idx:])
        self._messages = new_messages

        # 更新 cursor 到压缩后的最后一条消息
        if self._messages:
            self._cursor_id = self._messages[-1].message_id

        info.update({
            "compressed": True,
            "messages_after": len(self._messages),
        })
        _emit("history_compression_rebuilt", {
            "messages_after": len(self._messages),
        })

        try:
            compressed_context = self.to_messages_all()
        except Exception:
            compressed_context = []
        _emit("history_compression_context", {
            "messages": compressed_context,
            "message_count": len(compressed_context),
        })

        return info if return_info else True

    def _identify_rounds(self) -> List[Tuple[int, int]]:
        rounds: List[Tuple[int, int]] = []
        current_start: Optional[int] = None
        for idx, msg in enumerate(self._messages):
            if msg.role == "user":
                if current_start is not None:
                    rounds.append((current_start, idx - 1))
                current_start = idx
            elif msg.role == "summary":
                continue
        if current_start is not None:
            rounds.append((current_start, len(self._messages) - 1))
        return rounds

    # ═════════════════════════════════════════════════════════════════════
    # 序列化
    # ═════════════════════════════════════════════════════════════════════

    def serialize_messages(self) -> List[Dict[str, Any]]:
        """序列化为可持久化结构（含树信息）。"""
        items: List[Dict[str, Any]] = []
        for msg in self._messages:
            items.append({
                "role": msg.role,
                "content": msg.content,
                "metadata": (msg.metadata or {}),
                "message_id": msg.message_id,
                "parent_id": msg.parent_id,
            })
        return items

    def load_messages(self, items: List[Dict[str, Any]]) -> None:
        """从序列化结构恢复消息（兼容 v1 无 ID 的旧快照）。"""
        self._messages = []
        self._id_index = {}
        for item in items or []:
            role = item.get("role")
            if role not in {"user", "assistant", "tool", "summary"}:
                continue
            msg = Message(
                content=item.get("content", ""),
                role=role,
                metadata=item.get("metadata", {}) or {},
                message_id=item.get("message_id", ""),
                parent_id=item.get("parent_id"),
            )
            self._messages.append(msg)
            self._id_index[msg.message_id] = msg

        # 重建 cursor
        if self._messages:
            self._cursor_id = self._messages[-1].message_id

    def serialize_entries(self) -> List[Dict[str, Any]]:
        """导出完整的树条目列表（用于快照持久化）。"""
        entries: List[Dict[str, Any]] = []
        for msg in self._messages:
            entries.append(msg.to_entry())
        entries.extend(self._entries)
        return entries

    def load_entries(self, entries: List[Dict[str, Any]]) -> None:
        """从树条目列表恢复（用于快照恢复）。"""
        self._messages = []
        self._id_index = {}
        self._entries = []
        for entry in entries or []:
            etype = entry.get("type", ENTRY_MESSAGE)
            if etype == ENTRY_MESSAGE:
                msg = Message(
                    content=entry.get("content", ""),
                    role=entry.get("role", "user"),
                    metadata=entry.get("metadata", {}) or {},
                    message_id=entry.get("id", ""),
                    parent_id=entry.get("parentId"),
                )
                self._messages.append(msg)
                self._id_index[msg.message_id] = msg
                self._cursor_id = msg.message_id
            else:
                self._entries.append(entry)
                if etype in (ENTRY_LEAF, ENTRY_MODEL_CHANGE, ENTRY_THINKING_CHANGE,
                             ENTRY_LABEL, ENTRY_BRANCH_SUMMARY):
                    self._cursor_id = entry["id"]
                if etype == ENTRY_MODEL_CHANGE:
                    self._current_model = {
                        "provider": entry.get("provider", ""),
                        "modelId": entry.get("modelId", ""),
                    }
                elif etype == ENTRY_THINKING_CHANGE:
                    self._thinking_level = entry.get("thinkingLevel", "off")

    def to_messages(self) -> List[Dict[str, Any]]:
        """将所有消息转为 OpenAI messages 格式（向后兼容 — 使用 _messages）。"""
        return self._messages_to_openai(self._messages)

    def to_messages_branch(self) -> List[Dict[str, Any]]:
        """将当前分支的消息转为 OpenAI messages 格式（树感知版本）。"""
        branch = self.get_current_branch()
        return self._messages_to_openai(branch)

    def to_messages_all(self) -> List[Dict[str, Any]]:
        """将所有消息转换为 OpenAI messages 格式。"""
        return self._messages_to_openai(self._messages)

    def _messages_to_openai(self, msgs: List[Message]) -> List[Dict[str, Any]]:
        """将 Message 列表转为 OpenAI API 格式。"""
        messages: List[Dict[str, Any]] = []
        for msg in msgs:
            if msg.role == "user":
                messages.append({"role": "user", "content": msg.content})
            elif msg.role == "assistant":
                am: Dict[str, Any] = {"role": "assistant", "content": msg.content}
                reasoning = (msg.metadata or {}).get("reasoning_content")
                if reasoning:
                    am["reasoning_content"] = reasoning
                # tool_calls 处理
                meta = msg.metadata or {}
                if meta.get("action_type") == "tool_call":
                    tool_calls = meta.get("tool_calls")
                    if tool_calls:
                        try:
                            am["tool_calls"] = []
                            for call in tool_calls:
                                name = call.get("name") or "unknown_tool"
                                call_id = call.get("id")
                                arguments = call.get("arguments") or {}
                                args_str = (arguments if isinstance(arguments, str)
                                            else json.dumps(arguments, ensure_ascii=False))
                                am["tool_calls"].append({
                                    "id": call_id, "type": "function",
                                    "function": {"name": name, "arguments": args_str},
                                })
                        except Exception as exc:
                            logger.warning("Failed to build tool_calls: %s", exc)
                    else:
                        tool_name = meta.get("tool_name")
                        tool_call_id = meta.get("tool_call_id")
                        tool_args = meta.get("tool_args")
                        if tool_name and tool_call_id:
                            try:
                                am["tool_calls"] = [{
                                    "id": tool_call_id, "type": "function",
                                    "function": {"name": tool_name,
                                                 "arguments": json.dumps(tool_args or {}, ensure_ascii=False)},
                                }]
                            except Exception as exc:
                                logger.warning("Failed to build tool_calls: %s", exc)
                messages.append(am)
            elif msg.role == "tool":
                tool_name = (msg.metadata or {}).get("tool_name", "unknown")
                tool_call_id = (msg.metadata or {}).get("tool_call_id")
                if tool_call_id:
                    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": msg.content})
                else:
                    messages.append({"role": "user", "content": f"Observation ({tool_name}): {msg.content}"})
            elif msg.role == "summary":
                messages.append({"role": "system", "content": f"## Archived History Summary\n{msg.content}"})
        return messages

    def get_rounds_count(self) -> int:
        return len(self._identify_rounds())

    # ═════════════════════════════════════════════════════════════════════
    # 内部方法
    # ═════════════════════════════════════════════════════════════════════

    def _add_message(self, msg: Message) -> None:
        self._messages.append(msg)
        self._id_index[msg.message_id] = msg
        self._cursor_id = msg.message_id

    def _find_entry(self, entry_id: str) -> Optional[Dict[str, Any]]:
        for entry in self._entries:
            if entry["id"] == entry_id:
                return entry
        return None

    def _collect_branch_messages(
        self, from_cursor: Optional[str], to_ancestor: str
    ) -> List[Message]:
        """收集从 from_cursor 到共同祖先（不含）之间的消息。"""
        # 找到共同祖先
        branch_msgs: List[Message] = []
        current = from_cursor
        while current and current != to_ancestor:
            msg = self._id_index.get(current)
            if msg:
                branch_msgs.append(msg)
                current = msg.parent_id
            else:
                entry = self._find_entry(current)
                if entry:
                    current = entry.get("parentId")
                else:
                    break
        branch_msgs.reverse()
        return branch_msgs
