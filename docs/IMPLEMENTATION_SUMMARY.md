# MyCodeAgent 完整实现总结

> 最后更新: 2026-06-26 | 测试: 937 passed, 0 failed

---

## 一、AgentTeams 团队协作

### 做了什么

实现了完整的多 Agent 团队协作系统：15 个团队工具、消息通信、任务看板、计划审批、daemon worker 并行执行。

### 为什么这么做

Claude Code 的 AgentTeams 是生产级多 Agent 协作的参考实现。通过复刻其核心机制来理解多 Agent 系统的设计模式。

### 怎么做的

- **架构**：5 个单一职责服务 (TeamStore / MessageRouter / TaskBoardStore / ApprovalService / WorkerSupervisor) + TeamManager 装配 + ExecutionService 单 turn 内核
- **隔离模型**：in-process daemon thread + JSONL 文件即消息队列（非 actor 模型）
- **锁策略**：mkdir 原子创建 + 30s stale reclaim，8 线程 vs 5 任务 claim 无重复
- **15 个工具**：TeamCreate / SendMessage / TeamStatus / TeamDelete / TeamCleanup / TeamFanout / TeamCollect / TeamApprovals / TeamApprovePlan / TeamTaskCreate / TeamTaskGet / TeamTaskUpdate / TeamTaskList / TeamList / TeamRetry
- **10/10 验收基线**：消息、广播、shutdown request/response、plan approval、任务看板 CRUD、依赖阻塞、并发 claim、自动认领、runtime 摘要、cleanup 流程、save/load 恢复
- **心跳机制**（后续优化）：work_item 增加 `heartbeat_ts` + 后台 sweep 线程，300s 无心跳自动 requeue

---

## 二、软沙箱权限系统

### 做了什么

实现了基于项目根目录的软沙箱：项目外路径需用户确认，敏感目录永拒，乐观锁防并发写冲突。

### 为什么这么做

Claude Code 的权限系统是安全基线。软沙箱比"YOLO 模式"更适合学习场景——既保护用户又让用户理解 Agent 的操作边界。

### 怎么做的

- **PermissionGate**：项目外路径需用户确认，敏感目录（`/etc/shadow`, `C:\Windows\System32\`, `.ssh/id_rsa`）永拒
- **乐观锁**：Read 时缓存 mtime/size，Write/Edit 前自动注入期望值，不匹配返回 CONFLICT
- **子代理权限**：共享主代理的权限缓存，不可交互

---

## 三、Git Worktree 会话隔离

### 做了什么

EnterWorktree → 切换到独立 git worktree 目录 → 所有工具自动跟随 → ExitWorktree 恢复。

### 为什么这么做

Claude Code 的 worktree 机制让 Agent 可以在隔离的文件系统中工作，避免实验性修改影响主分支。对于学习项目，这是理解文件系统隔离的绝佳案例。

### 怎么做的

- **WorktreeManager**：`git worktree add` 创建隔离目录，`project_root` 动态切换
- **工具自动跟随**：所有工具的 `project_root` 由框架统一注入，切换后自动指向 worktree
- **子代理继承**：Task 工具创建的 subagent 自动使用当前 worktree

---

## 四、首次启动向导

### 做了什么

检测首次运行 → 交互式引导配置 LLM provider + API key → 生成 `.env` → 显示快速入门。

### 为什么这么做

降低新用户的使用门槛。手动创建 `.env` 文件、查找 API key、配置 provider 对新手不友好。

### 怎么做的

- **触发条件**：`.env` 不存在或 `LLM_API_KEY` 为空
- **6 步流程**：欢迎 → 选 Provider（7 个预设）→ 输 API Key → 功能开关 → 确认 → 完成
- **Provider 预设**：DeepSeek / OpenAI / Zhipu / Kimi / Qwen / Ollama / Custom
- CLI 参数：`--wizard` 强制触发，`--skip-wizard` 跳过

---

## 五、多会话管理

### 做了什么

`/sessions` 列出所有会话、`/resume` 切换到指定会话、`/rename` 重命名、自动保存。

### 为什么这么做

借鉴 Claude Code 的会话管理，让用户可以在多个对话之间切换，每个会话独立保存和恢复。

### 怎么做的

- **SessionManager**：`memory/sessions/` 目录 + `index.json` 索引
- **快照格式 v2**：system_messages + history_entries + cursor_id + labels + model + thinking_level
- **自动保存**：Ctrl+C / exit / 正常结束 三种触发，覆盖写入 `session-latest.json`
- **向后兼容**：v1 快照自动升级到 v2

---

## 六、Output Styles 输出风格

### 做了什么

三种内置风格：`default`（简洁高效）、`explanatory`（解释实现选择）、`learning`（Learn by Doing 交互）。

### 为什么这么做

Kode-Agent 的输出风格系统让用户可以根据场景选择 Agent 的交互方式。学习代码库时用 explanatory，日常编码用 default。

### 怎么做的

- **OutputStyleManager**：加载 `prompts/output_styles/*.md` + 项目 `output_styles/*.md`
- **注入方式**：`{output_style}` 占位符替换到 L1 系统提示末尾
- **自定义风格**：Markdown + YAML frontmatter，项目自定义覆盖内置
- **CLI**：`/style` 显示/切换，StatusLine 显示当前风格

---

## 七、Token 用量预算

### 做了什么

`/budget 500k` 设置 token 预算上限，Agent 追踪消耗，接近上限时提醒。

### 为什么这么做

Claude Code 的 budget 指令让用户控制单次会话的 token 消耗。对于按 token 计费的 API，这是成本控制的关键功能。

### 怎么做的

- **TokenBudget**：解析 `500k` / `10万` / `1M` 格式
- **追踪**：每次 LLM 调用后 `spend(total_tokens)`
- **CLI**：`/budget` 显示剩余，`/budget 500k` 设置，`/budget none` 清除

---

## 八、VCR — LLM API 录制回放

### 做了什么

录制真实 LLM 请求/响应对为 JSON fixture，测试时回放 fixture，零 API 调用。

### 为什么这么做

Kode-Agent 的 VCR 系统让测试变快、变确定、变免费。SHA-256 去重确保相同输入命中相同 fixture。

### 怎么做的

- **3 种模式**：`new_episodes`（录制新）、`once`（调用不存储）、`none`（只回放）
- **去重**：对 messages 做 dehydrate（去 CWD/时间戳）后 SHA-256 作为 fixture 文件名
- **集成**：通过 Feature 的 `llm_intercept` 钩子拦截 `invoke_raw`
- **配置**：`VCR_ENABLED` / `VCR_RECORD_MODE` / `VCR_FIXTURE_DIR`

---

## 九、Hook System — 生命周期钩子

### 做了什么

在 Agent 的 4 个生命周期节点插入自定义脚本：SessionStart / PreToolUse / PostToolUse / SessionEnd。

### 为什么这么做

Kode-Agent 的 Hook 系统让用户自定义工作流——提交前自动 lint、Bash 执行前审计、启动时注入环境变量。

### 怎么做的

- **配置**：`.mycode/hooks.json`，matcher 支持 `*` / 精确名 / glob
- **执行**：subprocess 调用，stdin JSON 输入，stdout JSON 输出
- **退出码**：0=成功，1=警告，2=硬阻止
- **能力**：阻止/修改工具调用、注入系统消息、设置环境变量

---

## 十、提示词瘦身 (Pi Agent 学习)

### 做了什么

系统提示词从 ~19K tokens 压缩到 ~2.2K tokens，减少 88%。

### 为什么这么做

Pi Agent 的"减法思维"证明：前沿模型不需要冗长的提示词。Pi 仅用 ~300 词提示词就在 TerminalBench 排名第 2。

### 怎么做的

1. **工具提示词按需注入**：给 `Tool` 基类加 `usage_notes` 属性（~50 字），替代原来每个工具 2000-5000 字的完整描述。这些描述的参数、响应格式已在 function calling schema 中存在，不需要重复发送
2. **L1 精简**：153 行 → 57 行，删除冗长示例和 "Available Tools" 区块
3. **Late Binding**：`ContextBuilder` 实时组装，工具提示词从 `Tool.usage_notes` 实时获取

---

## 十一、会话树 (Pi Agent 学习)

### 做了什么

借鉴 Pi 的 JSONL 会话树设计，将线性历史升级为树形结构。支持 fork、分支导航、model_change、thinking_level_change、branch_summary。

### 为什么这么做

Pi 的会话树是它最独特的设计：对话可以在任意历史节点分叉探索，所有分支保存在单个 JSONL 文件中。

### 怎么做的

- **Message 扩展**：`message_id`（8 位短 ID）+ `parent_id`（父节点）
- **HistoryManager**：`fork()` / `get_current_branch()` / `navigate_to()` / `get_tree()`
- **JsonlSessionStore**：Pi 兼容的 JSONL 格式，单个文件追加写入
- **SessionStore v2**：`cursor_id` + `history_entries` + `labels` + `current_model` + `thinking_level`
- **新命令**：`/tree`（ASCII 树形图）、`/fork <id>`（分叉）、`/thinking on|off`

---

## 十二、Plan Mode 文件化

### 做了什么

ExitPlanMode 将计划写入 `PLAN.md` 文件（同时保留上下文注入）。

### 为什么这么做

Pi 的理念："用 PLAN.md 替代 Plan Mode 工具——可版本控制、可共享、可审查"。

### 怎么做的

- `exit_plan_mode()` 增加 `_write_plan_md()`：生成带时间戳的 Markdown 计划文件
- 保留上下文注入（`runtime_blocks` 机制）
- 自动附加 TodoWrite 提醒

---

## 十三、耦合度优化

### 做了什么

消除 CodeAgent 对 33 个工具类的硬编码依赖，统一 55 项环境变量到 Config。

### 为什么这么做

CodeAgent 有 46 个 import 是全项目耦合度最高的文件。新增工具需要改 2 处。55 个 `os.getenv()` 散落在 19 个文件中。

### 怎么做的

- **ToolBootstrap**：DI 容器 + 自动扫描 `tools/builtin/`，根据构造函数参数名注入依赖
- **Config 统一**：55 项配置收敛到 `Config.from_env()`，属性访问 + os.getenv fallback
- **配置注入**：8 个模块通过 `set_xxx_config(config)` 接收

---

## 十四、架构重构

### 做了什么

1. **Feature 协议**：11 个 Feature 按 order 排序的可插拔架构
2. **DelegateModeFeature 拦截**：从 CodeAgent 硬编码移到 Feature.pre_tool_use()
3. **MCPFeature 接管**：MCP 注册+重试+状态全部从 CodeAgent 搬到 Feature
4. **RuntimeView 抽取**：105 行 Team 视图逻辑从 CodeAgent 抽到 `runtime_view.py`
5. **SlashCommandRegistry**：530 行 if-elif 替换为命令注册表
6. **Late Binding ContextBuilder**：删除全局缓存，实时组装

### 为什么这么做

审计报告指出 CodeAgent 是 1420 行 god class。Feature 协议提供开闭原则，新增功能不需要改 CodeAgent。

### 怎么做的

- 每个 Feature 实现 `init()` / `runtime_blocks()` / `pre_tool_use()` / `post_tool_use()` / `cleanup()` 钩子
- ReAct 循环统一调用 `_collect_runtime_blocks()` + Feature 拦截链
- SlashCommandRegistry：`registry.register("/model", handler_func)`

---

## 十五、审计修复 (24/32)

### P0 致命（全部修复）

| 修复 | 说明 |
|------|------|
| trace_logger token 死代码 | `return` 后的累加代码移到 `_update_stats` |
| CircuitBreaker 误熔断 | INVALID_PARAM 等 6 个安全错误码不计入 failure |
| Team Engine heartbeat | work_item 加 `heartbeat_ts` + sweep 线程 |
| Bash 截断 | 标记 `truncated=true`，ObservationTruncator 统一处理 |
| Bash 沙箱 | 新增 python 写文件操作检测 |
| 会话树主路径 | v2 snapshot 传递 cursor_id/history_entries |
| 三套持久化 | SessionStore v2 统一 |

### P1 高优（8 项修复）

max_steps 可配置、minimax 硬编码移除、token 估算保守化、RuntimeView 抽取、Hook+Feature 统一、MCPFeature 接管注册、SlashCommandRegistry、双份工具定义删除

### P2 卫生（9 项修复）

dead code 删除、plugin logger、subagent prompt 去重、ALWAYS_IGNORE 共享常量、VCR once 模式、@skill 补全、工具描述内联、Config dead field 删除
