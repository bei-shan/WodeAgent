# MyCodeAgent (WodeAgent) 项目总览

> 版本: v2.0 | 日期: 2026-06-20 | 测试: 737 passed, 0 failed

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
              ├── ToolRegistry            (工具注册表)
              ├── PermissionGate          (软沙箱)
              ├── WorktreeManager         (git worktree 隔离)
              ├── BackgroundTaskRunner    (后台子代理)
              ├── TeamManager             (AgentTeams 团队协作)
              ├── HelloAgentsLLM          (LLM 客户端)
              └── TraceLogger             (结构化追踪)

TUI 层: tui/
  ├── StreamingResponse     (Rich Live 流式渲染)
  ├── MentionCompleter      (@-mention 补全)
  ├── PermissionDialog      (交互式权限弹窗)
  └── StatusLine            (模型状态栏)
```

---

## 三、工具清单（27 个）

| 类别 | 工具 | 说明 |
|------|------|------|
| 文件 | Read, Write, Edit, MultiEdit | 文件读写编辑 |
| 文件 | LS, Glob | 目录列表、文件名搜索 |
| 搜索 | Grep | 正则搜索 |
| 系统 | Bash, TodoWrite | 命令执行、任务管理 |
| 交互 | AskUser, Skill | 询问用户、技能加载 |
| 子代理 | Task (+ background) | 子代理委派 + 后台执行 |
| 团队(13) | TeamCreate/SendMessage/Status/Delete/Cleanup/Fanout/Collect, TeamTaskCreate/Get/Update/List, TeamApprovals/ApprovePlan, TeamList, TeamRetry | 完整团队协作 |
| 隔离(2) | EnterWorktree, ExitWorktree | git worktree 会话隔离 |
| 计划(2) | EnterPlanMode, ExitPlanMode | 只读分析模式 |
| 模型 | SwitchModel | 切换模型 |
| 后台 | TaskOutput | 查询后台任务 |
| MCP | 动态 | MCP 工具注册 |

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
- 团队创建、消息通信（5 种消息类型）
- 任务看板（依赖解锁、自动认领）
- plan approval 闸门
- daemon worker 并行执行
- 会话持久化

### 4.5 Model Management

```
三层体系:
  Model Profiles  → MODEL_PROFILES + MODEL_<NAME>_ID/_API_KEY/_BASE_URL
  Model Pointers  → MODEL_POINTER_MAIN/TASK/COMPACT
  /model <name>   → 手动切换 (用户或 LLM)
```

### 4.6 Claude Code 风格 TUI

| 组件 | 功能 |
|------|------|
| streaming | Rich Live 流式渲染 LLM 输出 |
| @-mention | prompt_toolkit 补全 agent/model/file |
| PermissionDialog | prompt_toolkit 交互式权限弹窗 |
| StatusLine | prompt 行显示 `[model] [plan] [wt:name]` |

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

# Paths
WORKTREE_STORE_DIR=.worktrees
WORKTREE_BASE_REF=fresh
```

---

## 六、项目结构

```
MyCodeAgent/
├── agents/           # Agent 核心
│   └── codeAgent.py  # CodeAgent (ReAct 循环)
├── core/             # 核心引擎
│   ├── llm.py        # HelloAgentsLLM
│   ├── config.py     # 配置
│   ├── background_task.py   # 后台任务
│   ├── model_profiles.py    # 模型 profiles + pointers
│   ├── context_engine/      # 上下文构建
│   ├── team_engine/         # AgentTeams
│   └── worktree/            # git worktree
├── tools/            # 工具
│   ├── base.py       # Tool 基类 + 协议
│   ├── registry.py   # ToolRegistry
│   ├── permission_gate.py  # 软沙箱
│   ├── builtin/      # 内置工具 (27个)
│   └── mcp/          # MCP 客户端
├── tui/              # TUI 组件
│   ├── streaming.py  # 流式渲染
│   ├── mention_completer.py  # @-mention
│   ├── permission_dialog.py  # 权限弹窗
│   └── status_line.py        # 状态栏
├── prompts/          # LLM 提示词
│   ├── agents_prompts/  # Agent 系统提示
│   └── tools_prompts/   # 工具描述
├── scripts/          # 入口脚本
│   └── chat_test_agent.py  # 主入口
├── tests/            # 测试 (737个)
├── docs/             # 文档
└── mcp_servers.json  # MCP 配置
```

---

## 七、MCP 配置

当前 `mcp_servers.json` 配置了 3 个服务器：

```json
{
  "mcpServers": {
    "fetch": {
      "command": "uvx", "args": ["mcp-server-fetch"]
    },
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp", "--api-key", "${CTX7_API_KEY}"]
    },
    "tavily-mcp": {
      "command": "npx",
      "args": ["-y", "tavily-mcp@0.1.4"]
    }
  }
}
```

| 服务器 | 状态 | 需要 |
|--------|------|------|
| fetch | ✅ 可用 | `uvx` (已安装) |
| context7 | ❌ 缺 key | 注册 https://upstash.com 获取 `CTX7_API_KEY` |
| tavily-mcp | ❌ 缺 key | 注册 https://tavily.com 获取 `TAVILY_API_KEY` |

连接模式：`MCP_CONNECT_MODE=manual`（后台连接 + 每步 ReAct 自动重试）

---

## 八、Kode-Agent 学习计划进度

| 优先级 | 功能 | 状态 |
|--------|------|------|
| P0 | Plan Mode | ✅ |
| P0 | Background Task | ✅ |
| P1 | Model Pointers | ✅ |
| P1 | Model Profiles + /model | ✅ |
| P1 | WebSearch/WebFetch | ⬜ (MCP 已有配置) |
| P2 | Output Styles | ⬜ |
| P3 | LSP / Hook / VCR | ⬜ |

---

## 九、设计文档索引

| 文档 | 说明 |
|------|------|
| `docs/agent_teams/AgentTeams功能设计文档.md` | AgentTeams 完整设计 (v2) |
| `docs/plans/2026-06-18-agentteams-optimization-design.md` | AgentTeams 优化方案 |
| `docs/plans/2026-06-18-worktree-feature-design.md` | Worktree 功能设计 |
| `docs/plans/2026-06-18-soft-sandbox-permission-design.md` | 软沙箱权限设计 |
| `docs/plans/2026-06-18-kode-agent-learning-plan.md` | Kode-Agent 分析 + 优化计划 |
| `docs/plans/2026-02-17-agentteams-parallel-execution-implementation.md` | 并行执行方案 |
| `docs/plans/2026-02-17-claudecode-teams-replication-checklist.md` | Claude Teams 复刻清单 |
| `docs/上下文工程设计文档.md` | 上下文工程 |
| `docs/通用工具响应协议.md` | 工具协议规范 |
| `docs/fixed_update.md` | 早期 bug 修复记录 |
