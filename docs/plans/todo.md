# MyCodeAgent Roadmap

> **最后更新**：2026-06-29（晚间 — P1 §1-5 全部清零）
> **本次重写依据**：基于跨项目对比（Hermes / Pi / Kode）+ 项目完整度审计的综合产物
> **关联文档**：
> - 设计反思：[docs/design/2026-06-29-feature-system-reflections.md](../design/2026-06-29-feature-system-reflections.md)
> - Team Engine 硬化路线：[docs/design/2026-06-26-team-engine-production-hardening.md](../design/2026-06-26-team-engine-production-hardening.md)
> - LLM Streaming 设计：[docs/design/2026-06-26-llm-streaming-design.md](../design/2026-06-26-llm-streaming-design.md)
> - Web/桌面端总览：[docs/design/2026-06-26-web-desktop-overview.md](../design/2026-06-26-web-desktop-overview.md)
> - AgentTeams 设计：[docs/agent_teams/AgentTeams功能设计文档.md](../agent_teams/AgentTeams功能设计文档.md)

---

## ✅ 里程碑回顾（按时间顺序）

| 阶段 | 内容 | 完成 |
|---|---|---|
| 耦合度优化 P1 | Config 统一 55 项环境变量 | ✅ |
| 耦合度优化 P2 | ToolBootstrap 自动发现（33 imports → 0） | ✅ |
| 耦合度优化 P3 | Config 注入 8 模块 | ✅ |
| 架构重构 P1 | AgentFeature 协议 + env_helpers | ✅ |
| 架构重构 P2 | 10 个 Feature 迁移 + MCPFeature | ✅ |
| Pi 学习 A | 提示词瘦身（~19K → ~2.2K tokens） | ✅ |
| Pi 学习 B | 会话树（JSONL / fork / `/tree` / model_change / thinking） | ✅ |
| Pi 学习 C | Plan Mode 文件化（PLAN.md） | ✅ |
| Pi 学习 D | Late Binding ContextBuilder | ✅ |
| Pi 学习 E | 工具描述内联（33 prompt 文件削除） | ✅ |
| MCP 修复 | 跨事件循环 stale session 检测 | ✅ |
| 审计批量 1-5 | 32 条审计中 17 条修复 | ✅ |
| **2026-06 加固** | **agent-runtime-decouple**（SessionController + 事件系统 + permission broker） | ✅ |
| 2026-06 加固 | **Web/桌面端**（FastAPI 41 REST + WebSocket + Vite/React/TS 前端） | ✅ |
| 2026-06 加固 | Team Engine 生产硬化（单一所有权 / `.teams/` 持久化 / retry+backoff / 心跳 sweep） | ✅ |
| 2026-06 加固 | LLM Streaming（`stream_raw()` + TUI Rich Live + ReAct 主链路集成） | ✅ |
| 2026-06 加固 | 两层 Skill 系统（`skills/` + `.mycodeagent/skills/`） | ✅ |
| 2026-06 加固 | Hooks 管理面板（`.mycode/hooks.json` Web UI 编辑） | ✅ |
| 2026-06 加固 | SlashCommandRegistry（替换 530 行 if-elif） | ✅ |
| 2026-06 加固 | `@skill:` 自动补全 | ✅ |
| 2026-06-29 | 删除 SwitchModel 工具（模型切换是用户策略，不暴露给 LLM） | ✅ |
| 2026-06-29 | Pi 风 summary（`<summary>` XML 包，user 消息）+ byte-stable arguments | ✅ |
| 2026-06-29 | feature 系统反思文档（8 个改进点） | ✅ |
| 2026-06-29 | TurnExecutor reasoning_content 修复（DeepSeek/Qwen 兼容） | ✅ |
| 2026-06-29 | README / CLAUDE.md / PROJECT_OVERVIEW 刷新到当前状态 | ✅ |
| **2026-06-29 P1 §1** | MCP 工具计数从 ToolRegistry 拿（`desktop/service/app.py:742` TODO 清掉）`de3bd03` | ✅ |
| **2026-06-29 P1 §5** | WorktreeFeature 非 git 目录预检 + 工具自动剔除 + 3 测试 `69803ad` | ✅ |
| **2026-06-29 P1 §4** | BudgetFeature enforce 模式（opt-in，`BUDGET_ENFORCE=true`）+ `BudgetExceeded` 异常 + 6 测试 `ac9de49` | ✅ |
| **2026-06-29 P1 §3** | `on_model_changed` lifecycle hook + SessionFeature/BudgetFeature 2 个 reactor + 8 测试 `dc47226` | ✅ |
| **2026-06-29 P1 §2** | `cleanup()` 协议激活：close() 反向迭代 features；MCPFeature/AgentTeamsFeature.cleanup 新建；idempotent + 10 测试 `13c59b7` | ✅ |

**P1 §1-5 累计**：5 个 commit，~600 行代码，+27 测试（958 → 985），实际耗时约 2.5 小时（vs 估时 2.5 人天，因 scope 清晰）。

| **2026-06-29 Phase 5** | 子代理流式（Step 25-28：事件契约 + TurnExecutor 流式 + sync Task 事件总线 + 后台 observer + TaskOutput 中间态 + Web/TUI 渲染）4 个 commit `8e39b60`, `fbbb381`, `f946e9a`, now | ✅ |

**Phase 5 统计**：4 个 commit，~700 行代码，+24 测试（985 → 1009），约 3 小时。`docs/design/2026-06-29-subagent-streaming-design.md`。

---

## 🔥 P1 立即可做（"小快好省"）—— ✅ 全部完成

> 2026-06-29 已 ship，详见上方里程碑表。原 5 项内容备查：
> 1. MCP 工具计数 TODO（`de3bd03`）
> 2. `cleanup()` 协议激活（`13c59b7`）
> 3. `on_model_changed` 事件 hook（`dc47226`）
> 4. BudgetFeature enforce 模式（`ac9de49`）
> 5. WorktreeFeature 非 git 预检（`69803ad`）

---

## 🚀 P1 中期项（设计已写明，1-5 天工作量）

### A. tmux 真正驱动 teammate（1-2 天）

**现状**：tmux 模式是有壳无核——`new-session`/`new-window` 创建出窗口，但**不带 command 参数**，窗口里是空 shell，teammate agent 根本没进去。

**完成态**：通过 `tmux send-keys` 或 `respawn-pane -k '<cmd>'` 把 teammate 的工作输出 / REPL bridged 到对应 window；或退一步把 worker 的事件流 tee 到对应 pane。

**关键文件**：`core/team_engine/tmux_orchestrator.py:32,49`

**附带**：跨平台开关（Windows 必须 fallback）、真实 tmux 集成测试（目前全是 mock）。

### B. 架构重构 Phase 3 —— ReAct 循环瘦身（3-5 天）

| Step | 内容 | 难度 |
|---|---|---|
| 16 | `_collect_runtime_blocks()` —— 统一 Feature 上下文收集 | 小 |
| 17 | `_invoke_llm_with_interception()` —— VCR 拦截在此 | 中 |
| 18 | 重构 `_execute_tool()` 使用 Feature 拦截 | 中 |
| 19 | 添加工具耗时统计 | 小 |
| 20 | 废弃旧 `/save` `/load` API | 小 |

### C. 架构重构 Phase 5 —— 子代理流式 ✅ 已完成

> 2026-06-29 已 ship（Steps 25-28，4 commits，24 new tests）。设计文档：
> `docs/design/2026-06-29-subagent-streaming-design.md`。
> 子代理（同步 Task ± 后台 Task）的 LLM token、工具调用现在通过事件总线实时回流。
> WebSocket 白名单已放行 4 个 subagent 事件类型；TUI `tui/subagent_renderer.py` 新增。
> 主 Agent 通过 `TaskOutput(task_id)` 可查询运行中子代理的 `last_step / current_tool / last_event`。
> 详情：`docs/design/2026-06-29-subagent-streaming-design.md §7 验收标准`。

### D. TUI streaming.py 真正激活（1 天）

**现状**：P1 LLM streaming 主链路完成，但 TUI `streaming.py` 仍是骨架，reasoning delta 在 `scripts/chat_test_agent.py:681` 被显式 drop。

**完成态**：reasoning block 单独渲染、token 速率显示、step 标识。

### E. 审计 P1 项剩余（每项 0.5-1 天）

- P1 #10 MCPFeature 接管 MCP 工具注册（已部分完成，缺补全）
- P1 #11 Hook + Feature 双层拦截合并
- P1 #12 14 个团队工具与主 tool 表去重
- P1 #14 每次 `run()` save 3 次的优化

---

## 🛠 P2 设计已写明（下一阶段）

| # | 项 | 工作量 | 出处 |
|---|---|---|---|
| 6 | **`post_tool_use` 改写 result** —— 协议返回 `{system_messages, rewritten_result}` 并向后兼容，对齐 Hermes transform | 1 天 | reflections §3.1 |
| 7 | **ContextEngineFeature 抽象** —— history_manager / context_builder / summary_compressor 抽成 `ContextEngineFeature(order=10)`，允许 plugin 替换 | 2-3 天 | reflections §3.2 |
| 8 | **plugin `kind=exclusive` 冲突检测** —— 与 #7 一起出，防止两个 plugin 静默争 ContextEngine slot | 1 天 | reflections §3.4 |
| 9 | **BackgroundTaskFeature shutdown** —— `BackgroundTaskRunner.stop()` + cooperative cancel flag + `cleanup()` join | 1 天 | `core/background_task.py:122-123` |
| 10 | **Team Engine 加固** —— per-worktree `.teams/.tasks` 隔离 / TeamManager 拆分（WorkerLoopService / TeamStateSnapshotService / TeamDisplayService / tmux adapter）/ 可观测性（retry 写 metadata、approval 事件、message-status 损坏告警） | 6-10 天 | team-engine-hardening §三 |
| 11 | **Web/Desktop 鉴权 + WS 双向化** —— Token/Origin 校验；WS 接收 interrupt / flow control；payload 增加 reasoning-delta / tool_call-delta | 2-3 天 | web-desktop-overview §11 |
| 12 | **架构重构 Phase 4 测试补全** —— PluginLoader 已建但缺测试 | 1 天 | 既有 todo |
| 13 | **VCR 流事件 fixtures + TeamEngine TurnExecutor 流式** | 2-3 天 | llm-streaming §5 |
| 14 | **Anthropic Messages API 原生 streaming adapter** —— 真接 Anthropic 主路径 | 2-3 天 | llm-streaming §5 |
| 15 | **审计 P2 项剩余** —— #21 VCR fixtures 目录 / #23 fake test 重写 / #24 TUI 组件测试 / #27 agent_teams 双重 init | 4-6 天 | 既有 todo |

---

## 🧹 P3 体验 / 重构 / Claude Code parity

| # | 项 | 工作量 | 出处 |
|---|---|---|---|
| 16 | **`/team msg <name> ...` direct teammate 交互（U2）** —— Claude Code parity 体验层最后一块 | 1 天 | parity-checklist:209-213 |
| 17 | **TUI MCP 连接状态展示（Phase 6）** —— 状态栏显示 MCP server 连接情况 | 0.5 天 | 既有 todo |
| 18 | **ToolWhitelistFeature 基类** —— DelegateModeFeature / PlanModeFeature 几乎重复，提共同基类 | 0.5 天 | reflections §4.2 |
| 19 | **PlanModeFeature 跨 session 自动 load PLAN.md** —— init 时检查 project_root/PLAN.md 自动注入 | 0.5 天 | `core/features/plan_mode.py:28-39` |
| 20 | **架构重构 Phase 6 体验优化** —— MCP 连接状态显示（与 #17 重叠）| - | 既有 todo |

---

## 🌫 P4 观察 / YAGNI

记着但暂不动手，等真出现需求再说：

| 项 | 说明 |
|---|---|
| `runtime_blocks` 优先级机制 | 当前只有 SystemReminderFeature 一个 user，机制保留观察即可 |
| `llm_intercept` 单用户复盘 | 6 个月观察期：若仍是 VCR 唯一用户，考虑退化成 `agent._llm_wrapper` 单字段 |

---

## ❌ 已设计 / 显式放弃（won't fix）

这些项已在设计文档里出现过，但综合评估后决定**不补建**。建议把对应设计文档加 ❌ 标注。

| 项 | 原始出处 | 放弃理由 |
|---|---|---|
| split-pane teammate 模式 | parity-checklist:43 | display_mode.py 和 config.py 的 `VALID_TEAMMATE_MODES` 已不收 split-pane，MVP non-goal 已声明 |
| 终端焦点切换 / 键盘导航（Shift+Up/Down） | AgentTeams 设计文档:180 | 已显式 non-goal |
| VCR `all` 模式 | `core/vcr.py:80-83` 文档列 3 模式但有 audit 提到 4 个 | 实际只有 3 个，文档里删掉"4 模式"提法即可 |
| Tauri / Electron 桌面壳 | web-desktop-overview | 本地 dev 场景用不上，Web UI 已够用 |
| `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` env 兼容 | parity-checklist C1 | 纯 parity 兼容层，README 加 mapping 表足够 |
| `~/.claude/teams` dual-mode 路径 | parity-checklist C2 | 同上，不真做 |

---

## 实施建议（已调整 — P1 §1-5 和 Phase 5 全部 ship）

### 第 1 批 ✅ —— P1 §1-5 + Phase 5 已完成（2026-06-29）

### 下一步（推荐 A，按 ROI 排序）

**A. tmux 真做（1-2 天）—— 推荐首选**
- 项目里唯一"画了壳没装核"的显眼功能
- scope 清晰：`tmux_orchestrator.py` 加 send-keys/respawn-pane，TeamManager 钩子已就位
- 体验上立竿见影：`TEAMMATE_MODE=tmux` 真能用了

**B. ReAct Phase 3 Step 16-20（3-5 天）**
- 框架级瘦身，影响所有未来的 feature 改动
- 难度中等，独立性强
- 推荐 A 之后做

**C. Phase 5 子代理流式（2-3 天）**
- BackgroundTaskRunner 进度回调 + TUI 展示
- 需要在 P1 LLM streaming 完成后做（已完成）
- 解锁"看子代理在干嘛"的体验

**D. 选一个 P2 启动**（推荐顺序）
- P2 #6 `post_tool_use` 改 result（最独立，1 天）
- P2 #7+#8 ContextEngineFeature + plugin kind=exclusive（一起出，2-3 天）
- P2 #9 BackgroundTaskFeature shutdown（独立，1 天）
- P2 #11 Web 鉴权 + WS 双向化（独立，2-3 天）
- P2 #10 Team Engine 拆分（大工程，6-10 天，最后做）

### 长期（按需）
- Phase 4 测试补全
- Anthropic 主路径适配
- P3 体验项（`/team msg <name>`、TUI MCP 状态等）
- 文档清理：把放弃项的 ❌ 标到原设计文档里

---

## Roadmap 维护规则

1. **完成一项**：从 P 区移到 "里程碑回顾"，加完成时间
2. **新发现 todo**：先归到 P 区某个优先级，附 issue/commit/file:line 出处
3. **改优先级**：附简短理由（一行注释）
4. **放弃**：移到 "已设计 / 显式放弃" 区，加放弃理由

---

**TL;DR**：P1 §1-5 + Phase 5 全部 ship ✅（2026-06-29）。下一首选 tmux 真做（1-2 天）→ Phase 3 ReAct 瘦身（3-5 天）→ 选一个 P2 启动。
