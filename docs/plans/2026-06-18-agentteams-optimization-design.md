# AgentTeams 优化设计文档

> 状态：草稿 → 待审阅
> 日期：2026-06-18
> 目标：基于完成度审计发现的问题，对 AgentTeams 模块进行系统化优化
> 前提：本次优化保持向后兼容，不破坏现有 API 和测试

---

## 零、审阅结论

| 决策点 | 结论 |
|--------|------|
| 优化范围 | P1+P2+P3 共 10 项，P4 作为后续优化 |
| 向后兼容 | 所有改动保持现有 API 不变，新增为可选参数 |
| 持久化策略 | 沿用现有 JSONL 文件存储，不引入 sqlite |
| 测试策略 | 每个改动点配套单元测试 |

---

## 一、背景

基于 2026-06-18 的 AgentTeams 模块完成度审计，模块整体完成度约 95%，无 NotImplementedError 或 TODO 标记。但存在 13 个优化点，按优先级分为 P1（健壮性）、P2（性能）、P3（功能补全）、P4（边界情况）。

本次设计覆盖 P1+P2+P3 共 10 项，P4 留作后续。

---

## 二、P1 健壮性修复（3 项）

### 2.1 Worker 静默退出无感知

**现状问题：**

`TeammateWorker.run()` 空闲超时后静默退出（`worker.py:66 break`），Manager 完全不知道。之后如果有消息发给这个 teammate，`send_message` 会尝试 `_start_worker` 重新拉起——但当前代码中 `send_message` 只对非 lead 的 recipient 调用 `_start_worker`，且 `_start_worker` 在 `WorkerSupervisor` 中如果 worker 已存在会静默跳过（需确认）。

**影响链路：**
```
Worker 空闲退出
  → Manager 不知道
  → team_status 仍显示 "active"（基于 export_state 的快照数据）
  → 新消息到达时 _start_worker 创建新线程，但旧状态残留
```

**设计方案：**

1. `TeammateWorker` 增加 `on_stop: Optional[Callable[[], None]]` 回调参数
2. `run()` 退出前（finally 或 stopped 状态设置后）调用 `on_stop()`
3. `WorkerSupervisor.start_worker()` 接受 `on_stop` 参数并传递给 Worker
4. `TeamManager._start_worker()` 传入 `on_stop` 回调，回调中：
   - 从 `worker_supervisor.workers` 中移除该条目
   - 可选：emit `EVENT_WORKER_STOPPED` 事件

**改动文件：**
- `core/team_engine/worker.py` — 增加 `on_stop` 参数，`run()` 退出前调用
- `core/team_engine/supervisor.py` — `start_worker` 传递 `on_stop`
- `core/team_engine/manager.py` — `_start_worker` 传入回调
- `core/team_engine/protocol.py` — 增加 `EVENT_WORKER_STOPPED` 事件类型常量

**影响范围：** 仅新增可选参数，现有调用无需修改（`on_stop` 默认 `None`）

---

### 2.2 会话恢复时消息重复处理

**现状问题：**

`_processed_by_member: Dict[tuple, set]` 是纯内存结构（`manager.py:97`）。`import_state()` 不恢复这个集合（`manager.py:552-553` 只清空 `_plan_approvals`）。重启后，inbox 中已处理的消息会被重新处理——最坏情况下，已完成的 work item 会被重复创建。

**影响链路：**
```
会话 A: Worker 处理消息 msg_001 → 创建 work item → 完成 → processed_ids.add("msg_001")
  → 会话保存 (export_state) → _processed_by_member 未导出
会话 B: 会话恢复 (import_state) → _processed_by_member 为空
  → Worker 启动 → 读取 inbox → msg_001 再次出现 → 重复创建 work item
```

**设计方案：**

方案 A（最小改动，推荐）：在 `export_state()` 中序列化 `_processed_by_member`，在 `import_state()` 中恢复。

```python
# export_state() 增加
"processed_messages": {
    f"{team}|{member}": list(msg_ids)
    for (team, member), msg_ids in self._processed_by_member.items()
}

# import_state() 增加
snapshot_processed = snapshot.get("processed_messages")
if isinstance(snapshot_processed, dict):
    for key, msg_ids in snapshot_processed.items():
        if "|" in key:
            team, member = key.split("|", 1)
            self._processed_by_member[(team, member)] = set(msg_ids)
```

方案 B（更彻底）：在 inbox JSONL 消息上持久化 `processed` 状态标记。但这需要改动 `TeamStore` 的消息格式和读取逻辑，改动范围大，风险高。

**选择方案 A。** 原因：
- 改动最小（仅 `export_state` + `import_state` 两个方法）
- 与现有快照机制一致
- 已处理消息 ID 集合通常很小（每个 teammate 几十条），不会让快照膨胀

**改动文件：**
- `core/team_engine/manager.py` — `export_state()` 增加 `processed_messages` 字段，`import_state()` 恢复

**影响范围：** `export_state` 返回格式新增一个可选字段（向后兼容），`import_state` 新增恢复逻辑

---

### 2.3 ApprovalService 纯内存，重启丢失

**现状问题：**

`ApprovalService` 所有审批请求存在 `self._requests: Dict`（`approval.py:17`）。进程重启后全部丢失，teammate 等待审批的 `plan_approval_response` 永远不会到达。

**影响链路：**
```
Teammate 认领任务 → require_plan_approval=True
  → ApprovalService.create_request() → 内存中 pending
  → LLM 看到 EVENT_PLAN_APPROVAL_REQUESTED
  → 用户说 "approve" → LLM 调用 TeamApprovePlan
  → 正常流程 OK

但如果进程在 create_request 之后、approve 之前重启：
  → 内存丢失 → teammate 的 plan_approval 请求永远得不到响应
  → teammate 永远不会创建对应的 work item
```

**设计方案：**

将审批请求持久化到 `TeamStore` 的 team 目录下，通过 `export_state`/`import_state` 保持会话一致性。

1. `TeamStore` 增加 `save_approval(team, request)` / `load_approvals(team)` / `delete_approval(team, request_id)` 方法，存储为 `<team_dir>/approvals.jsonl`
2. `ApprovalService` 增加 `load_from_store(team, store)` 方法，启动时恢复
3. `TeamManager.export_state()` 中已有 `approvals` 字段（`manager.py:504-508`），无需改动
4. `TeamManager.import_state()` 中恢复审批：读取磁盘的 approvals.jsonl，重建 `ApprovalService._requests`

**简化方案（推荐）：** 不在 `TeamStore` 中新增审批文件，而是利用现有的 `export_state` 快照。`import_state()` 时如果快照中包含 `approvals` 数据，重建 `ApprovalService._requests`。

具体做法：
```python
# import_state() 中增加审批恢复
snapshot_approvals = snapshot.get("approvals")
if isinstance(snapshot_approvals, dict):
    for team_name, counts in snapshot_approvals.items():
        # counts 只有数量，没有完整请求数据
        # 需要改为 export 完整请求列表
        pass
```

当前 `export_state` 的 `approvals` 只有 counts（`{"pending": 1, "approved": 0, "rejected": 0}`），不包含完整的请求数据。需要改为同时导出完整请求列表。

**最终方案：**

1. `ApprovalService` 增加 `export_team(team_name) -> List[dict]` 方法，返回该 team 的所有请求完整数据
2. `TeamManager.export_state()` 的 `approvals` 字段改为 `{"counts": {...}, "requests": [...]}`
3. `TeamManager.import_state()` 恢复审批请求到 `ApprovalService._requests`

**改动文件：**
- `core/team_engine/approval.py` — 增加 `export_team()` 方法
- `core/team_engine/manager.py` — `export_state()` 调整 approvals 格式，`import_state()` 恢复审批

**影响范围：** `export_state` 的 `approvals` 字段格式变更（从 `dict[str, dict]` 变为 `dict[str, dict]`），需要检查 `codeAgent.py` 和 `progress_view.py` 中对 `approvals` 的使用

---

## 三、P2 性能优化（3 项）

### 3.1 Inbox 全量读取

**现状问题：**

`_process_member_inbox` 每次轮询（0.02s 间隔）调用 `TeamStore.read_inbox_messages()`（`manager.py:599`），读取**全部** JSONL 行后在内存中过滤已处理的。随着 inbox 增长，每次轮询都重新解析全部 JSONL。

**设计方案：**

在 `_processed_by_member` 基础上，增加 **最后处理的消息行号** 记录。下次读取时只读取该行号之后的消息。

1. `TeamStore` 增加 `read_inbox_messages_from(team, member, start_line: int)` 方法，只读取第 N 行之后的 JSONL
2. `_processed_by_member` 改为记录 `(team, member) -> {"msg_ids": set, "last_line": int}`
3. `_process_member_inbox` 从 `last_line` 之后开始读取

或者更简单的方案：在 `_processed_by_member` 旁边增加 `_inbox_cursor: Dict[tuple, int]`，记录每个 teammate 已处理到的行号。

```python
# 新增
self._inbox_cursor: Dict[tuple[str, str], int] = defaultdict(lambda: 0)

# _process_member_inbox 中
cursor = self._inbox_cursor[(team_name, teammate_name)]
rows = self.store.read_inbox_messages_from(team_name, teammate_name, cursor)
# ... 处理消息 ...
self._inbox_cursor[(team_name, teammate_name)] = cursor + len(rows)
```

**TeamStore 新增方法：**
```python
def read_inbox_messages_from(self, team_name: str, member_name: str, start_line: int = 0) -> List[Dict]:
    """Read inbox messages starting from line N (0-based)."""
    path = self._inbox_path(team_name, member_name)
    if not path.exists():
        return []
    rows = self._read_jsonl(path)
    return rows[start_line:]
```

**改动文件：**
- `core/team_engine/store.py` — 增加 `read_inbox_messages_from()` 方法（或修改 `read_inbox_messages` 增加 `offset` 参数）
- `core/team_engine/manager.py` — 增加 `_inbox_cursor`，`_process_member_inbox` 使用增量读取，`export_state`/`import_state` 持久化 cursor

**影响范围：** 新增可选参数，现有 `read_inbox_messages` 调用不受影响

---

### 3.2 消息/工作项无上限

**现状问题：**

Inbox JSONL 和 work_items JSONL 只追加不清理。长时间运行的 team 文件会无限增长，每次读取都要解析更多数据。

**设计方案：**

1. 添加环境变量配置上限：
   - `TEAM_MAX_INBOX_SIZE`（默认 10000）— 单成员 inbox 消息上限
   - `TEAM_MAX_WORK_ITEMS`（默认 5000）— 单成员 work items 上限
2. `TeamStore.append_inbox_message()` 写入后检查行数，超限时截断（保留最新 N 条）
3. `TeamStore.create_work_item()` 写入后检查行数，超限时归档旧条目

具体实现：
```python
# store.py
def _trim_jsonl(self, path: Path, max_lines: int) -> None:
    """Keep only the last max_lines entries in a JSONL file."""
    if not path.exists():
        return
    rows = self._read_jsonl(path)
    if len(rows) <= max_lines:
        return
    # 保留最新的 max_lines 条
    trimmed = rows[-max_lines:]
    with open(path, 'w', encoding='utf-8') as f:
        for row in trimmed:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')

def append_inbox_message(self, team_name, member_name, message):
    # ... existing logic ...
    max_inbox = int(os.getenv("TEAM_MAX_INBOX_SIZE", "10000"))
    self._trim_jsonl(inbox_path, max_inbox)
```

**改动文件：**
- `core/team_engine/store.py` — 增加 `_trim_jsonl()` 方法，`append_inbox_message` 和 `create_work_item` 后调用
- `.env.example` — 增加 `TEAM_MAX_INBOX_SIZE`、`TEAM_MAX_WORK_ITEMS` 配置项

**影响范围：** 纯新增，不影响现有逻辑

---

### 3.3 文件锁竞争

**现状问题：**

`TeamStore.lock()` 使用 `mkdir(parents=True, exist_ok=False)` 实现文件锁（`store.py:73-74`）。在高并发 fanout 场景下（多个 worker 同时读写 inbox 和 work items），频繁的 mkdir/rmtree 操作产生大量文件系统 syscall。

**评估：** 当前默认并发数为 `TEAM_LLM_MAX_CONCURRENCY=4`，且 worker 轮询间隔为 0.02s，实际并发压力不大。mkdir 锁在 Windows/Linux 上都能可靠工作。

**方案决策：暂时不优化。** 原因：
1. 当前并发量（4 worker + 0.02s 轮询）不足以成为瓶颈
2. 切换到 fcntl/msvcrt 文件锁会引入平台差异复杂度
3. 切换到 sqlite 会大幅增加改动范围
4. 等到实际出现性能问题时再优化

**不做改动。**

---

## 四、P3 功能补全（4 项）

### 4.1 缺少 Team 列表工具

**现状问题：**

13 个 team 工具覆盖增删查改和任务管理，但没有 `TeamList` 工具。LLM 无法知道当前有哪些 team，只能靠记忆或从运行时摘要中推断。

**设计方案：**

新增 `TeamListTool`，调用 `TeamStore.list_teams()` 返回摘要。

```python
# tools/builtin/team_list.py (新文件)

class TeamListTool(Tool):
    """List all existing teams."""
    
    def run(self, parameters):
        teams = self._team_manager.store.list_teams()
        summaries = []
        for name in teams:
            try:
                status = self._team_manager.get_status(name)
                summaries.append({
                    "team_name": name,
                    "member_count": len(status.get("members", [])),
                    "active_teammates": status.get("active_teammates", 0),
                    "idle_teammates": status.get("idle_teammates", 0),
                })
            except Exception:
                summaries.append({"team_name": name, "error": "unavailable"})
        return self.create_success_response(data={"teams": summaries}, ...)
```

**改动文件：**
- `tools/builtin/team_list.py` — 新增文件
- `prompts/tools_prompts/team_list_prompt.py` — 新增 prompt 文件
- `agents/codeAgent.py` — `_register_agent_teams_tools` 增加注册
- `tests/test_team_tools.py` — 增加 `TeamListTool` 测试

---

### 4.2 缺少失败重试工具

**现状问题：**

`TeamManager.retry_failed_work()` 已实现（`manager.py:468`），但未暴露为 LLM 可调用的工具。LLM 看到 `EVENT_WORK_ITEM_FAILED` 后无法主动重试。

**设计方案：**

新增 `TeamRetryTool`，包装 `retry_failed_work()`。

```python
# tools/builtin/team_retry.py (新文件)

class TeamRetryTool(Tool):
    """Retry a failed work item."""
    
    def get_parameters(self):
        return [
            ToolParameter(name="team_name", type="string", required=True, ...),
            ToolParameter(name="work_id", type="string", required=True, ...),
        ]
    
    def run(self, parameters):
        team_name = parameters["team_name"]
        work_id = parameters["work_id"]
        try:
            item = self._team_manager.retry_failed_work(team_name, work_id)
            return self.create_success_response(data=item, ...)
        except TeamManagerError as exc:
            return self.create_error_response(error_code=map_team_error_code(exc.code), ...)
```

**改动文件：**
- `tools/builtin/team_retry.py` — 新增文件
- `prompts/tools_prompts/team_retry_prompt.py` — 新增 prompt 文件
- `agents/codeAgent.py` — `_register_agent_teams_tools` 增加注册
- `tests/test_team_tools.py` — 增加 `TeamRetryTool` 测试

---

### 4.3 Worker 健康状态不透明

**现状问题：**

`team_status` 返回 `active_teammates` / `idle_teammates`（来自 `WorkerSupervisor.team_state()`），但看不到：
- Worker 的 `last_error`
- Worker 的 `last_active` 时间
- 已处理消息数
- 已执行 work item 数

**设计方案：**

1. `TeammateWorker` 增加统计字段：`messages_processed: int`、`work_items_executed: int`
2. `WorkerSupervisor.team_state()` 返回每个 worker 的详细信息
3. `TeamManager.get_status()` 合并 worker 详情

```python
# supervisor.py team_state() 返回值增强
def team_state(self, team_name: str) -> dict:
    teammates = {}
    for (t, name), worker in self.workers.items():
        if t != team_name:
            continue
        teammates[name] = {
            "state": worker.state,
            "last_active": worker.last_active,
            "last_error": worker.last_error,
            "messages_processed": worker.messages_processed,
            "work_items_executed": worker.work_items_executed,
        }
    active = sum(1 for w in teammates.values() if w["state"] == "active")
    idle = sum(1 for w in teammates.values() if w["state"] == "idle")
    return {
        "teammates": teammates,
        "active_teammates": active,
        "idle_teammates": idle,
        "stopped_teammates": sum(1 for w in teammates.values() if w["state"] == "stopped"),
    }
```

**改动文件：**
- `core/team_engine/worker.py` — 增加统计字段
- `core/team_engine/supervisor.py` — `team_state()` 返回详细信息
- `core/team_engine/manager.py` — `get_status()` 合并
- `tests/test_team_worker.py` — 增加统计字段测试

**影响范围：** `team_state()` 返回值格式变化，需检查调用方（`export_state`、`get_status`、`progress_view`）

---

### 4.4 TurnExecutor 系统提示词硬编码

**现状问题：**

`ExecutionService._run_turn_executor_work()` 中 worker 的 system prompt 硬编码（`execution.py:71-74`）：
```python
"content": (
    "You are a teammate worker. Complete the assigned work item. "
    "Task recursion is forbidden."
),
```

无法根据 teammate 的 role（developer/reviewer/planner）定制行为。

**设计方案：**

1. `spawn_teammate` 的 `role` 参数映射到系统提示词
2. `TeamStore` 的 member 配置中已存储 `role` 字段
3. `ExecutionService._run_turn_executor_work` 从 member 配置中读取 `role`，选择对应的 system prompt

```python
# execution.py

_ROLE_PROMPTS = {
    "developer": "You are a developer teammate. Complete the assigned work item. ...",
    "reviewer": "You are a reviewer teammate. Review the assigned work item. ...",
    "planner": "You are a planner teammate. Plan the approach for the assigned work item. ...",
}

def _build_system_prompt(self, role: str) -> str:
    base = _ROLE_PROMPTS.get(role, _ROLE_PROMPTS["developer"])
    return f"{base}\nTask recursion is forbidden."
```

**改动文件：**
- `core/team_engine/execution.py` — `_run_turn_executor_work` 从 config 读取 role，映射 prompt
- `core/team_engine/protocol.py` — 增加 `DEFAULT_TEAMMATE_ROLES` 常量

**影响范围：** 仅影响 worker 的 system prompt 内容，不影响调用链

---

## 五、改动汇总

| 编号 | 优化项 | 优先级 | 改动文件数 | 新增文件 | 破坏性变更 |
|------|--------|--------|-----------|---------|-----------|
| 2.1 | Worker 退出感知 | P1 | 4 | 0 | 否（新增可选参数） |
| 2.2 | 消息去重持久化 | P1 | 1 | 0 | 否（export_state 新增字段） |
| 2.3 | 审批持久化 | P1 | 2 | 0 | 否（export_state approvals 格式调整） |
| 3.1 | Inbox 增量读取 | P2 | 2 | 0 | 否（新增可选参数） |
| 3.2 | 存储上限 | P2 | 2 | 0 | 否（纯新增） |
| 3.3 | 文件锁优化 | P2 | 0 | 0 | **不做改动** |
| 4.1 | Team 列表工具 | P3 | 4 | 2 | 否（新增工具） |
| 4.2 | 失败重试工具 | P3 | 4 | 2 | 否（新增工具） |
| 4.3 | Worker 健康状态 | P3 | 4 | 0 | 是（team_state 返回格式变化） |
| 4.4 | 可配置提示词 | P3 | 2 | 0 | 否（纯新增） |

**总计：** 改动 23 个文件，新增 4 个文件，1 处破坏性变更（team_state 返回值格式）

---

## 六、实施顺序

```
第 1 批（P1 健壮性）：2.1 → 2.2 → 2.3
第 2 批（P2 性能）：  3.1 → 3.2
第 3 批（P3 功能）：  4.3 → 4.4 → 4.1 → 4.2
```

每批完成后运行全部测试确认无回归。

---

## 七、P4 边界情况（不在本次范围）

| 编号 | 问题 | 简要方案 |
|------|------|---------|
| P4-1 | 孤立快照静默跳过 | import_state 收集跳过的 team 名称并 log warning |
| P4-2 | 事件缓冲区无上限 | 添加 MAX_EVENT_BUFFER=1000，超限丢弃最旧并 log |
| P4-3 | 工作项缺少优先级 | 增加 priority 字段，排序时优先处理 |
