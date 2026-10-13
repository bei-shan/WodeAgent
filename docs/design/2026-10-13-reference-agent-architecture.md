# WodeAgent 核心架构参考与分阶段优化（2026-10-13）

> 参考项目：第三方工程 [Claude Code Best](https://github.com/claude-code-best/claude-code)（`main`，**不是 Anthropic 官方源码**）与 [OpenCode](https://github.com/anomalyco/opencode)（`dev`）。
> 本文件仅对比经过源码核验的设计思想；不复制第三方项目的实现代码，不假定两个项目与当前版本 WodeAgent 具有相同技术栈或授权方式。

## 1. 源码依据

| 关注点 | 参考文件 | 有价值的设计思想 |
| --- | --- | --- |
| 会话事件 | [OpenCode Bus](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/bus.ts) | 易丢失的实时订阅与有序可重放日志区分；每个聚合 ID 上单调递增的序号和 `after` 游标 |
| 事件传输 | [OpenCode EventFeed](https://github.com/anomalyco/opencode/blob/dev/packages/server/src/event-feed.ts) | 每订阅者独立队列；背压/溢出有明确失败语义 |
| LLM 与工具分层 | [OpenCode ToolRuntime](https://github.com/anomalyco/opencode/blob/dev/packages/ai/src/tool-runtime.ts) | 工具执行单元与模型 IO/后续运行分离，结果统一封装 |
| Agent Loop | [Claude Code Best query](https://github.com/claude-code-best/claude-code/blob/main/src/query.ts) | 明确的结束原因，模型出错重试、上下文压缩后的受控恢复 |
| 生命周期 | [Claude Code Best QueryEngine](https://github.com/claude-code-best/claude-code/blob/main/src/QueryEngine.ts) | 会话持续状态与一次执行的生命周期分层 |
| 权限 | [Claude Code Best Tool](https://github.com/claude-code-best/claude-code/blob/main/src/Tool.ts) | 工具输入校验先于权限决策，并将工具活动、权限交互接入运行上下文 |

## 2. 我们的现状

- `agents/codeAgent.py` 已实现多步 ReAct、工具执行、HistoryManager、压缩、停止生成和结构化事件，没必要推倒重建。
- `core/features/` 已拆分 MCP、Plan、Teams、Session 等能力，适合继续模块化演进。
- `core/runtime/session_controller.py` 负责线程隔离、消息入口、权限和 Web 问答；原先只有一个共享 `queue.Queue`。
- `desktop/service/app.py` WebSocket 原先从这个共享队列 `get`：两名客户端会分摊而不是同时收到事件；断网后事件可能丢失，重连依赖历史快照回退。
- `desktop/web/src/api.ts` 有 WebSocket 重连，但没有事件游标、重放及去重协议。

## 3. 第一阶段：执行事件链路可靠性（本分支实现）

这不是照搬 OpenCode 的持久化 EventStore，而是按我们的 Python/FastAPI 实际情况实现**有界的内存事件日志**：

1. 每个会话分配独立、单调递增的序号，线程安全发布事件。
2. `read_after(seq)` 与 `wait_after(seq)` 支持游标追赶；多个消费者不互相消费。
3. 每会话最多保留 2,048 条事件，避免 Token 流无限累积；过期游标返回明确的 `stream.reset`。
4. WebSocket 支持 `?after=N`，先重放再跟随新事件，没有 “订阅前后空窗”。
5. 浏览器保存最后成功处理的序号，自动重连时带游标；重复序号不重复追加 Token。
6. 新会话第一次连接从 0 重放，防止首条消息产生时 WS 仍在连接而丢流。
7. 已有会话仍由磁盘历史承担跨进程恢复；连接时恢复仍在等待的 Permission / AskUser 对话框。
8. 增加并发订阅、缓存淘汰、WebSocket 重连及浏览器去重的回归测试。

**明确限制**：日志不落盘。服务进程重启后，只有历史快照可恢复，Token 增量与工具瞬时活动不可从旧进程回放；日志过期时 Web 会提示并加载快照。当前没有正式的多用户身份隔离，也不是公网部署方案。

## 4. 第二阶段（后续独立 PR，按优先级推进）

| 优先级 | 改造 | 验收条件 |
| --- | --- | --- |
| P0 | 统一 Turn 终态（completed / failed / cancelled）并记录可观测失败原因 | 后端、前端、持久化与中断返回同一终态；失败不被误报为成功 |
| P0 | Agent 执行错误分类：可重试网络错误、速率限制、上下文超限、工具错误 | 有边界的重试与退避；工具副作用不重放；取消中断优先 |
| P1 | 上下文策略化压缩与压缩事件 | 预算、压缩触发、保留关键目标及中断后的恢复测试 |
| P1 | 工具权限分层与执行前校验 | 只读、安全写入、危险操作有可审计决策，工具参数先校验 |
| P1 | EventStore v2：将关键终态和操作写盘，区分 durable/ephemeral | 进程重启后有序恢复关键事件，按游标去重 |
| P2 | Agent Teams 任务状态机与子代理结果统一回传 | 多 Agent 正确归并、失败和中断无孤儿任务 |
| P2 | OpenTelemetry / Trace 中转（可选） | 一条用户请求有清晰 run_id/turn_id/span，敏感信息脱敏 |

各阶段仅借鉴上游的接口边界、数据结构和失败处理思路；**不按源码文件照搬**，每阶段独立验证后合并。
