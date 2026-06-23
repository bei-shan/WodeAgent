# Kode-Agent 学习与优化计划

> 日期：2026-06-18
> 来源：深度分析 D:\agent_devlop\Kode-Agent (v2.0.2)
> 目标：从 Kode-Agent 提取可移植的优秀设计模式，融入 MyCodeAgent

---

## 零、MyCodeAgent 已有功能对照

为避免重复工作，先列出 MyCodeAgent **已经具备**的 Kode-Agent 同等能力：

| Kode-Agent 功能 | MyCodeAgent 对应 | 状态 |
|-----------------|-----------------|------|
| TaskTool (subagent delegation) | TaskTool (oneshot/persistent/parallel) | ✅ |
| TodoWriteTool | TodoWriteTool | ✅ |
| MultiEditTool | MultiEditTool | ✅ |
| FileRead/Write/Edit | Read/Write/Edit | ✅ |
| Glob/Grep | Glob/GrepTool | ✅ |
| BashTool | BashTool (加固版) | ✅ |
| AskUserQuestion | AskUserTool | ✅ |
| SkillTool | SkillTool | ✅ |
| MCP integration | MCP client/loader | ✅ |
| Session persistence | session_store.py | ✅ |
| Permission system | PermissionGate (soft sandbox) | ✅ |
| EnterWorktree/ExitWorktree | ✅ (刚实现) | ✅ |
| AgentTeams (team orchestration) | AgentTeams | ✅ |
| Context compression | HistoryManager + SummaryCompressor | ✅ |
| System reminders injection | Runtime system blocks | ✅ |

**结论：MyCodeAgent 在核心工具链和团队协作上已接近 Kode-Agent。** 差距主要在高级功能（Plan Mode、Background Execution、Model Pointers）和体验层。

---

## 一、建议引入的优化点

### P0 — 高价值低投入（立即实施）

#### 1. Plan Mode (EnterPlanMode / ExitPlanMode)

**Kode-Agent 做法：**
- `EnterPlanMode`：切换到计划模式，LLM 只允许使用 Read/Grep/Glob/LS 等只读工具
- 计划模式下 LLM 产出一个结构化计划文本
- `ExitPlanMode(plan=<text>)`：将计划注入到系统提示词中，恢复全部工具访问

**MyCodeAgent 实现方案：**
- 新增 `EnterPlanModeTool` / `ExitPlanModeTool`
- `EnterPlanMode` 设置 `CodeAgent._in_plan_mode = True`，修改 `_get_openai_tools_for_current_mode()` 只返回只读工具
- `ExitPlanMode(plan=<plan_text>)` 将 plan 注入 system prompt，恢复全部工具
- 类似 delegate_mode 的工具过滤机制

**预计文件：**
- `tools/builtin/enter_plan_mode.py` / `exit_plan_mode.py`
- `prompts/tools_prompts/enter_plan_mode_prompt.py` / `exit_plan_mode_prompt.py`
- `agents/codeAgent.py` (修改 `_get_openai_tools_for_current_mode()`)

**价值：** 允许 LLM 先做只读分析再执行，减少错误操作。实现成本低（复用已有的工具过滤机制）。

---

#### 2. Background Task Execution（子代理异步执行）

**Kode-Agent 做法：**
- `TaskTool` 支持 `run_in_background: true` 参数
- 后台子代理独立运行，主 Agent 继续执行
- 结果通过 `TaskOutput(task_id)` 异步获取
- 后台进程有独立的 notification/status

**MyCodeAgent 实现方案：**
- `TaskTool` 新增 `run_in_background=True` 参数
- 使用 `threading.Thread(daemon=True)` 运行后台子代理
- 新增 `TaskOutputTool`：查询后台任务的状态和结果
- `TaskList` 工具：列出所有后台任务
- 结果存储：`.tasks/output/task_{id}.json`

**预计文件：**
- `tools/builtin/task.py` (+background 模式)
- `tools/builtin/task_output.py` (新)
- `tests/test_task_background.py`

**价值：** 允许并行执行多个子代理查询，显著提升多步骤任务的效率。

---

### P1 — 中等投入高价值

#### 3. Model Pointer System（模型指针系统）

**Kode-Agent 做法：**
- 定义 `main`、`task`、`compact`、`quick` 四个指针
- 每个指针映射到具体的模型 profile
- 不同场景使用不同模型：主对话用 `main`，子代理用 `task`，压缩用 `compact`，快速判断用 `quick`

**MyCodeAgent 实现方案：**
- 扩展 `Config` 类支持 `model_pointers` 配置
- 环境变量：`MODEL_POINTER_MAIN`、`MODEL_POINTER_TASK`、`MODEL_POINTER_COMPACT`
- `HelloAgentsLLM` 支持从 pointer 解析实际模型

**价值：** 解耦模型选择和代码逻辑，允许用户灵活替换模型而无需改代码。

---

#### 4. WebFetch / WebSearch Tools（网络搜索工具）

**Kode-Agent 做法：**
- `WebFetch(url)`：获取网页内容，支持 HTML→Markdown 转换
- `WebSearch(query)`：搜索引擎查询，返回结果摘要

**MyCodeAgent 实现方案：**
- 新增 `WebFetchTool`：使用 `requests`/`httpx` 抓取 URL，`html2text` 转 Markdown
- 新增 `WebSearchTool`：集成 DuckDuckGo API（免费）或 Google Custom Search
- 安全：URL 白名单/黑名单，内容大小限制

**价值：** 赋予 Agent 实时信息获取能力，不再局限于训练数据。

---

### P2 — 体验层优化

#### 5. Tool Aliasing（工具别名）

**Kode-Agent 做法：**
- `Read` → `FileRead`，`Write` → `FileWrite` 等
- `resolveToolNameAlias()` 统一解析

**现有状态：** MyCodeAgent 已经使用简短名称（Read/Write/Edit/Bash），不需要别名。此条跳过。

---

#### 6. Output Styles（输出风格）

**Kode-Agent 做法：**
- `default` / `Explanatory` / `Learning` 三种输出风格
- 通过 Markdown 文件配置自定义风格
- 风格影响 system prompt 中的输出指令

**MyCodeAgent 实现方案：**
- 新增 `output_styles/` 目录
- 环境变量 `AGENT_OUTPUT_STYLE=default|explanatory|learning`
- 风格文件通过 `load_env` 读取，注入 system prompt

**价值：** 适配不同用户需求（学习者需要详细解释，专家需要简洁输出）。

---

### P3 — 长期规划

#### 7. LSP Integration（语言服务器集成）

- 需要 LSP client 库，复杂度高
- 提供 go-to-definition、find-references、hover 等代码导航能力
- 可先做 Python LSP（最常用），再扩展到其他语言

#### 8. Hook System（生命周期钩子）

- PreToolUse / PostToolUse / Stop / SessionStart / SessionEnd
- 允许外部脚本（Python）注册为钩子
- 用于 CI/CD、自定义验证、工作流自动化

#### 9. VCR Test Recording（API 录制回放）

- 录下 LLM API 的 response，存储为 fixture
- 测试时回放 fixture，无需真实 API 调用
- 快速、确定性、零成本

---

## 二、实施顺序

```
Phase 1 (本迭代):
  1.1 Plan Mode (EnterPlanMode/ExitPlanMode)  ← P0
  1.2 Background Task Execution               ← P0

Phase 2 (下迭代):
  2.1 Model Pointer System                    ← P1
  2.2 WebFetch / WebSearch                    ← P1

Phase 3 (体验):
  3.1 Output Styles                           ← P2

Phase 4 (长期):
  LSP / Hook System / VCR                     ← P3
```

---

## 三、不做引入的功能（理由）

| Kode-Agent 功能 | 理由 |
|----------------|------|
| ACP 协议 | MyCodeAgent 无 IDE 集成需求 |
| Native binary distribution | 纯 Python 项目，pip install 足够 |
| Plugin marketplace | 规模不够，Skill 系统已覆盖基本需求 |
| React/Ink TUI | 改造成本极高，收益有限 |
| Jupyter notebook 编辑 | MyCodeAgent 的核心场景不是数据分析 |
| @-mention 系统 | 依赖 TUI，与 MyCodeAgent 的文本界面不匹配 |
| bwrap 沙箱 | Linux-only，MyCodeAgent 需要跨平台 |
| Binary feedback | Anthropic 专有功能 |
| Claude Desktop MCP import | Claude 专有 |
