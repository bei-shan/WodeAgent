"""JSONL 会话存储 — 兼容 Pi Agent 的会话树格式。

单个 JSONL 文件存储整个会话树，每行一个条目。
支持追加写入、从任意节点沿 parent_id 链回溯构建上下文。

格式（与 Pi 兼容）:
    {"type":"session","version":1,"id":"s_xxx","timestamp":"...","cwd":"..."}
    {"type":"message","id":"m001","parentId":null,"role":"user","content":"..."}
    {"type":"leaf","id":"lf01","parentId":"m003","targetId":"m001"}
    ...

用法:
    store = JsonlSessionStore.create(filepath, cwd=".", session_id="s_xxx")
    store.append_entry({"type":"message","id":"m001","parentId":None,...})
    store.set_leaf("m001")
    path = store.get_path_to_root("m001")  # → [root_entry, ..., m001]
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


class JsonlSessionStore:
    """单个 JSONL 文件的会话树存储。

    追加写入模式 — 每条 entry 追加到文件末尾。
    内存中维护 id→entry 索引和 leaf_id 光标。
    """

    def __init__(self, filepath: Path) -> None:
        self._filepath = Path(filepath)
        self._entries: List[Dict[str, Any]] = []
        self._by_id: Dict[str, Dict[str, Any]] = {}
        self._leaf_id: Optional[str] = None
        self._labels: Dict[str, str] = {}
        self._metadata: Dict[str, Any] = {}

    # ── 工厂方法 ────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        filepath: str | Path,
        cwd: str = ".",
        session_id: Optional[str] = None,
        parent_session_path: Optional[str] = None,
    ) -> "JsonlSessionStore":
        """创建新的 JSONL 会话文件并写入 session header。"""
        fp = Path(filepath)
        fp.parent.mkdir(parents=True, exist_ok=True)
        sid = session_id or f"s_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_short_id()}"
        header = {
            "type": "session",
            "version": 1,
            "id": sid,
            "timestamp": datetime.now().isoformat(),
            "cwd": cwd,
        }
        if parent_session_path:
            header["parentSession"] = parent_session_path
        fp.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")

        store = cls(fp)
        store._metadata = {
            "id": sid,
            "createdAt": header["timestamp"],
            "cwd": cwd,
            "path": str(fp),
        }
        return store

    @classmethod
    def open(cls, filepath: str | Path) -> "JsonlSessionStore":
        """从已有的 JSONL 文件加载会话树。"""
        fp = Path(filepath)
        content = fp.read_text(encoding="utf-8")
        lines = [l for l in content.split("\n") if l.strip()]

        if not lines:
            raise ValueError(f"Empty session file: {filepath}")

        header = json.loads(lines[0])
        if header.get("type") != "session":
            raise ValueError(f"Invalid session header in {filepath}")

        store = cls(fp)
        store._metadata = {
            "id": header.get("id", ""),
            "createdAt": header.get("timestamp", ""),
            "cwd": header.get("cwd", "."),
            "path": str(fp),
        }

        leaf_id: Optional[str] = None
        for i, line in enumerate(lines[1:], start=2):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at line {i} in {filepath}: {exc}")
            store._entries.append(entry)
            eid = entry.get("id", "")
            if eid:
                store._by_id[eid] = entry

            # 追踪 leaf_id
            if entry.get("type") == "leaf":
                leaf_id = entry.get("targetId")
            elif eid:
                leaf_id = eid

            # 追踪 labels
            if entry.get("type") == "label":
                target = entry.get("targetId")
                label = entry.get("label")
                if label:
                    store._labels[target] = label
                elif target in store._labels:
                    del store._labels[target]

        store._leaf_id = leaf_id
        return store

    # ── 属性 ──────────────────────────────────────────────────────────

    @property
    def filepath(self) -> Path:
        return self._filepath

    @property
    def metadata(self) -> Dict[str, Any]:
        return dict(self._metadata)

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    # ── 写入 ──────────────────────────────────────────────────────────

    def create_entry_id(self) -> str:
        """生成唯一的短 ID（碰撞重试）。"""
        for _ in range(100):
            eid = _short_id()
            if eid not in self._by_id:
                return eid
        return _short_id()

    def append_entry(self, entry: Dict[str, Any]) -> str:
        """追加条目到文件末尾。返回 entry_id。

        自动设置 parentId 为当前 leaf_id（如果未提供）。
        """
        if "id" not in entry:
            entry["id"] = self.create_entry_id()
        if "parentId" not in entry:
            entry["parentId"] = self._leaf_id
        if "timestamp" not in entry:
            entry["timestamp"] = datetime.now().isoformat()

        eid = entry["id"]
        with open(self._filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self._entries.append(entry)
        self._by_id[eid] = entry
        self._leaf_id = eid

        if entry.get("type") == "label":
            target = entry.get("targetId")
            label = entry.get("label")
            if label:
                self._labels[target] = label
            elif target in self._labels:
                del self._labels[target]

        return eid

    def set_leaf(self, target_id: Optional[str]) -> str:
        """移动光标到指定节点。写入 leaf 条目。"""
        if target_id is not None and target_id not in self._by_id:
            raise ValueError(f"Target entry not found: {target_id}")

        leaf_entry = {
            "type": "leaf",
            "id": self.create_entry_id(),
            "parentId": self._leaf_id,
            "timestamp": datetime.now().isoformat(),
            "targetId": target_id,
        }
        self.append_entry(leaf_entry)
        self._leaf_id = target_id
        return leaf_entry["id"]

    # ── 读取 ──────────────────────────────────────────────────────────

    def get_entry(self, entry_id: str) -> Optional[Dict[str, Any]]:
        return self._by_id.get(entry_id)

    def get_leaf_id(self) -> Optional[str]:
        return self._leaf_id

    def get_label(self, entry_id: str) -> Optional[str]:
        return self._labels.get(entry_id)

    def get_entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def get_path_to_root(self, leaf_id: Optional[str]) -> List[Dict[str, Any]]:
        """从 leaf_id 沿 parentId 链走到根。返回正序列表。"""
        if leaf_id is None:
            return []
        path: List[Dict[str, Any]] = []
        current: Optional[str] = leaf_id
        while current is not None:
            entry = self._by_id.get(current)
            if entry is None:
                break
            path.insert(0, entry)  # 倒序插入 = 最终正序
            current = entry.get("parentId")
        return path

    def find_entries(self, entry_type: str) -> List[Dict[str, Any]]:
        """查找指定类型的所有条目。"""
        return [e for e in self._entries if e.get("type") == entry_type]
