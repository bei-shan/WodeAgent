# MyCodeAgent 优化实施总结

> 日期: 2026-06-26 | 来源: Pi Agent 学习 + 外部审计 + 架构重构  
> 测试: 937 passed, 0 failed | 修复审计项: 24/32

---

## 一、提示词瘦身 (Phase A)

### 做了什么

系统提示词从 ~19K tokens 压缩到 ~2.2K tokens，减少 88%。

### 为什么这么做

Pi Agent 的"减法思维"证明：前沿模型已经过大量 RL 训练来理解编码任务，不需要冗长的提示词。Pi 仅用 ~300 词提示词就在 TerminalBench 排名第 2。

### 怎么做的

1. **工具提示词按需注入**：给 `Tool` 基类加 `usage_notes` 属性（~50 字的使用建议），替代原来每个工具 2000-5000 字的完整提示词。这些大段文本的参数描述、响应格式已经在 OpenAI function calling schema 中存在，不需要重复发送给 LLM。

2. **L1 系统提示精简**：从 153 行压缩到 57 行。删除 6 个冗长的 `<example>` 块和重复的 "Available Tools" 区块。

3. **Late Binding**：`ContextBuilder` 改为实时组装而非预缓存。工具提示词从 `Tool.usage_notes` 实时获取（纯内存操作），MCP 工具连上后自动感知，不需要手动失效缓存。

---

## 二、会话树 (Phase B)

### 做了什么

借鉴 Pi Agent 的 JSONL 会话树设计，将线性历史升级为树形结构。

### 为什么这么做

Pi 的会话树是它最独特的设计：对话可以在任意历史节点分叉，支持"支线任务"而不污染主上下文。每个分支保存在单个 JSONL 文件中。

### 怎么做的

1. **Message 模型扩展**：每个消息增加 `message_id`（8 位短 ID）+ `parent_id`（父节点 ID，null=根）。

2. **HistoryManager 树操作**：
   - `fork(target_id)` — 从任意历史消息分叉
   - `get_current_branch()` — 沿 parent_id 链构建当前分支上下文
   - `navigate_to()` — 移动到目标节点，可选生成 branch_summary
   - `append_model_change()` / `append_thinking_change()` — 记录会话状态变化

3. **JsonlSessionStore**：Pi 兼容的 JSONL 存储格式。单个文件，每行一个条目，追加写入。

4. **SessionStore v2**：快照增加 `cursor_id`、`history_entries`、`labels`、`current_model`、`thinking_level` 字段，向后兼容 v1。

5. **新命令**：`/tree`（ASCII 树形图）、`/fork <id>`（分叉）、`/thinking on|off`（思考深度）。

---

## 三、耦合度优化

### 做了什么

消除 CodeAgent 对 33 个工具类的硬编码依赖，统一 55 项环境变量到 Config。

### 为什么这么做

CodeAgent 有 46 个 import 是全项目耦合度最高的文件。新增工具需要改 2 处（import + register 调用）。55 个 `os.getenv()` 散落在 19 个文件中。

### 怎么做的

1. **ToolBootstrap — 工具自动发现**：DI 容器模式。扫描 `tools/builtin/` 目录，识别所有 Tool 子类。根据构造函数参数名自动注入依赖（`bootstrap.provide("team_manager", ...)`）。新增工具零改动 CodeAgent。

2. **Config 统一**：所有 `os.getenv()` 收敛到 `Config.from_env()`。配置通过属性访问（`config.tool_output_max_lines`），带 `os.getenv()` fallback 向后兼容。

3. **配置注入**：8 个模块通过 `set_xxx_config(config)` 接收 Config，优先使用 config 属性，回退到 os.getenv()。

---

## 四、安全与可靠性修复

### 做了什么

修复了 7 个 P0 致命问题 + 8 个 P1 高优问题。

### P0 修复

| 修复 | 为什么重要 |
|------|-----------|
| **trace_logger token 累加死代码** | `return` 后面的 token 累加代码永远不会执行 → 所有 session 的 total_usage 永远是 0 |
| **CircuitBreaker 误熔断** | INVALID_PARAM 也被计入 failure → 模型连传错参数会熔断整个工具 |
| **Team Engine heartbeat** | worker 崩溃后 work_item 永久卡 running → 加 heartbeat_ts + 后台 sweep 线程 |
| **Bash 截断** | 10MB 输出不截断 → ObservationTruncator 统一处理 |
| **Bash 沙箱** | `python -c "open(...)"` 可绕过软沙箱写文件 → 新增 python 写操作检测 |

### 如何系统修复

采用"批量设计→实施→测试"的工作流程：先分析根因（读源码+审计报告），写出完整设计方案，逐文件实施，每次改动都跑全量 937 测试确认无回归。

---

## 五、架构重构

### 做了什么

1. **Feature 协议**：11 个 Feature 按 order 排序，3 个生命周期钩子（init/post_init/cleanup），4 个运行时钩子（runtime_blocks/pre_tool/post_tool/llm_intercept）。

2. **DelegateModeFeature 拦截统一**：将 CodeAgent 硬编码的 delegate mode 检查移到 Feature.pre_tool_use()，统一拦截路径。

3. **MCPFeature 接管 MCP 生命周期**：注册 + 重试 + 状态显示全部从 CodeAgent 搬到 Feature。

4. **RuntimeView 抽取**：105 行 `_format_runtime_system_blocks` 从 CodeAgent 抽到 `core/team_engine/runtime_view.py`。

5. **SlashCommandRegistry**：530 行 if-elif 链替换为命令注册表。新增命令只需 `registry.register("/foo", handler)`。

---

## 六、代码卫生

| 修复 | 效果 |
|------|------|
| 删除 `tool_result_compressor.py` | 弃用 re-export 文件，30 行 |
| 删除 `Agent._history` dead state | 8 行死数据 |
| 删除 `Config.max_history_length` | 死字段，从未被读取 |
| Plugin loader 异常日志 | `try/except: pass` → `except as exc: logger.warning(...)` |
| Subagent prompt 去重 | 4 个 prompt 共享 `SUBAGENT_BASE_RULES`，减少 60 行重复 |
| ALWAYS_IGNORE 共享常量 | Grep/Glob/LS 三处重复 → `core/constants.py` |
| VCR `once` 模式实现 | `new_episodes`=录+存，`once`=用但不存，`none`=不调 API |
| @skill 自动补全 | MentionCompleter 新增 `get_skills` 回调 + `@skill:` 补全 |
| 工具描述内联 | 33 个 prompt 文件不再被 `ContextBuilder` 加载，描述直接写在工具类中 |

---

## 七、关键设计决策

| 决策 | 理由 |
|------|------|
| **不砍工具** | MyCodeAgent 是学习框架，31 个工具本身就是学习成果。Pi 的 4 工具极简是产品哲学 |
| **不引入 tiktoken** | 太重，OpenAI 专有，对 DeepSeek/Qwen/Kimi 不适用。改用 `max(chars//3, chars//2)` 保守估算 |
| **不改成 TypeScript** | Python 生态是根基。Feature 协议是 Python 专属的可插拔设计 |
| **保留软沙箱** | Pi 的 YOLO 模式+容器是另一方向。软沙箱+乐观锁是更有价值的"学习材料" |
| **保留子代理 + AgentTeams** | 多 Agent 协作是核心学习目标。Pi 用 Bash 自我调用替代子代理是为了极简 |
| **保留 MCP** | MCP 是行业标准协议，集成它本身就是重要学习成果 |
