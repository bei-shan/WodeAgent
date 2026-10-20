# Ridge Code：参考实现差距审计与执行路线图（2026-10-20）

> **范围与判断口径**：基于当日仓库 `main` 源码目录、既有安全/发布门禁、以及公开参考项目的当前架构。不是根据 README “有这个功能”就标记完成。验收须在对应 SHA 上有可追溯的成功测试；需要凭证、真实硬件或在线平台的门禁不得以 Mock、跳过或默认允许冒充 PASS。历史 `docs/design` 不代表当前主干。借鉴接口、边界、失败语义，不复制上游代码。

## 对照的上游（以源码和官方文档为准）

| 参考 | 主要借鉴 | 依据 |
| --- | --- | --- |
| [OpenCode](https://github.com/anomalyco/opencode) (`dev`) | 显式 Session/Job 生命周期、后台任务恢复与消费状态；工具及权限隔离；完整请求预算 | [Job service](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/job.ts)、[Session v2 设计](https://github.com/anomalyco/opencode/blob/dev/specs/v2/session.md) |
| [DeerFlow 2.0](https://github.com/bytedance/deer-flow) (`main`) | 长任务 Harness（文件系统/Skills/子代理/压缩/工件）、沙箱授权、检查点与工件归属 | [Core concepts](https://github.com/bytedance/deer-flow/blob/main/frontend/src/content/en/introduction/core-concepts.mdx)、[Sandbox authz](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/authz/sandbox_authz.py)、[Artifact middleware](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/agents/middlewares/artifact_capture_middleware.py) |
| [Claude Code Best（第三方）](https://github.com/claude-code-best/claude-code) (`main`) | Bash/PowerShell 命令安全分析、分层授权、Plan/执行状态；不是 Anthropic 官方源码 | [Bash security](https://github.com/claude-code-best/claude-code/blob/main/packages/builtin-tools/src/tools/BashTool/bashSecurity.ts)、[Permission design](https://github.com/claude-code-best/claude-code/blob/main/docs/safety/permission-model.mdx) |
| [browser-use](https://github.com/browser-use/browser-use) (`main`) | CDP session ownership，导航前/后 URL 策略（含重定向、标签页），DOM/watchdog，下载与隐私边界 | [Security watchdog](https://github.com/browser-use/browser-use/blob/main/browser_use/browser/watchdogs/security_watchdog.py)、[Session manager](https://github.com/browser-use/browser-use/blob/main/browser_use/browser/session_manager.py) |

对照的重点不是功能名称数量，而是 **授权是否落在不可由模型绕过的执行边界、结果是否可恢复、是否能在实际平台验收**。

## 主干能力审计（证据和未完成点）

| 模块 | 已有基础（主干证据） | 尚缺 / 风险 | 优先级 |
| --- | --- | --- | --- |
| 工具执行及审批 | `core/runtime/tool_runtime.py`、`tools/registry.py`、`tools/permission_gate.py`，本地 Bash 可要求逐命令审批 | 要对 MCP/插件/子代理/跨工具调用建立同一不可升级的主体与授权记录；黑名单和 prompt 规则不等价于系统隔离 | P0 |
| 运行时进程回收 | `core/runtime/process_execution.py` POSIX process group、超时和取消、有限管道捕获 | **Windows 缺 Job Object 子进程树所有权**；Windows 创建到分配之间的竞态；进程独立于宿主系统资源的强隔离仍缺 | P0 |
| 浏览器与电脑控制 | Web UI Playwright E2E 已有，但它测试的是前端 | 主干未见原生 Agent Browser/Computer Use 工具/会话拥有者；多标签页、导航和子资源规则、下载、截图/文件权限、OS 专用实现尚无真实端到端证据 | P0 |
| Docker/BoxLite/E2B | `core/sandbox/` 多后端、真实 Docker CI；云 VM/BoxLite opt-in | 可变宿主读写挂载导致 TOCTOU 和秘密文件竞态；强制网络出站策略、镜像/SDK 固定摘要、真实 VM 检查未完成 | P0 |
| 文件、上传与产物 | `core/sandbox/guest_files.py`、E2B artifacts；部分 guest 文件测试 | 无端到端的“租户/会话/工具调用 → 文件/下载/截图”独占授权及生命周期（本地和 hosted 要区别）；MIME/大小/路径/下载来源要绑定到资源主体 | P0 |
| Agent 上下文 | `core/context_engine`，压缩、工具结果帧、静态可信 L1、预算中间件 | 需要实际多轮长时执行、模型限额、截图/附件 token 计量、摘要质量的回放评估和丢失数据恢复证据 | P1 |
| 会话与后台任务 | `core/runtime/event_journal.py`、SSE、`core/team_engine`，持久化队列原语 | 主 Agent 执行非跨进程可恢复；worker 领取/撤销/心跳/副作用与 SSE outbox 闭环尚未串成真实 hosted 执行链路 | P0 |
| Skills/MCP/插件 | `core/skills`、`core/plugin_loader.py`、`tools/mcp`，测试覆盖输入校验及 OAuth | 来源/版本固定和供应链签名、插件限权升级、MCP 非可信描述/响应的跨会话对抗评估不足 | P1 |
| 用户隔离/权限 | hosted OIDC + PostgreSQL RLS 的 metadata 服务、真实 Postgres/Redis CI | 未贯穿真实 Agent、沙箱、MCP、内存、附件和所有 WS/SSE；不能宣传已达生产多租户 | P0 |
| 记忆 | `core/memory` SQLite/Mem0 抽象及配置测试 | OpenViking/Tencent/OpenMemory 等仍须独立 SDK 兼容测试，跨租户保留/撤销/删除和用户明确选择 | P2 |
| 可观测与发布 | OpenTelemetry/Trace SDK 测试，四条真实 CI 集成（PR #127） | 线上对抗集、24h/100+ principals 压测、真实备份恢复、事故回滚、依赖审计、固定哈希、branch required checks | P1/P2 |

## 交付批次（按依赖顺序，多个可并行设计但分别验收）

### 第一批 A：Windows 进程树执行边界（本 PR 实施）

- [x] 增加 Windows Job Object（`KILL_ON_JOB_CLOSE`），创建/配置/分配失败拒绝启动或拒绝未受控执行。
- [x] 正常退出、取消和超时均关闭 Job Object，不允许仍继承 Job 的子进程留存；保留 POSIX 行为。
- [x] 新增 Windows 实机 CI，验证父进程、孙进程取消、超时、正常结束后的回收，以及 Job Object 不可用时 fail closed。
- [ ] CI 验证后在此文档记录具体 SHA/PR/Windows job 证据。
- [ ] **后续加强**：消除 Popen 后分配 Job 的窗口，采用经 Windows 版本验收的原子进程创建到 Job（STARTUPINFOEX Job List）方案；校验 nested Job、子进程 breakaway、杀进程失败和句柄泄漏。当前 Job 不是进程或网络沙箱。

**验收**：Windows runner 真机执行，无跳过，子进程无法在取消/超时/正常结束后留下标记文件；Linux 完整回归/真实服务 CI 维持全绿。

### 第二批 B：受限 browser/computer use（开发优先级 P0）

1. 定义 `BrowserSessionOwner`：`tenant/user/session/turn`（本地 trusted 可单主体），CDP profile、tabs、cookies、录像、下载与文件路径按主体隔离；明确禁止复用跨用户浏览器。
2. 封装可选 backend（Playwright/CDP，不能借 Web UI E2E 代替）。所有导航、点击后的重定向、弹窗/新标签页、XHR/fetch 和下载需 **浏览器进程侧** 网路访问控制和 URL 规则；防 scheme 及 localhost/metadata/private IP/DNS rebinding，并在 host/VM/网络层验证；失败关停 session。
3. 输入作为事件而非未审查脚本；操作分类 `read/navigate/type/click/download/upload/submit`。所有会对外发送或改变状态的操作进入每动作授权；截图、附件作为带主体/过期/大小上限的非文本产物，模型接收非可信内容帧。
4. Computer Use 拆成 `Windows/Linux/macOS` 适配器；屏幕/窗口/键鼠操作只作用于用户授权会话，支持取消、可见审计、权限回收，不提供默认主机桌面控制。
5. 实机 Playwright / 浏览器测试：重定向到 localhost、弹出新标签页、下载路径越权、截图跨会话、手动审批拒绝、任务中断清理；OS 测试矩阵 Windows/macOS/Linux。

**验收**：所有高风险动作无法以工具 metadata 或模型提示绕过权限；真实沙箱内运行、关键攻击场景 0 越权，Web UI 能展示安全的进度和产物。

### 第三批 C：沙箱工作区与工件安全（P0）

- 将整工作区直接 RW bind mount 改为 **逐会话生成快照 + 明确回写**，敏感文件绝不进入 guest；fd/symlink、换名和并发攻击模拟实机验收。
- 所有工件（上传、下载、截图、长工具结果）绑定 owner、会话、资源路径和 TTL，存储层服务端校验，提供清理/删除，拒绝任意路径和跨主体访问。
- 实际 Docker/E2B/BoxLite 检查网络断开、目的地址 allowlist、负载配额及取消，BoxLite 受控 KVM Runner 与 E2B 授权环境以 BLOCKED 表示无证据。

**验收**：有针对 workspace race、artifact IDOR、网络绕过和存储消耗的回归与真实沙箱实测报告。

### 第四批 D：持久执行 + 多租户闭环（P0/P1）

- 统一 Principal/AuthorizationProvider 到工具、模型、沙箱、浏览器、MCP、内存和 SSE，使用受信服务上下文，默认 deny。
- 将现有 Postgres queue/Redis admission 嵌入 hosted Agent worker：获取 claim → fenced exec → 续约 → outbox → SSE cursor → 幂等恢复；所有副作用调用标识不可重放。
- 宕机/网络分区/取消/租户成员撤销真实集成测试，并在 100+ 租户负载条件下验证隔离。

**验收**：多进程 worker 中断恢复无重复执行、跨租户 IDOR 0 泄漏、权限撤销在工具边界立即生效；跑完真实 staging 后才可改变 hosted NO-GO。

### 第五批 E：上下文/扩展/记忆/发布（P1/P2）

- 模型限额/附件/工具 schema 的完整 request 预算，压缩时结构化任务状态与引用 id，跨多次压缩和 fork 实际长任务 eval。
- Skills/MCP/插件来源与版本固定、可禁用和最小权限的安装/升级边界；真实恶意 MCP/网页/仓库注入攻击集。
- 每租户可选择记忆后端，支持撤销、删除、保留政策和 SDK 故障 fail closed。
- 固定 CI required checks、依赖/镜像摘要、签名发布；部署级备份恢复、24 小时 soak、压测、回滚以及审核签字。

**验收**：发布报告必须附 SHA-bound CI、真实外部攻击测试、服务端授权追踪和人工 sign-off。缺一项仍保持生产 NO-GO。

## 禁止的“假完成”

- Mock browser 测试绿 ≠ Agent Browser/Computer Use 已上线。
- GitHub Actions 部分任务 skip ≠ 真实 BoxLite/E2B/多租户验收通过。
- 进程组/Job Object ≠ 无宿主逃逸、无网络或无凭证访问。
- 正则关键词命令黑名单 ≠ 完备 shell 解析安全策略。
- 仅有 Postgres/Redis 组件测试 ≠ 执行链路可生产多租户。
- 文档或设计计划 ≠ 代码已交付；每个阶段在 `docs/architecture/modernization-gates.md` 和 release gates 留下证据。
