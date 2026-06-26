# AgentTeams 功能设计文档（MVP v2 实现对齐版）

> 最后更新：2026-06-26
> v3 变更：补充生产化状态，TeamManager 生命周期单一所有权、worktree root rebinding、message status JSONL 持久化、approval JSON 文件级持久化、worker LLM retry 加固、TeamRetry 状态校验。详见 `docs/design/2026-06-26-team-engine-production-hardening.md`。

## 1. 目标与范围

本设计文档描述 MyCodeAgent 当前 **AgentTeams MVP** 的真实实现状态，用于开发、测试与回归。

MVP 目标：
1. 在 `CodeAgent + ToolRegistry + TaskTool` 架构上提供可用的会话团队能力。
2. 支持团队创建、消息通信、并行分工执行、状态观测和清理。
3. 保持 `Task(mode=oneshot)` 兼容，不破坏原有一次性子代理路径。
4. 通过 feature flag 可快速关闭与回滚。

MVP v3 生产化加固（2026-06-26）：
1. `TeamManager` 改为由 `AgentTeamsFeature` 单一初始化，避免重复 sweep thread。
2. `CodeAgent.close()` 显式关闭 `TeamManager`。
3. worktree enter/exit 会重绑定路径型工具、permission gate、SkillLoader、ContextBuilder 与 worker 执行根。
4. MessageRouter 的 delivered/processed 状态写入 `.teams/<team>/message_status.jsonl`，启动时恢复。
5. ApprovalService 的 pending/approved/rejected/dispatched 状态写入 `.teams/<team>/approvals.json`，启动时恢复。
6. Worker LLM retry 改为异常类/status_code/字符串综合判断，并支持 `TEAM_LLM_MAX_RETRIES`、`TEAM_LLM_RETRY_BACKOFF`。
7. `TeamRetry` 只允许重试 failed/canceled work item。

MVP 非目标：
1. 不做完整的多窗格交互 UI（如 Shift+Up/Down 焦点切换）。
2. 不做每个 teammate 的完整独立会话历史恢复。
3. 不做分布式调度、复杂事务、复杂重试编排。
4. 不拆分 TaskTool 语义层（`Task` + `TeamSpawn`）。

## 2. 系统架构

核心模块位于 `core/team_engine/`：
1. `manager.py`：团队编排入口，管理消息、任务板、审批、worker 生命周期。
2. `store.py`：团队配置、inbox、work item 的文件化持久化与锁。
3. `task_board_store.py`：共享任务板（依赖、claim、状态更新）。
4. `message_router.py`：消息协议与 ACK 状态推进。
5. `execution.py`：teammate 工作执行服务（复用 TurnExecutor）。
6. `turn_executor.py`：单轮 LLM + tool call 执行内核。
7. `supervisor.py` + `worker.py`：worker 线程生命周期管理。
8. `approval.py`：计划审批状态机。

`CodeAgent` 挂载点：
1. 根据 `enable_agent_teams` 初始化 `TeamManager`。
2. 注册 Team 系列工具。
3. 每轮 ReAct 前注入 runtime system block（不污染 user 轮次）。
4. `save_session/load_session` 调用 team state 导出/恢复。

## 3. 功能工具面

已注册工具（15个）：
1. `TeamCreate`
2. `SendMessage`
3. `TeamStatus`
4. `TeamDelete`
5. `TeamCleanup`
6. `TeamFanout`
7. `TeamCollect`
8. `TeamTaskCreate`
9. `TeamTaskGet`
10. `TeamTaskUpdate`
11. `TeamTaskList`
12. `TeamApprovals`
13. `TeamApprovePlan`
14. `TeamList`（v2 新增）
15. `TeamRetry`（v2 新增）

Task 相关模式：
1. `Task(mode=oneshot)`：保持原行为（默认）。
2. `Task(mode=persistent)`：创建持久 teammate。
3. `Task(mode=parallel)`：快捷并行分发（底层走 fanout）。

## 4. 协议与状态机

### 4.1 消息类型

支持消息类型：
1. `message`
2. `broadcast`
3. `shutdown_request`
4. `shutdown_response`
5. `plan_approval_response`

### 4.2 ACK 状态

消息 ACK 状态：
1. `pending`
2. `delivered`
3. `processed`

### 4.3 work item 状态

work item 状态：
1. `queued`
2. `running`
3. `succeeded`
4. `failed`
5. `canceled`

### 4.4 执行语义（MVP 实现）

当前语义：
1. `message/broadcast` 不仅 ACK，会进入 teammate 的执行语义（自动入 work item 并执行）。
2. `shutdown_request` 收到后会回发 `shutdown_response`，并携带同一 `request_id`，再执行停止流程。
3. `plan_approval_response` 与审批请求关联，审批通过后才会派发对应工作项。

## 5. 存储模型

默认目录：
1. `.teams/`
2. `.tasks/`

团队配置：
1. `.teams/<team>/config.json`
2. `members` 中保留 `name/role/tool_policy`

消息状态索引：
1. `.teams/<team>/message_status.jsonl`
2. 保存 delivered/processed 最新状态，用于重启后恢复 message counts 与 recent messages。

审批状态存储：
1. `.teams/<team>/approvals.json`
2. 保存 pending/approved/rejected/dispatched 全状态，用于崩溃或重启后恢复 plan approval gate。

并行工作项存储：
1. `.teams/<team>/work_items/work_items_<teammate>.jsonl`
2. 每个 teammate 独立分片，降低锁竞争。

任务板存储：
1. `.tasks/<team>/task_<id>.json`
2. `_meta.json` 维护 task id 递增。

## 6. 并发与一致性

并发策略：
1. 单 worker 线程内串行执行（`max_concurrency=1`）。
2. 多 worker 线程并行。
3. LLM 调用通过全局 semaphore 做并发闸门。

锁策略：
1. 使用目录锁（`mkdir` 原子创建）。
2. 支持 `timeout + stale reclaim`。
3. 任务板 claim 在锁内执行，防重复认领。

关键修复约束：
1. `create_team` 不再默认拉起所有 worker，避免干扰纯 claim 测试与竞态。
2. worker 在 fanout/message 等触发点按需启动。

## 7. 安全与权限

基础权限策略：
1. teammate 成员 `tool_policy` 必含 `role` 与 `tool_policy` 结构。
2. teammate 路径中 `Task` 永久禁止（denylist + 执行层双重过滤）。
3. 支持 delegate mode 下的工具调用限制（lead 只做编排）。

## 8. 运行时注入与会话恢复

运行时注入：
1. team runtime 摘要通过 system block 注入。
2. 不写入 user 轮次，不破坏历史压缩边界。

会话持久化：
1. `save_session` 导出 `teams_snapshot` 与并行工作索引。
2. `load_session` 触发 `import_state` 恢复团队状态。
3. 恢复时避免重复拉起同名 worker。
4. 恢复时会将 running work requeue 为 queued。

## 9. 测试与验收口径

MVP 验收重点：
1. Team 工具协议返回统一（success/error 形状一致）。
2. `message/broadcast` 可触发 teammate 执行。
3. `shutdown_request/response` request_id 关联可验证。
4. 并行执行有效（多 worker 并行，oneshot 兼容）。
5. claim 竞争场景无重复认领。
6. 会话恢复不重复拉起 worker。

## 10. 已知限制（MVP 保留）

1. 终端交互暂不支持完整 teammate 焦点切换 UI（键盘导航体验未复刻）。
2. in-process teammate 不是完整独立会话恢复（当前为状态恢复）。
3. shutdown 协议是可用基线，未扩展复杂拒绝/协商状态机。
4. TaskTool 语义分层未拆分（`Task` + `TeamSpawn`），Task 持续承载 oneshot/persistent/parallel 三种模式。
5. Worktree 下 team store 仍固定在 TeamManager 创建时目录；当前只保证 worker 执行根随 active worktree 变化。

### v3 已解决的限制

- ApprovalService 文件级持久化不足 → 已通过 `.teams/<team>/approvals.json` 保存 pending/approved/rejected/dispatched 全状态

### v2 已解决的限制

- Worker 空闲退出无感知 → 已通过 `on_stop` 回调解决
- 消息去重不持久化 → delivered/processed 状态已写入 `message_status.jsonl`，processed/cursor 仍进入 session snapshot
- 审批请求重启丢失 → 已通过 `.teams/<team>/approvals.json` 文件级持久化解决
- Inbox 全量读取 → 已通过 `_inbox_cursor` 增量读取解决
- 存储文件无限增长 → 已通过 `_trim_jsonl` 和配置上限解决
- Worker 健康状态不透明 → 已通过 `team_state` 详细统计解决
- TurnExecutor 系统提示词硬编码 → 已通过角色映射解决
- 缺少 TeamList 工具 → 已新增
- 缺少失败重试工具 → 已新增 `TeamRetry`

## 10b. 验收基线（Claude Code Teams 复刻完成度）

满足以下 10 条即可认为核心复刻完成：

| # | 验收项 | 状态 |
|---|--------|------|
| 1 | 支持 message + broadcast | ✅ |
| 2 | shutdown request/response 带 request_id | ✅ |
| 3 | plan approval 可阻塞执行 | ✅ |
| 4 | 共享任务看板 CRUD 完整 | ✅ |
| 5 | 任务依赖可阻塞/解锁 | ✅ |
| 6 | 多 teammate 并发 claim 无重复领取 | ✅ |
| 7 | teammate 空闲后可自动认领新任务 | ✅ |
| 8 | runtime 能显示任务/消息/审批摘要 | ✅ |
| 9 | cleanup 遵循"先关闭成员再清理" | ✅ |
| 10 | save/load 后团队状态可继续推进 | ✅ |

## 11. 配置项

关键环境变量：
1. `ENABLE_AGENT_TEAMS`（总开关）
2. `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`（兼容开关）
3. `AGENT_TEAMS_STORE_DIR`（默认 `.teams`）
4. `AGENT_TASKS_STORE_DIR`（默认 `.tasks`）
5. `TEAMMATE_MODE`（`auto|in-process|tmux`）
6. `TEAM_DELEGATE_MODE`
7. `TEAM_LLM_MAX_CONCURRENCY`（默认 `4`）
8. `TEAM_WORKER_MAX_STEPS`（默认 `8`）
9. `TEAM_MAX_INBOX_SIZE`（默认 `10000`，v2 新增）
10. `TEAM_MAX_WORK_ITEMS`（默认 `5000`，v2 新增）
11. `TEAM_LLM_MAX_RETRIES`（默认 `2`，v3 新增）
12. `TEAM_LLM_RETRY_BACKOFF`（默认 `0.2`，v3 新增）

---

该文档为当前代码实现对齐版本。若后续进入非 MVP 阶段（完整交互 UI、会话级恢复、多进程隔离），需在此文档上继续版本化扩展。
