# Pi Agent 设计哲学学习 + MyCodeAgent 优化规划

> 日期: 2026-06-23 | 来源: Pi Agent (Mario Zechner) 设计哲学研究

---

## 一、Pi Agent 设计哲学核心梳理

### 1.1 "减法思维" — 最核心的设计原则

Pi 的基本论点是：**前沿模型已经经过大量 RL 训练来理解编码任务，它们不需要 10,000+ token 的系统提示词，也不需要 50+ 工具。** 在 TerminalBench 上，Pi 用 4 个工具 + ~300 词的提示词排名第 2，证明了"少即是多"。

### 1.2 四个核心设计决策

| 决策 | Pi 的做法 | 背后的推理 |
|------|----------|-----------|
| **仅 4 工具** | Read / Write / Edit / Bash | Bash 已足够强大，能覆盖 Grep/Glob/LS 等功能。工具越少，模型理解越精准 |
| **极短提示词** | ~300-1000 词 | 模型已经 RL 训练过，大段指令反而干扰模型判断 |
| **无子代理** | 用 Bash 自我调用来替代 | "黑洞里的黑洞" — 上下文无法传递，调试困难 |
| **无 MCP** | CLI 工具通过 Bash 执行 | MCP 工具描述占 7-9% 上下文窗口，而 CLI 工具不需要额外描述 |

### 1.3 三个技术创新

**1. 会话树 (Session Tree)**
```
session.jsonl:
  root ── branch-A ── branch-A1
       └── branch-B
```
- 会话不是线性的，而是**树形**的
- 可在任意历史节点分叉探索
- 所有分支存在**单个 JSONL 文件**中
- `/tree` 命令可视化整个会话树

**2. 自我扩展 (Self-Extension)**
- 不下载扩展 → **让 Agent 自己写扩展**
- TypeScript 扩展 + 热重载 → 修改立即生效
- Agent 可以"自我进化"

**3. Late Conversion（延迟转换）**
- 内部用强类型 `AgentMessage`
- 只在调用 LLM 边界时才转换为 LLM 消息格式
- 避免在两种格式间反复转换

### 1.4 "反特性"清单（刻意不做的事）

| 排除的特性 | 替代方案 |
|-----------|---------|
| 无 Plan Mode | 用 `PLAN.md` 文件 — 可版本控制、可共享 |
| 无子代理 | 用 Bash 自我调用来替代 |
| 无 MCP | CLI 工具通过 Bash 执行 |
| 无 maxSteps | 循环自然结束 |
| 无权限门控 | YOLO 模式 + 容器隔离实现真正安全 |
| 无内存层 | "代码即真相" — 维护代码状态 |
| 无 LSP | 增量编辑时类型检查给误导性信号 |
| 无预置扩展 | 让 Agent 自己写扩展 |

---

## 二、MyCodeAgent 现状评估

### 2.1 提示词体积分析

```
L1 system prompt:     ~4,082 tokens  (12,247 chars)
所有工具提示词:       ~14,871 tokens  (44,613 chars)
─────────────────────────────────────────────
合计 (每次 LLM 调用): ~18,953 tokens
```

**问题**：每次 LLM 调用都要消耗 ~19K tokens 在系统提示上。对于 128K 上下文窗口来说，这是 ~15%。如果用户有中等长度的对话，剩余的 ~109K tokens 要分配给对话历史和回复。

### 2.2 工具数量分析

| 类别 | 数量 | 工具 |
|------|------|------|
| 文件操作 | 6 | Read, Write, Edit, MultiEdit, LS, Glob |
| 搜索 | 1 | Grep |
| 系统 | 4 | Bash, TodoWrite, AskUser, Skill |
| 子代理 | 2 | Task, TaskOutput |
| 计划模式 | 2 | EnterPlanMode, ExitPlanMode |
| Worktree | 2 | EnterWorktree, ExitWorktree |
| 模型切换 | 1 | SwitchModel |
| AgentTeams | 15 | TeamCreate, SendMessage, ... |
| MCP (外部) | 0-5 | fetch, context7, tavily-mcp |
| **合计** | **31-36** | |

Pi 只有 4 个工具。但这并不意味着我们应该砍到 4 个 — MyCodeAgent 是一个**学习框架**，探索多种工具模式本身就是目标之一。

### 2.3 与 Pi 的对齐与差异

| 维度 | Pi | MyCodeAgent | 评价 |
|------|-----|-------------|------|
| 哲学定位 | 极简生产工具 | 学习实验框架 | **定位不同，无需完全模仿** |
| 工具数 | 4 | 31 | 学习框架需要更多探索 |
| 提示词 | ~300 词 | ~19K tokens | ⚠️ **需要优化** |
| 子代理 | Bash 自我调用 | Task 工具 | ✅ 设计合理，学习价值高 |
| 会话结构 | 树形 | 线性 + 快照 | ⚠️ **树形结构值得借鉴** |
| 扩展系统 | TS 热重载 | Python Feature 协议 | ✅ 两者都是可插拔设计 |
| 上下文压缩 | 无（不需要） | HistoryManager + Summary | ✅ 适合长对话场景 |
| 安全 | YOLO + 容器 | 软沙箱 + 乐观锁 | ✅ 两个方向，都有价值 |
| Plan Mode | PLAN.md 文件 | EnterPlanMode 工具 | ⚠️ **文件方式更简洁** |

---

## 三、优化计划

### Phase A: 提示词瘦身 🎯 P0

**目标**：将 L1 系统提示词从 ~19K tokens 降到 ~5K tokens 以下。

**具体措施**：

1. **工具提示词按需注入** (Step A1)
   - 当前：所有 31+ 工具的完整提示词每次都注入 L1
   - 改为：只注入 OpenAI function calling schema（已包含工具描述），删除冗余的 "Available Tools" 文本块
   - 预估节省：~12K tokens

2. **L1 提示词精简** (Step A2)
   - 当前 153 行 (~4K tokens)，包含大量示例和冗余规则
   - 保留核心：ReAct 循环规则、安全规则、工具使用规则
   - 删除冗长的 `<example>` 块（6 个示例可以删到 2 个）
   - 预估节省：~2K tokens

3. **Plan Mode 引导条件化** (Step A3)
   - 当前：Plan Mode 引导总是在 L1 末尾
   - 改为：只在 EnterPlanMode 工具注册时注入，且更简洁
   - 已实现 ✅（`_append_plan_mode_guidance` 已做条件注入）

### Phase B: 会话树 🎯 P1

**目标**：将线性 HistoryManager 升级为树形会话结构。

**Pi 的 Session Tree 设计要点**：
```
每个消息都有 parent_id → 自然形成树
/tree 命令可视化
可以在任意节点分叉 → 新分支复用父节点的上下文
单个 JSONL 文件存储整棵树
```

**具体措施**：

1. **HistoryManager 支持分支** (Step B1)
   - 给每条消息加 `parent_id` 字段
   - `fork()` 方法创建新分支
   - 读取某条消息时，沿 `parent_id` 链回溯构建上下文

2. **SessionStore 支持树形结构** (Step B2)
   - 快照格式增加 `parent_message_id` 和 `branch_name`
   - `/tree` 命令显示会话树
   - `/branch <name>` 创建新分支

3. **分支导航** (Step B3)
   - 在 TUI 中支持 `/tree` 和 `/branch` 命令
   - 分支切换时保留共享上下文

### Phase C: Plan Mode 文件化 🎯 P1

**目标**：将 Plan Mode 从"注入到上下文"改为"写入 PLAN.md 文件"。

**Pi 的做法**：
```
用户: "帮我实现 XXX"
Agent: 1) 读取代码 → 2) 写 PLAN.md → 3) 按计划执行
PLAN.md 可以版本控制、可以分享、可以在后续对话中重新加载
```

**当前 MyCodeAgent 的做法**：
```
EnterPlanMode → 只读工具 → ExitPlanMode(plan="...") → 注入到 system prompt
```

**改进方案** (Step C1)：
- ExitPlanMode 将 plan 写入 `PLAN.md` 文件（而非仅注入上下文）
- Plan 文件持久化，可被后续对话引用
- 保留 `_plan_text` 注入作为快速路径（小计划直接注入）

### Phase D: 系统提示词 Late Binding 🎯 P2

**目标**：借鉴 Pi 的 Late Conversion 模式，将 LLM 消息格式转换推到最后一刻。

**当前流程**：
```
ContextBuilder._get_system_messages() → 构建完整 system prompt 文本 → 缓存
每次 ReAct step → 复用缓存（如果有 runtime blocks 则追加）
```

**改进方案**：
- ContextBuilder 内部存储结构化数据（Tool 对象、Skill 列表、MCP 列表）
- 只在 `build_messages()` 时序列化为 LLM 消息格式
- 工具变化时自动失效缓存

### Phase E: 工具提示词生成优化 🎯 P2

**目标**：工具描述不再单独生成大段文本，而是复用 OpenAI function calling schema。

**当前流程**：
```
每个工具:
  1. Tool.to_dict() → function calling schema (JSON)   ← LLM 需要
  2. prompts/tools_prompts/*.py → 文本描述             ← 也在 L1 中注入
```

**问题**：第 2 步的文本描述与第 1 步的 schema 描述**高度重复**。

**改进方案** (Step E1)：
- 工具描述只从 `Tool.to_dict()` 的 `description` 字段生成
- 删除 `prompts/tools_prompts/*.py` 中的冗余描述
- 只保留 schema 中无法表达的"使用建议"（如 Task 工具的 `subagent_type` 说明）

---

## 四、实施优先级

```
Phase A (提示词瘦身) ──────────── P0, 预计节省 ~14K tokens
    ├── A1: 工具提示词按需注入     (~12K tokens 节省)
    ├── A2: L1 精简               (~2K tokens 节省)
    └── A3: Plan Mode 条件化       (已实现 ✅)

Phase B (会话树) ──────────────── P1, 新增能力
    ├── B1: HistoryManager 分支支持
    ├── B2: SessionStore 树形结构
    └── B3: TUI 分支导航

Phase C (Plan Mode 文件化) ────── P1, 改变行为
    └── C1: ExitPlanMode 写文件 + 可选注入

Phase D (Late Binding) ────────── P2, 架构优化
    └── D1: ContextBuilder 结构化存储

Phase E (工具提示词优化) ──────── P2, 进一步瘦身
    └── E1: 删除冗余的工具提示词文本
```

---

## 五、不做的事情（明确范围外）

- **不砍工具** — MyCodeAgent 是学习框架，31 个工具本身就是学习成果
- **不改 YOLO 模式** — 软沙箱是我们的特色，Pi 的容器方案是另一个方向
- **不删除子代理** — Task 工具是核心学习目标之一
- **不删除 AgentTeams** — 多 Agent 协作是重要实验方向
- **不改成 TypeScript** — Python 生态是我们的根基
- **不删除 MCP** — MCP 集成是重要学习成果

---

## 六、Pi 哲学可以如何适配 MyCodeAgent

核心原则：**"学习框架"不是"过度设计"的借口，但也不是"极简主义"的约束。**

可以借鉴的：
1. **提示词瘦身** — 直接可用，不影响任何学习目标
2. **会话树** — 增强学习体验，探索新的会话模式
3. **Plan Mode 文件化** — 更贴近真实开发流程
4. **Late Binding** — 架构优化，代码更清晰

不需要照搬的：
1. 4 工具极简 — 学习框架需要探索更多工具模式
2. YOLO 模式 — 软沙箱是安全学习的优秀实践
3. 无子代理 — Task 工具是核心学习目标
4. 无 MCP — MCP 协议是重要的行业标准
