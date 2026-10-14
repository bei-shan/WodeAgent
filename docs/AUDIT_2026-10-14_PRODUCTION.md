# Ridge Code — 生产化缺口审计（2026-10-14）

> 对象：`bei-shan/WodeAgent`（旧称 CodeAgent，产品名 Ridge Code）。
> 基线：`main@34453c5`，审计基于 GitHub 当前源码、测试、工作流和 [v0.1 验收清单](releases/v0.1-readiness.md)。
> **结论**：可作为持续开发的单人本地 Agent 候选；**不能把当前实现或绿色 CI 宣传成已完成生产验收的多租户 Agent**。

## 对标原则与参考

- [OpenCode](https://github.com/anomalyco/opencode)：按工作会话隔离状态、权限规则、模型工具调用策略；把多工具执行与事件发布分开。
- [DeerFlow 2.0 架构](https://github.com/bytedance/deer-flow/blob/main/docs/ARCHITECTURE.md)：Harness → App 单向依赖、每线程沙箱/工作区、子 Agent 任务生命周期、MCP/Skills 中间件和 Gateway 信任边界。
- [claude-code-best/claude-code](https://github.com/claude-code-best/claude-code)：借鉴可观测性、Hook/权限与工程验收方式；**不是**直接复制未审核代码或声称兼容其内部协议。

这里的“对标”是架构思想和验收标准，不追求一比一照搬。

## 逐层核查

| 能力 | 仓库可核实的基础 | 仍未完成 / 不应夸大的边界 | 生产验收方向 |
| --- | --- | --- | --- |
| Agent 主循环 | `core/runtime/agent_loop.py`、`turn_lifecycle.py`、`agents/codeAgent.py` 分层 | 主 Agent 依然承担大量服务胶水；异常/重启复杂度高 | 失败注入、重启后幂等与端到端真实提供商 |
| 工具并发 | `tool_scheduler.py` 仅并发相邻的安全调用；危险工具串行屏障；三阶段发布 | 安全声明由 Registry/工具实际定义；错误/取消组合要持续验证 | 读写交错竞态、取消、超时、输出顺序回归 |
| 上下文工程 | `core/context_engine/` 历史、截断、分支压缩与恢复测试 | 尚缺统一的真实任务质量评测集、性能基线与大上下文压力报告 | 不同模型、多轮复杂任务、召回/截断质量评价 |
| 沙箱 | `DockerCommandProvider` 隔离 Bash；Docker 模式禁止宿主工具旁路 | Docker 为**可选实验性**；宿主插件未隔离、无完整容器租约/崩溃残留回收；工作区仍 RW 挂载 | 真实 Docker 逃逸/路径测试、终止与残留清理、文件最小权限 |
| MCP/OAuth | HTTP/stdio、OAuth PKCE、SDK v1/v2 兼容、schema 验证与超时处理 | 真实 OAuth 提供商刷新/撤销、非幂等远端操作无法回滚 | 断流、超时、刷新、并发隔离、审计日志 |
| 插件/Skills | 安装包校验、版本固定/回滚、代码信任内容指纹、按需 Skills | Python 插件在宿主进程运行；“允许”不等于强隔离 | 进程隔离、能力声明、签名/可信发布源、更新回滚演练 |
| 会话/传输 | `SessionController`、独立事件订阅、epoch-aware SSE 重放 | 事件 journal 仅驻内存；重启后以持久历史为准，不是完整消息流恢复 | 重连游标过期/进程重启，长会话、多 UI 并发 |
| 追踪 | 可选 OTel/Langfuse，默认关闭，采集字段收敛 | 模型输出质量没有天然评分；远端真实出口和数据保护需运营验收 | Span 层级、字段白名单、导出背压和丢弃率 |
| 服务边界 | 单用户 Web 本地 Token，应用服务层初步抽离 | 无多租户 RBAC/租户配额；大路由和前端状态文件仍偏大 | 鉴权、Origin/CSRF、文件安全、部署端口检查 |
| 发布工程 | Python 全回归、Web/PTY E2E、Docker CI、MCP 兼容、离线 soak | 仍缺真实模型/MCP/30m 连接、跨平台和运维回滚签字记录 | 不通过签字不打 v0.1 正式标签 |

## 本轮已开始落地的 P0 修复

Docker Bash 原先通过 `subprocess.run(timeout=...)` 阻塞：用户 Stop 无法中断当前 CLI，只能等其超时。修复分支将 `current_cancel_event()` 从沙箱 Bash 传到 Docker 执行器；默认进程路径复用 `run_process` 的进程组终止，在中断/超时后再执行 **`docker rm --force`**；结果协议返回 `INTERRUPTED`，不会被误判为成功。增加 fake-runner 单元回归和实际 Docker 集成测试，以及 CPU/超时非有限数拒绝。

**安全约束**：`docker rm --force` 是尽力清理，daemon 不可达、宿主崩溃时仍可能留下容器；这不等于事务回滚，也不等于 Docker 模式已达到完整隔离等级。

## 追加落地：交互式文件授权作用域

本轮后续安全复核确认：之前 `PermissionGate` 的交互式授权仅按 canonical path 缓存，`Read` 许可可能被 `Write/Edit/MultiEdit` 复用。这属于权限提升风险，而非单纯代码风格问题。已将交互许可缓存细分为 `read` 和 `write` 能力，保留受信任调用方显式 `grant(path)` 的旧通配授权以维护兼容，增加真实 Read→Write 阻断测试。参见 [权限作用域设计](design/2026-10-14-operation-scoped-permissions.md)。

## 有顺序的后续 Backlog

### P0：v0.1 单用户发布闸门（必须完成）

1. **真实提供商验收**：30 分钟以上工具混合会话，实际 MCP 服务器和 OAuth 刷新/撤销，保留仅含脱敏指标的报告（无密钥、无用户原文）。
2. **终止与恢复**：断网、无响应提供商、取消、重启、SSE 断连后状态一致性；确保远端写操作不被盲目重试。
3. **Docker 清理保障**：为异常退出残留容器引入受限 namespace / label、TTL 与启动时清理；独立检查隔离/文件边界；验证进程取消链路。
4. **发布证据**：记录 SHA、CI Run URLs、跨平台和手工验收、回滚/备份演练、artifact 校验和责任人，不满足则不发布正式标签。

### P1：生产工程质量

5. 用结构化、可重放的 Agent 测试任务集建立成功率、token/时延/成本、工具权限拒绝率与上下文质量基线；质量与遥测指标分离。
6. 提炼 Harness/App 单向依赖 CI 断言（参考 DeerFlow）；拆分 `codeAgent.py`、`desktop/service/app.py`、`App.tsx`、`TeamManager` 时坚持小 PR 与回归保障。
7. 插件 Python 代码进程外隔离；细分只读/写入/网络/执行能力授权，防止“读权限”无意变成“写权限”。
8. 发布兼容策略：会话/工具事件协议版本、旧 `.mycodeagent/` 备份迁移、回滚和数据库/JSONL 崩溃恢复测试。

### P2：从本地单用户迈向托管生产（另立产品目标）

9. 多租户认证与资源隔离、任务额度、审计、速率限制、密钥托管、持久任务调度与横向伸缩。
10. 企业级插件供应链、权限策略管理、沙箱强制执行、多副本任务租约与 HA/灾备。

## 完成定义

- “代码已实现”需要源码 + 独立测试；“CI 已通过”需要关联的确切提交 SHA 和通过的 Actions URL。
- “生产就绪”额外要求真实运营环境接受测试和签字，不能用 mock 或普通单元测试替代。
- 文档中的旧称、内部兼容路径与设计稿不代表可以不经迁移直接删改。
