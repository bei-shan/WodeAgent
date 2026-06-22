# MyCodeAgent (WodeAgent) 项目总览

> 版本: v3.0 | 日期: 2026-06-22 | 测试: 841 passed, 0 failed

---

## 一、项目简介

MyCodeAgent 是一个 AI 代码助手，基于 ReAct 循环架构。支持多工具调用、子代理委托、团队协作、git worktree 文件隔离、Claude Code 风格的 TUI 界面。

### 技术栈

Python 3.12+ / OpenAI-compatible API / prompt_toolkit + Rich (TUI) / JSONL + JSON 文件存储

---

## 二、架构总览

```
entrypoint: scripts/chat_test_agent.py
  └── RichConsoleCodeAgent (TUI 层)
        └── CodeAgent (agents/codeAgent.py) ── ReAct 循环
              ├── ContextBuilder          (system prompt + tools 组装)
              ├── HistoryManager          (消息历史 + 压缩)
              ├── SummaryCompressor       (上下文摘要)
              ├── ToolRegistry            (工具注册表 + 熔断)
              ├── PermissionGate          (软沙箱)
              ├── WorktreeManager         (git worktree 隔离)
              ├── BackgroundTaskRunner    (后台子代理)
              ├── OutputStyleManager      (输出风格)
              ├── VCR                     (LLM 录制回放)
              ├── HookManager             (生命周期钩子)
              ├── TeamManager             (AgentTeams 团队协作)
              ├── HelloAgentsLLM          (LLM 客户端)
              └── TraceLogger             (结构化追踪)

TUI 层: tui/
  ├── StreamingResponse     (Rich Live 流式渲染)
  ├── MentionCompleter      (@-mention 补全)
  ├── PermissionDialog      (交互式权限弹窗)
  └── StatusLine            (模型/plan/style/worktree 状态栏)
```

---

## 三、工具清单（33 个）

### 文件操作 (6)
| 工具 | 说明 |
|------|------|
| Read | 读取文件（含乐观锁 mtime/size） |
| Write | 创建/覆写文件 |
| Edit | 精确字符串替换编辑 |
| MultiEdit | 批量编辑同一文件 |
| LS | 目录列表 |
| Glob | 文件名 glob 搜索 |

### 搜索 (1)
| 工具 | 说明 |
|------|------|
| Grep | 正则内容搜索 (ripgrep) |

### 系统 (3)
| 工具 | 说明 |
|------|------|
| Bash | Shell 命令执行 |
| TodoWrite | 任务管理/计划跟踪 |
| AskUser | 向用户提问 |

### 子代理 (2)
| 工具 | 说明 |
|------|------|
| Task | 子代理委派 (oneshot/persistent/parallel + background) |
| TaskOutput | 查询后台任务结果 |

### 技能与交互 (2)
| 工具 | 说明 |
|------|------|
| Skill | 加载命名技能 |
| SwitchModel | 切换 LLM 模型 |

### 计划模式 (2)
| 工具 | 说明 |
|------|------|
| EnterPlanMode | 进入只读分析模式 |
| ExitPlanMode | 退出并注入计划 |

### Worktree 隔离 (2)
| 工具 | 说明 |
|------|------|
| EnterWorktree | 创建/进入 git worktree |
| ExitWorktree | 退出并保留/删除 worktree |

### AgentTeams 团队 (16)
| 工具 | 说明 |
|------|------|
| TeamCreate | 创建团队 |
| SendMessage | 发送团队消息 |
| TeamStatus | 查看团队状态 |
| TeamDelete | 删除团队 |
| TeamCleanup | 清理团队数据 |
| TeamList | 列出所有团队 |
| TeamRetry | 重试失败的工作项 |
| TeamFanout | 并行分发任务 |
| TeamCollect | 收集并行结果 |
| TeamApprovals | 查看 plan 审批 |
| TeamApprovePlan | 审批 plan |
| TeamTaskCreate | 创建看板任务 |
| TeamTaskGet | 获取任务详情 |
| TeamTaskUpdate | 更新任务状态 |
| TeamTaskList | 列出任务 |

### MCP 外部工具
| 服务器 | 状态 | 工具 |
|--------|------|------|
| fetch | ✅ | web 请求 |
| context7 | ✅ | 文档查询 |
| tavily-mcp | ✅ | 网络搜索 |

---

## 四、核心功能

### 4.1 Plan Mode

```
触发: /plan | --plan CLI | LLM 调用 EnterPlanMode
 → 只读工具: Read/Grep/Glob/LS/TodoWrite
 → 分析 → 产出计划
 → ExitPlanMode(plan="...") → 注入 plan + TodoWrite 提醒
```

### 4.2 Background Task

```
Task(run_in_background=true) → daemon 线程 → 主循环继续
 → Runtime block 显示任务状态
 → TaskOutput(task_id) 获取结果
 → 结果原子写入: .tasks/output/{task_id}.json
```

### 4.3 Worktree 会话隔离

```
EnterWorktree(name) → git worktree add → project_root 切换
 → 所有工具自动跟随 → 子代理继承
ExitWorktree(keep|remove) → 恢复 project_root
```

### 4.4 AgentTeams

15 个工具，10/10 验收基线通过。支持：
- 团队创建、消息通信（5 种消息类型，ACK 三态）
- 任务看板（依赖解锁、自动认领）
- plan approval 闸门
- daemon worker 并行执行（信号量控制 LLM 并发）
- 会话持久化（cursor 增量 inbox 读取）
- role-based system prompt（developer/reviewer/planner）
- 存储限制（`TEAM_MAX_INBOX_SIZE` / `TEAM_MAX_WORK_ITEMS`）
- tmux 显示模式

### 4.5 Model Management

```
三层体系:
  Model Profiles  → MODEL_PROFILES + MODEL_<NAME>_ID/_API_KEY/_BASE_URL
  Model Pointers  → MODEL_POINTER_MAIN/TASK/COMPACT
  /model <name>   → 手动切换 (用户或 LLM)
```

### 4.6 Output Styles

| 风格 | 效果 |
|------|------|
| `default` | 保持简洁高效 |
| `explanatory` | ★ Insight 格式解释实现选择 |
| `learning` | Learn by Doing + TODO(human) 交互 |

配置: `AGENT_OUTPUT_STYLE` env var 或 `/style` 命令。通过 `{output_style}` 占位符注入 L1 系统提示末尾。

### 4.7 VCR — LLM API 录制回放

```
VCR_ENABLED=true → 拦截 LLM 调用
  ├── SHA-256(messages+tools) → fixture 文件名
  ├── fixture 存在 → 回放 (0 API 调用)
  └── fixture 不存在 → 调 API → 写入 fixture
```

3 种模式: `new_episodes` / `once` / `none`

### 4.8 Hook System — 生命周期钩子

| 事件 | 能力 |
|------|------|
| SessionStart | 注入上下文、设置环境变量 |
| PreToolUse | 阻止/修改工具调用 (exit 2 block) |
| PostToolUse | 注入系统消息 |
| SessionEnd | 清理资源 |

配置: `.mycode/hooks.json`，matcher 支持 `*` / 精确名 / glob

### 4.9 Claude Code 风格 TUI

| 组件 | 功能 |
|------|------|
| streaming | Rich Live 流式渲染 LLM 输出 |
| @-mention | prompt_toolkit 补全 agent/model/file |
| PermissionDialog | prompt_toolkit 交互式权限弹窗 |
| StatusLine | 显示 `[model] [plan] [style:xxx] [wt:name]` |

### 4.10 上下文工程

- **分层提示**: L1 (system+tools) + L2 (CODE_LAW) + runtime blocks
- **历史压缩**: `COMPRESSION_THRESHOLD` 触发 LLM 摘要，超时回退截断
- **工具输出截断**: 超限落盘 `tool-output/`，7 天自动清理
- **@file 预处理**: 注入 system-reminder 强制 Read
- **熔断**: 连续 3 次失败 → 临时禁用 → 300s 后半开
- **Trace**: JSONL + HTML 双轨，自动脱敏
- **乐观锁**: Read/Write/Edit 的 mtime/size 校验

### 4.11 安全

- **软沙箱**: `PERMISSION_SOFT_SANDBOX=true`，项目外路径需确认
- **敏感目录永拒**: `/etc/shadow`, `C:\Windows\System32\`, `.ssh/id_rsa` 等
- **子代理权限**: 子代理共享主代理的权限缓存，不可交互

---

## 五、配置速查

```bash
# 基础 LLM
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL_ID=deepseek-v4-pro

# Model Profiles (for /model switching)
MODEL_PROFILES=deepseek,deepseek-chat
MODEL_DEEPSEEK_ID=deepseek-v4-pro
MODEL_DEEPSEEK_CHAT_ID=deepseek-chat
MODEL_POINTER_MAIN=deepseek
MODEL_POINTER_TASK=deepseek-chat
MODEL_POINTER_COMPACT=deepseek-chat

# MCP (mcp_servers.json or MCP_SERVERS env var)
MCP_CONNECT_MODE=manual

# 功能开关
ENABLE_AGENT_TEAMS=false
PERMISSION_SOFT_SANDBOX=true

# 输出风格
# AGENT_OUTPUT_STYLE=explanatory

# VCR (测试用)
# VCR_ENABLED=true

# Worktree
WORKTREE_STORE_DIR=.worktrees
WORKTREE_BASE_REF=fresh

# 上下文
CONTEXT_WINDOW=128000
COMPRESSION_THRESHOLD=0.8
MIN_RETAIN_ROUNDS=10

# Trace
TRACE_ENABLED=true
TRACE_SANITIZE=true
```

---

## 六、项目结构

```
MyCodeAgent/
├── agents/               # Agent 核心
│   └── codeAgent.py      # CodeAgent (ReAct 循环)
├── core/                 # 核心引擎
│   ├── llm.py            # HelloAgentsLLM
│   ├── config.py         # 配置
│   ├── agent.py          # Agent 基类
│   ├── background_task.py   # 后台任务
│   ├── model_profiles.py    # 模型 profiles + pointers
│   ├── output_styles.py     # 输出风格管理
│   ├── vcr.py               # LLM 录制回放
│   ├── hook_system.py       # 生命周期钩子
│   ├── session_store.py     # 会话持久化
│   ├── response_parser.py   # 响应解析
│   ├── context_engine/      # 上下文构建 + 历史 + 压缩 + trace
│   ├── team_engine/         # AgentTeams (19 文件)
│   ├── worktree/            # git worktree
│   └── skills/              # 技能加载器
├── tools/                # 工具
│   ├── base.py           # Tool 基类 + 协议
│   ├── registry.py       # ToolRegistry
│   ├── permission_gate.py  # 软沙箱
│   ├── circuit_breaker.py  # 熔断器
│   ├── builtin/          # 33 个内置工具
│   └── mcp/              # MCP 客户端 (6 文件)
├── tui/                  # TUI 组件
│   ├── streaming.py      # 流式渲染
│   ├── mention_completer.py  # @-mention
│   ├── permission_dialog.py  # 权限弹窗
│   └── status_line.py        # 状态栏
├── prompts/              # LLM 提示词
│   ├── agents_prompts/   # Agent 系统提示 (7 文件)
│   ├── tools_prompts/    # 工具描述 (38 文件)
│   └── output_styles/    # 输出风格 (default/explanatory/learning)
├── scripts/              # 入口脚本
│   └── chat_test_agent.py  # 主入口
├── tests/                # 测试 (77 文件, 841+ 用例)
├── docs/                 # 文档
│   ├── plans/            # 设计文档 (7 份)
│   └── agent_teams/      # AgentTeams 设计文档
├── memory/               # trace/session/todo 输出
├── skills/               # 技能定义
├── tool-output/          # 工具输出落盘
├── .env.example          # 配置模板
└── mcp_servers.json      # MCP 配置
```

---

## 七、MCP 配置

当前 `mcp_servers.json` 配置了 3 个服务器：

| 服务器 | 命令 | 需要 |
|--------|------|------|
| fetch | `uvx mcp-server-fetch` | uvx |
| context7 | `npx @upstash/context7-mcp` | CTX7_API_KEY |
| tavily-mcp | `npx tavily-mcp@0.1.4` | TAVILY_API_KEY |

连接模式：`MCP_CONNECT_MODE=manual`（后台连接 + 每步 ReAct 自动重试）

---

## 八、Slash Commands

| 命令 | 说明 |
|------|------|
| `/model` | 显示当前模型 + token 统计 |
| `/model <id>` | 切换模型 |
| `/info` | 详细 token 用量 |
| `/plan` | 切换 Plan Mode |
| `/style [name]` | 显示/设置输出风格 |
| `/save [path]` | 保存会话快照 |
| `/load [path]` | 加载会话快照 |
| `/team msg <...>` | 发送团队消息 |
| `/team watch <...>` | 监控团队进度 |
| `/delegate <on\|off>` | 切换委托模式 |
| `/help` | 显示帮助 |
| `init` | 生成 code_law.md |
| `exit` / `quit` / `q` | 退出 |

---

## 九、子代理类型

| 类型 | 用途 |
|------|------|
| `general` | 复杂执行、子任务 |
| `explore` | 代码库扫描、入口点发现 |
| `plan` | 实现步骤分析、依赖评估 |
| `summary` | 压缩长输出、多文件总结 |

---

## 十、Agent 模式

| 模式 | 说明 |
|------|------|
| Plan | 只读工具，产出计划后恢复全部工具 |
| Delegate | 仅团队管理工具，工作由 teammates 执行 |
| Worktree | 切换到独立 git worktree 目录 |
| Background | 子代理 daemon 线程，主循环继续 |

---

## 十一、设计文档索引

| 文档 | 说明 |
|------|------|
| `docs/agent_teams/AgentTeams功能设计文档.md` | AgentTeams 完整设计 (v2) |
| `docs/plans/2026-06-18-agentteams-optimization-design.md` | AgentTeams 优化方案 |
| `docs/plans/2026-06-18-worktree-feature-design.md` | Worktree 功能设计 |
| `docs/plans/2026-06-18-soft-sandbox-permission-design.md` | 软沙箱权限设计 |
| `docs/plans/2026-06-18-kode-agent-learning-plan.md` | Kode-Agent 分析 + 优化计划 |
| `docs/plans/2026-06-22-output-styles-design.md` | 输出风格设计 |
| `docs/plans/2026-06-22-vcr-hook-system-design.md` | VCR + Hook 设计 |
| `docs/plans/2026-02-17-agentteams-parallel-execution-implementation.md` | 并行执行方案 |
| `docs/plans/2026-02-17-claudecode-teams-replication-checklist.md` | Claude Teams 复刻清单 |
| `docs/上下文工程设计文档.md` | 上下文工程 |
| `docs/通用工具响应协议.md` | 工具协议规范 |
| `docs/工具输出截断设计文档.md` | 输出截断策略 |
| `docs/TraceLogging设计文档.md` | Trace 追踪 |
| `docs/DEV_HANDOFF.md` | 开发者交接 |

---

## 十二、Kode-Agent 学习计划 — 全部完成 ✅

| 优先级 | 功能 | 状态 |
|--------|------|------|
| P0 | Plan Mode | ✅ |
| P0 | Background Task | ✅ |
| P1 | Model Pointers + /model | ✅ |
| P1 | WebSearch/WebFetch (MCP) | ✅ |
| P2 | Output Styles | ✅ |
| P3 | VCR | ✅ |
| P3 | Hook System | ✅ |
| P3 | LSP | ❌ 搁置 |
