# 会话树设计文档

> 日期: 2026-06-25 | 来源: Pi Agent 源码分析 | 优先级: P1

---

## 一、Pi 会话树源码分析

### 1.1 核心数据结构

Pi 的会话存储在**单个 JSONL 文件**中，每行一个 `SessionTreeEntry`：

```typescript
// 每个 entry 都有这些字段
interface SessionTreeEntry {
  id: string;        // 短 ID (uuidv7 前 8 位)
  parentId: string | null;  // 父节点 ID (null = 根)
  timestamp: string; // ISO 时间戳
  type: string;      // 条目类型
}
```

### 1.2 条目类型

| 类型 | 用途 | 是否进入 LLM 上下文 |
|------|------|-------------------|
| `message` | 实际对话消息 (user/assistant/toolResult) | ✅ |
| `compaction` | 压缩摘要 + `firstKeptEntryId` | ✅ (摘要文本) |
| `branch_summary` | 离开分支时的摘要 | ✅ (摘要文本) |
| `model_change` | 模型切换记录 | ❌ (影响上下文构建参数) |
| `thinking_level_change` | 思考深度变化 | ❌ (影响 API reasoning 参数) |
| `leaf` | 光标移动记录 (`moveTo` 产生) | ❌ |
| `label` | 节点标签 | ❌ |
| `active_tools_change` | 工具集变化 | ❌ |
| `session_info` | 会话元信息 | ❌ |
| `custom` / `custom_message` | 扩展自定义条目 | ✅ (custom_message) |

### 1.3 树是如何形成的

```
JSONL 文件内容示例:

{"type":"session","version":3,"id":"abc123",...}             ← 文件头
{"type":"message","id":"001","parentId":null,...}             ← 根消息
{"type":"message","id":"002","parentId":"001",...}            ← 线性增长
{"type":"message","id":"003","parentId":"002",...}
{"type":"leaf","id":"004","parentId":"003","targetId":"001"}  ← 光标跳回 001
{"type":"branch_summary","id":"bs1","parentId":"001",...}     ← 离开分支的摘要
{"type":"message","id":"005","parentId":"001",...}            ← 新分支从 001 开始

形成的树:
  root → 001 → 002 → 003 → 004(leaf)
         ↑                   ↓ target=001
         └── bs1 ← 005 ←────┘ (新分支)
```

**关键机制**：
- `appendMessage()` 的 `parentId` 始终指向 `getLeafId()`（当前光标）
- `moveTo(targetId)` 创建 `leaf` 记录，`targetId` 指向目标节点 → 后续消息自动分叉
- `navigateTree()` 离开分支时可选的 `branch_summary` 生成

### 1.4 上下文构建 (buildSessionContext)

```typescript
// 1. 从当前 leaf 沿 parentId 链走到根 → pathEntries[]
// 2. 遍历 pathEntries:
//    - message/custom_message/branch_summary → 转为 AgentMessage
//    - model_change → 记录当前模型
//    - thinking_level_change → 记录当前思考级别
//    - compaction → 跳过 firstKeptEntryId 之前的消息，注入摘要
// 3. 返回 { messages, thinkingLevel, model, activeToolNames }
```

### 1.5 压缩在树中的处理

```
树中有 compaction 条目:
  ... → compaction(summary="...", firstKeptEntryId="msg50") → msg50 → msg51 → ...

构建上下文时:
  1. 遇到 compaction → 注入摘要文本作为 compactionSummary 消息
  2. 只保留 firstKeptEntryId("msg50") 之后的消息
  3. msg50 之前的消息被跳过（但仍在 JSONL 中，可回溯）
```

---

## 二、MyCodeAgent 实现设计

### 2.1 数据模型

```python
# core/message.py — 扩展 Message
class Message(BaseModel):
    content: str
    role: MessageRole  # "user" | "assistant" | "tool" | "summary"
    timestamp: datetime = None
    metadata: Optional[dict] = None
    message_id: str = ""       # 新增: 短 ID (uuid7 前 8 位)
    parent_id: str | None = None  # 新增: 父消息 ID (None=根)

# 新增: 会话树条目类型
class SessionEntryType:
    MESSAGE = "message"
    COMPACTION = "compaction"
    BRANCH_SUMMARY = "branch_summary"
    MODEL_CHANGE = "model_change"
    THINKING_CHANGE = "thinking_level_change"
    LEAF = "leaf"
    LABEL = "label"
    SESSION_INFO = "session_info"
```

### 2.2 JSONL 存储格式

```jsonl
{"type":"session","version":1,"id":"s_20260625_abc123","timestamp":"2026-06-25T...","cwd":"D:/project"}
{"type":"message","id":"m001","parentId":null,"role":"user","content":"帮我写API","timestamp":"..."}
{"type":"message","id":"m002","parentId":"m001","role":"assistant","content":"好的...","model":"deepseek-v4-pro","timestamp":"..."}
{"type":"message","id":"m003","parentId":"m002","role":"tool","tool_name":"Read","content":"...","timestamp":"..."}
{"type":"model_change","id":"mc01","parentId":"m003","provider":"deepseek","modelId":"deepseek-chat","timestamp":"..."}
{"type":"leaf","id":"lf01","parentId":"m003","targetId":"m001","timestamp":"..."}
{"type":"branch_summary","id":"bs01","parentId":"m001","summary":"创建了REST API...","fromId":"m003","timestamp":"..."}
{"type":"message","id":"m005","parentId":"m001","role":"user","content":"用FastAPI重写","timestamp":"..."}
```

### 2.3 HistoryManager 改造

```python
class HistoryManager:
    _messages: list[Message]       # 所有消息（保留，兼容旧接口）
    _cursor_id: str | None         # 新增: 当前光标位置
    _id_index: dict[str, Message]  # 新增: ID → Message 索引
    _entries: list[dict]           # 新增: 完整的树条目列表 (用于 JSONL)

    # ── 新增方法 ──
    def fork(self, target_id: str) -> str:
        """从指定消息分叉。返回新光标 ID。"""
        ...

    def get_current_branch(self) -> list[Message]:
        """从 cursor 沿 parent_id 链走到根。"""
        ...

    def get_tree(self) -> dict:
        """返回完整树结构（用于 /tree 命令）。"""
        ...

    def navigate_to(self, target_id: str, summarize: bool = False) -> bool:
        """移动光标到目标节点，可选生成 branch_summary。"""
        ...

    def append_model_change(self, provider: str, model_id: str) -> None:
        """记录模型切换。"""
        ...

    def append_thinking_change(self, level: str) -> None:
        """记录思考深度变化。"""
        ...

    # ── 修改的方法 ──
    def append_user/assistant/tool(self, ...):
        # 自动设置 parent_id = self._cursor_id
        ...

    # ── 保留兼容的方法 ──
    def get_messages(self) -> list[Message]:
        # 返回当前分支的消息（向后兼容）
        ...
```

### 2.4 SessionStore 升级

```python
# 快照格式 v2
{
    "version": 2,
    "cursor_id": "m005",          # 新增
    "system_messages": [...],
    "history_entries": [...],     # 替代 history_messages — 完整的树条目
    "tool_schema_hash": "...",
    "read_cache": {...},
    "teams_snapshot": {...},
    "project_root": "...",
}
```

同时支持 JSONL 格式存储（与 Pi 兼容）：
```python
class JsonlSessionStore:
    """单个 JSONL 文件的会话存储。"""
    
    @staticmethod
    def create(filepath: Path, cwd: str, session_id: str) -> "JsonlSessionStore": ...
    
    @staticmethod
    def open(filepath: Path) -> "JsonlSessionStore": ...
    
    def append_entry(self, entry: dict) -> str: ...  # 返回 entry_id
    def get_path_to_root(self, leaf_id: str) -> list[dict]: ...
    def get_entry(self, entry_id: str) -> dict | None: ...
    def set_leaf(self, target_id: str) -> None: ...
    def get_leaf_id(self) -> str | None: ...
```

### 2.5 thinking_level_change 适配

DeepSeek 不支持多级 thinking，但支持 reasoning 开关。适配方案：

```python
# DeepSeek: reasoning_content 自动返回（模型自行决定）
# 所以 thinking_level 对 DeepSeek 简化为 "off" | "on"
# 后续切换到 Claude 时扩展为 "off" | "minimal" | "low" | "medium" | "high" | "xhigh"

THINKING_LEVELS_DEEPSEEK = {"off", "on"}
THINKING_LEVELS_CLAUDE = {"off", "minimal", "low", "medium", "high", "xhigh"}

# 在 LLM 调用时:
if thinking_level != "off":
    request_kwargs["reasoning_effort"] = thinking_level  # 对 DeepSeek: 直接传
```

### 2.6 Slash Commands

```
/tree              显示会话树（ASCII 树形图，带标签和时间）
/tree <id>         显示以某个节点为根的子树
/fork <id>         从指定消息分叉（新分支）
/fork <id> --summarize  分叉并生成离开分支的摘要
/branch            列出所有分支
/branch <name>     切换到命名分支
/thinking          显示当前思考级别
/thinking on|off   切换思考深度（DeepSeek 二态）
```

---

## 三、实施计划

### Step 1: Message 模型 + 短 ID 生成
- `Message` 加 `message_id` 和 `parent_id`
- `generate_short_id()` 函数（uuid7 前 8 位）
- 向后兼容：旧消息自动分配 ID

### Step 2: HistoryManager 树形支持
- 加 `_cursor_id`、`_id_index`、`_entries`
- 加 `fork()`、`get_current_branch()`、`get_tree()`、`navigate_to()`
- 改 `append_*()` 自动设置 `parent_id`
- 改 `compact()` 保留 compaction 条目而非删除

### Step 3: JsonlSessionStore
- JSONL 文件读写
- `create()` / `open()` / `append_entry()` / `get_path_to_root()`
- `set_leaf()` / `get_leaf_id()`

### Step 4: SessionStore 升级到 v2
- 快照版本 v2：`history_entries` + `cursor_id`
- 向后兼容 v1 快照自动升级
- JSONL 格式作为备选存储

### Step 5: model_change + thinking_change + branch_summary
- `append_model_change()` — SwitchModel 工具自动调用
- `append_thinking_change()` — `/thinking` 命令触发
- `navigate_to(summarize=True)` — 调用 LLM 生成 branch_summary

### Step 6: TUI 命令
- `/tree` — Rich Tree 组件可视化
- `/fork <id>` — 分叉
- `/thinking on|off` — 思考切换

### Step 7: 测试
- 树遍历正确性
- fork 后上下文隔离
- JSONL 读写
- 快照 v1→v2 迁移
- compaction 在树中的行为

---

## 四、文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/message.py` | 修改 | +message_id, +parent_id (~15 行) |
| `core/context_engine/history_manager.py` | 重构 | 树形支持 (~200 行) |
| `core/session_store.py` | 修改 | v2 快照 + JSONL (~120 行) |
| `agents/codeAgent.py` | 修改 | /tree /fork /thinking 命令 (~40 行) |
| `core/model_profiles.py` | 修改 | thinking_level 配置 (~20 行) |
| `tui/streaming.py` | 修改 | /tree 树形展示 (~30 行) |
| `tests/test_session_tree.py` | 新建 | 树形会话测试 (~200 行) |
| **总计** | | **~625 行** |

---

## 五、不做的事情

- 不照搬 Pi 的 `active_tools_change`（工具集固定，不需要动态记录）
- 不做 `custom` / `custom_message`（v1 不需要扩展）
- 不实现 hook 系统的 `session_before_tree` 事件（v2）
- JSONL 存储作为**可选格式**，默认仍用 JSON 快照（向后兼容）
