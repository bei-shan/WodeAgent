# 多对话管理功能设计文档

> 日期: 2026-06-22 | 优先级: P2 | 参考: Kode-Agent session model

---

## 一、功能概述

当前每次 `python scripts/chat_test_agent.py` 就是一个独立对话，虽然可以 `/save` 和 `/load`，但没有对话列表、切换、恢复能力。目标是实现 Kode-Agent 风格的多对话管理：

- 每次启动自动分配会话 ID，自动持久化
- 支持列出、切换、恢复历史对话
- CLI 参数和 slash command 双通道

---

## 二、Kode-Agent 参考

| 特性 | Kode-Agent 做法 |
|------|----------------|
| 存储 | `~/.kode/projects/<sanitized-cwd>/<uuid>.jsonl` |
| 识别 | UUID + 3-word slug + custom title + tag |
| 列表 | `/resume` → SessionSelector (表格) |
| 恢复 | `kode -c` (最近) / `kode -r <id>` (指定) / `kode -r` (选择器) |
| 重命名 | `/rename <title>` |
| 标签 | `/tag <tag>` |
| 自动持久化 | 每轮对话增量追加 JSONL |
| 分组 | 按项目 cwd 分目录 |

---

## 三、MyCodeAgent 实现设计

### 3.1 简化点（vs Kode-Agent）

- **不分组** — v1 所有对话存在 `memory/sessions/` 下，不按项目分目录
- **不含 tag** — v1 只有 title，tag 是 v2 功能
- **不用 slug** — 用创建时间戳 + 首条消息摘要代替
- **不用 JSONL 增量追加** — 复用现有的 `build_session_snapshot` 全量快照格式，每次保存覆盖

### 3.2 存储结构

```
memory/sessions/
  ├── index.json                      ← 对话索引
  │   [
  │     {
  │       "id": "abc123",
  │       "title": "fix login bug",
  │       "created_at": "2026-06-22T10:00:00",
  │       "modified_at": "2026-06-22T10:15:00",
  │       "message_count": 12,
  │       "preview": "你好，帮我修复登录页面的bug..."  ← 首条用户消息
  │     }
  │   ]
  ├── abc123.json                     ← 对话快照 (build_session_snapshot 格式)
  ├── def456.json
  └── ...
```

### 3.3 架构

```
core/session_manager.py (新建)
  └── SessionManager
      ├── _load_index() / _save_index()      → index.json 读写
      ├── create_session(title)               → 生成 ID + 创建空快照
      ├── save_session(agent)                 → 保存当前对话快照
      ├── load_session(agent, session_id)     → 恢复对话
      ├── list_sessions()                     → 返回对话列表
      ├── rename_session(session_id, title)   → 重命名
      ├── delete_session(session_id)          → 删除
      ├── get_current_id()                    → 当前会话 ID
      └── auto_title(first_user_message)      → 截取首条消息作为标题
```

### 3.4 CodeAgent 集成

```python
class CodeAgent:
    _session_id: str              # 当前会话 ID
    _session_manager: SessionManager

    def __init__(self, ...):
        self._session_manager = SessionManager(...)
        self._session_id = self._session_manager.create_session()
        # 自动保存当前对话（每轮用户输入后）

    def auto_save(self):
        self._session_manager.save_session(self)

    def resume_session(self, session_id):
        self._session_manager.load_session(self, session_id)
```

### 3.5 CLI 集成

```bash
# 新对话（默认）
python scripts/chat_test_agent.py

# 恢复最近对话
python scripts/chat_test_agent.py -c
python scripts/chat_test_agent.py --continue

# 恢复指定对话
python scripts/chat_test_agent.py -r abc123
python scripts/chat_test_agent.py --resume abc123
python scripts/chat_test_agent.py --resume 2    # 按序号恢复
```

### 3.6 Slash Commands

```
/sessions               → 列出所有对话（表格格式）
/resume [id|序号]        → 切换/恢复对话
/rename <title>          → 重命名当前对话
```

### 3.7 自动保存策略

- **创建时**：创建空快照
- **每轮用户输入后**：自动保存当前快照
- **退出时**：最终保存
- **切换对话时**：先保存当前对话，再加载目标对话

---

## 四、文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/session_manager.py` | **新建** | SessionManager (~200 行) |
| `agents/codeAgent.py` | **修改** | 集成 SessionManager (+30 行) |
| `scripts/chat_test_agent.py` | **修改** | -c/-r 参数 + /sessions/resume/rename 命令 (+60 行) |
| `tests/test_session_manager.py` | **新建** | 测试 (~150 行) |

### 预估

| 文件 | 行数 |
|------|------|
| `core/session_manager.py` | ~200 |
| `codeAgent.py` (修改) | +30 |
| `chat_test_agent.py` (修改) | +60 |
| `tests/test_session_manager.py` | ~150 |
| **总计** | **~440** |

---

## 五、实施步骤

```
Step 1: 创建 core/session_manager.py
Step 2: 修改 agents/codeAgent.py (集成 SessionManager + 自动保存)
Step 3: 修改 scripts/chat_test_agent.py (CLI 参数 + 新 slash commands)
Step 4: 编写测试 test_session_manager.py
Step 5: 全量测试验证
```

---

## 六、不做的事情 (v1)

- 按项目分目录 — v2
- Tag 标签 — v2
- 对话 fork — v2
- JSONL 增量追加 — 当前快照格式够用
- 跨项目对话发现 — v2
