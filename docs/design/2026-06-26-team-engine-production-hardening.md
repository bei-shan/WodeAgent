# AgentTeams 生产化加固方案

> 日期: 2026-06-26 | 状态: **P0/P1 核心加固已完成并有聚焦测试覆盖**

---

## 一、当前状态

AgentTeams 已具备可用 MVP，并完成本轮生产化加固：

1. Team 创建、删除、清理。
2. `message` / `broadcast` / shutdown request-response。
3. fanout / collect 并行 work item。
4. Task board CRUD、依赖、claim。
5. Plan approval gate。
6. Worker thread + JSONL 文件存储。
7. Message delivered/processed 状态文件级恢复。
8. Approval pending/approved/rejected/dispatched 状态文件级恢复。

核心模块：

| 模块 | 职责 |
|---|---|
| `core/team_engine/manager.py` | 团队编排入口 |
| `core/team_engine/store.py` | `.teams` 存储、JSON/JSONL 文件与锁 |
| `core/team_engine/message_router.py` | 消息发送、ACK、状态统计 |
| `core/team_engine/execution.py` | teammate work item 执行与 retry |
| `core/team_engine/turn_executor.py` | worker 单轮 LLM + tool call |
| `core/team_engine/approval.py` | plan approval 状态机 |
| `core/team_engine/task_board_store.py` | 共享任务板 |

---

## 二、本轮加固内容

### 2.1 TeamManager 生命周期单一所有权

之前 `CodeAgent.__init__` 和 `AgentTeamsFeature.init()` 都会初始化 `TeamManager`，而 `TeamManager` 构造时会启动 sweep thread，存在后台线程泄漏风险。

当前策略：

1. `CodeAgent` 只读取并记录 AgentTeams 配置。
2. `TeamManager` 只由 `AgentTeamsFeature` 初始化。
3. `CodeAgent.close()` 显式调用 `team_manager.shutdown()`。

### 2.2 Worktree root rebinding

之前 `EnterWorktree` 只更新了 `agent.project_root` 和 `context_builder.project_root`，但已注册工具实例的 `_project_root` / `_working_dir` / `_root` 仍可能指向原目录。

当前策略：

1. `CodeAgent._rebind_project_root(project_root)` 统一重绑运行时根目录。
2. enter/exit worktree 时统一更新：
   - `agent.project_root`
   - permission gate
   - 所有工具 `_project_root` / `_working_dir` / `_root`
   - `context_builder.project_root`
   - `SkillLoader`
   - `TeamManager.execution_service._project_root`
3. `TeamManager.set_project_root()` 让 worker 执行根随 active worktree 改变。

> 注意：`.teams` / `.tasks` store 当前仍绑定 TeamManager 创建时的 store root。也就是说，当前策略是“团队状态存原项目，worker 文件操作跟随 active worktree”。若后续希望每个 worktree 都有独立 team store，需要重建 TeamManager 或迁移 store。

### 2.3 MessageRouter 状态持久化

之前 `_message_status` 只在内存，崩溃或恢复后 message count / recent messages 不完整。

当前策略：

1. `TeamStore` 新增 `.teams/<team>/message_status.jsonl`。
2. `MessageRouter.send_message()` 写 delivered 状态。
3. `MessageRouter.mark_processed()` 写 processed 状态。
4. `MessageRouter.__init__()` 启动时从 store 恢复状态。
5. 聚焦测试：`test_message_status_survives_manager_recreate`。

### 2.4 ApprovalService 文件级持久化

之前 approval 主要依赖内存 + session snapshot。现在升级为文件级持久化。

当前策略：

1. `TeamStore` 新增 `.teams/<team>/approvals.json`。
2. `TeamStore.list_approval_requests()` / `upsert_approval_request()` / `clear_approval_requests()` 管理 approval 状态。
3. pending request 创建后立即持久化。
4. approved/rejected 响应后立即持久化。
5. `claim_next_approved_request()` 标记 `dispatched=True` 后立即持久化，再创建 work item，避免恢复后重复派发。
6. `TeamManager.__init__()` 从 `.teams/<team>/approvals.json` 恢复 approval 状态。
7. `import_state()` 兼容旧 session snapshot，并将旧 snapshot 中的 approvals backfill 到文件。
8. 聚焦测试：
   - `test_pending_approval_is_persisted_and_restored`
   - `test_approved_dispatched_approval_is_restored_and_not_dispatched_twice`
   - `test_import_state_backfills_approval_file`

### 2.5 Worker LLM retry 加固

之前 worker retry 只匹配 `rate limit` / `429` / `timeout` 字符串，且 retry 次数硬编码。

当前策略：

1. 新增环境变量：
   - `TEAM_LLM_MAX_RETRIES`，默认 `2`
   - `TEAM_LLM_RETRY_BACKOFF`，默认 `0.2`
2. retryable 判断扩展为：
   - 异常类名包含 RateLimit / Timeout / Connection / APIError
   - `status_code` 属于 408/409/425/429/5xx
   - 兼容旧字符串匹配
3. backoff 加入 10% jitter。
4. sleep 在 semaphore context 外发生，避免 backoff 期间占用 LLM 并发槽。
5. worker 多轮执行最终空响应时显式失败，避免把空字符串标成成功结果。
6. 聚焦测试：
   - `test_worker_retries_retryable_exception_then_succeeds`
   - `test_worker_non_retryable_exception_fails_fast`
   - `test_worker_releases_semaphore_before_backoff`

### 2.6 TeamRetry 状态校验

之前 `TeamRetry` 可以把任意 work item 置回 queued。

当前策略：

1. 只允许 retry `failed` / `canceled`。
2. 对 `queued` / `running` / `succeeded` 返回 `CONFLICT`。
3. 聚焦测试：
   - `test_retry_accepts_failed_or_canceled`
   - `test_retry_rejects_non_failed_states`
   - `test_retry_unknown_work_id_returns_not_found`

---

## 三、仍待生产化的 P2 项

### P2：Worktree + Team 更强隔离策略

当前已保证 worker 执行根跟随 active worktree，但 team store 仍固定在 TeamManager 创建目录。后续可选增强：

1. worktree 内独立 `.teams/.tasks` store。
2. enter/exit worktree 时停止或迁移 active workers。
3. 补充真实 `CodeAgent` 级 worktree rebinding 集成测试。

### P2：Manager 职责继续拆分

`TeamManager` 仍承担 worker loop、approval dispatch、sweep、state import/export、tmux lifecycle 等多重职责。后续建议拆分：

1. `WorkerLoopService`
2. `TeamStateSnapshotService`
3. `TeamDisplayService` / tmux adapter

### P2：更完整可观测性

1. retry attempt 写入 work item metadata 或 event。
2. approval 状态变化产生结构化 event。
3. message status 文件损坏时提供 warning，而非静默忽略。

---

## 四、当前验收口径

当前可以称为：

- **并行执行 MVP：已达标**
- **Team Engine P0/P1 核心生产化加固：已完成聚焦测试覆盖**
- **完整生产级：仍需 P2 隔离策略、职责拆分、可观测性增强**

---

## 五、已验证命令

```bash
python -m compileall core/llm.py agents/codeAgent.py core/team_engine/store.py core/team_engine/approval.py core/team_engine/manager.py core/team_engine/message_router.py core/team_engine/execution.py scripts/chat_test_agent.py
```

```bash
python -m pytest tests/test_llm_streaming.py tests/test_llm_temperature_policy.py tests/test_llm_provider_resolution.py tests/test_team_approval_persistence.py tests/test_team_plan_approval_gate.py tests/test_team_approval_tools.py tests/test_team_message_protocol.py tests/test_worktree_tools.py tests/test_team_retry.py tests/test_team_worker_retry.py -q
```

结果：`50 passed`。
