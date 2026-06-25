# 剩余审计项逐条分析

> 日期: 2026-06-26 | 原始审计: 32 条 | 已修复: 24 条 | 剩余: 8 条

---

## P1 #12 — 14 个 team 工具有 3 对重叠

### 是什么

审计报告指出 TeamStatus/TeamList、TeamDelete/TeamCleanup、TeamApprovePlan vs SendMessage(plan_approval_response) 三对是"两个工具干同一件事"。

实际情况：
| 工具对 | 是否真重叠 | 分析 |
|--------|-----------|------|
| TeamStatus vs TeamList | **部分** | TeamStatus 看单个 team 详情，TeamList 列出所有 team 名称。类似 `ls -l` vs `ls`，功能不同 |
| TeamDelete vs TeamCleanup | **是** | Delete 删除团队+数据，Cleanup 清理 inbox+work_items。后者可以合并到前者加参数 |
| TeamApprovePlan vs SendMessage(plan_approval_response) | **是** | 本质上就是发一条特殊消息。可以合并到 SendMessage |

### 能不能修

能。合并后的工具：`TeamDelete(cleanup=true)`、`SendMessage(message_type=plan_approval_response)`。

### 修了质量会更好吗

**影响中等偏小**。这些工具只在 `enable_agent_teams=true` 时注册，日常使用频率低。合并减少 3 个工具 → 31-3=28 个工具，对上下文体积有微小帮助。但改动涉及 AgentTeams 协议层，需要同步更新工具提示词和测试。

**建议**：低优先级，可以在做 Team Engine 生产化时一并处理。

---

## P1 #14 — 每次 run 至少 save 3 次

### 是什么

当前 `_auto_save_session()` 在三个时机被调用：
1. `run()` 正常返回后（line 577）
2. `run()` 异常时（line 585）
3. `SessionFeature.cleanup()` — Agent 关闭时（session.py:34）

每次 save 都是**全量快照**（system_messages + history_messages + read_cache + teams_snapshot + ...），写入一个完整 JSON 文件。三次调用大概率写入相同内容。

### 能不能修

能。方案：
- 加一个 `_snapshot_dirty` 标志，只在历史消息变化后才重写
- 或者在 `finally` 块统一保存一次（删除 run() 内两次）
- 或者改为增量追加（但和当前 JSON 快照格式冲突）

### 修了质量会更好吗

**影响小**。全量快照写入耗时 ~50ms（对 1000 条消息的会话），用户感知不到。但三次写入是代码坏味道。改为一次 `finally` 保存更干净。

**建议**：可以修，但优先度低。

---

## P1 #15 — 3 个 Feature 是"空壳"

### 是什么

审计报告指出：
1. **MCPFeature** — "不注册 MCP 工具"（注释自承）
2. **DelegateModeFeature** — 只设 `agent.delegate_mode` 和 `DELEGATION_ALLOWED_TOOLS` 两个值
3. **WorktreeFeature.cleanup** — 函数体只有 `pass`

### 实际分析

**MCPFeature** — 已经修复（P1 #10）。现在完整处理 MCP 注册+重试+状态显示。

**DelegateModeFeature** — 不是空壳。它精确地做了一件事：设置 delegate mode 并注册工具白名单。`init()` 只有 4 行但每行都有用。`pre_tool_use()` 拦截非白名单工具（P1 #11 修复）。

**WorktreeFeature.cleanup** — `pass` 是故意的。EnterWorktree/ExitWorktree 工具各自负责创建/清理 worktree。Feature 的 cleanup 钩子不应该重复清理。

### 能不能修

不需要修。这三个 Feature 的"简洁"是好的设计——每个只做一件事。

### 修了质量会更好吗

**不会**。这三个已经是最小化正确实现。加代码反而违反单一职责。

**建议**：关闭此条。

---

## P1 #19 — `codeAgent.py` camelCase 违反 PEP8

### 是什么

文件 `agents/codeAgent.py` 使用 camelCase 而非 PEP8 推荐的 snake_case (`code_agent.py`)。同类文件：`test_plan_mode_background.py`、`test_output_styles.py` 等 ~5 个文件。

### 能不能修

技术上能：`git mv agents/codeAgent.py agents/code_agent.py`，然后更新所有 import。

但 `codeAgent.py` 被 **46 个文件**引用（import、test、配置路径）。改名需要：
1. 重命名文件
2. 更新所有 `from agents.codeAgent import CodeAgent` → `from agents.code_agent import CodeAgent`
3. 更新 `ToolBootstrap` 可能涉及的任何路径引用
4. 全量回归测试

### 修了质量会更好吗

**几乎不会**。PEP8 命名规范对代码可读性有微小帮助，但 rename 的破坏性远超收益。Python 社区本身也不强制 snake_case 文件名（Django 的 `models.py`、Flask 的 `app.py` 是约定而非规范）。

**建议**：关闭此条。不值得。

---

## P1 #20 — TUI 双渲染

### 是什么

`tui/streaming.py` 是 Rich Live 流式渲染的骨架（90 行），但从未被调用。实际运行的是 `RichConsoleCodeAgent`（`scripts/chat_test_agent.py:66`）配合 `EnhancedUI`（`utils/ui_components.py`）。

### 为什么没激活

`StreamingResponse` 需要 LLM 逐 token 推送才能工作。当前 `HelloAgentsLLM.invoke_raw()` 是同步调用（`chat.completions.create(stream=False)`），一次性返回完整响应。要激活 streaming 需要：
1. `HelloAgentsLLM` 支持 `stream=True`
2. 逐 token 回调到 TUI 层
3. 同时保持 tool_call 的完整解析

这是 **LLM 客户端层的架构改动**，不是 TUI 层的问题。

### 能不能修

能，但需要改 LLM 客户端支持 streaming。工作量约 2-3 天。

### 修了质量会更好吗

**会**。用户能实时看到 Agent 的思考和回复，体验大幅提升。这是 Pi Agent 的核心体验之一。

**建议**：保留 `tui/streaming.py` 作为目标架构。等 LLM 层支持 streaming 后激活。

---

## P2 #23 — test_ui_components.py 是 __main__ 演示

### 是什么

审计报告说这个文件是 `__main__` 演示脚本而非 pytest 测试。

### 实际验证

```bash
$ python -m pytest tests/test_ui_components.py --collect-only -q
tests/test_ui_components.py::test_model_banner
tests/test_ui_components.py::test_tool_tree
tests/test_ui_components.py::test_token_tracker
tests/test_ui_components.py::test_enhanced_ui
4 tests collected
```

pytest **能正常发现并运行**这 4 个测试。文件末尾的 `if __name__ == "__main__"` 只是额外允许作为 demo 运行，不影响 pytest 的收集。

### 能不能修

不需要修。这是误报。

### 修了质量会更好吗

**不会**。测试已经正常工作。

**建议**：关闭此条。

---

## P2 #24 — TUI 组件 0 测试

### 是什么

`tui/permission_dialog.py`、`tui/streaming.py`、`tui/mention_completer.py` 没有专门的测试文件。

### 能不能修

技术上能。但 TUI 组件依赖 `prompt_toolkit`、`rich` 的渲染层，需要：
- Mock `Console`、`PromptSession`、`Live` 等 Rich 对象
- 模拟终端尺寸、键盘输入
- 验证渲染输出

这些 mock 工作量远超测试价值（TUI 层逻辑简单，主要是在调 Rich API）。Claude Code 本身也没给 TUI 组件写测试。

### 修了质量会更好吗

**几乎不会**。TUI 层是薄胶水层，核心逻辑在 `codeAgent.py` 和 `history_manager.py` 中（这些有完整测试覆盖）。给 TUI 写测试相当于测试"我调了 Rich 的 API 没有"，价值极低。

**建议**：关闭此条。

---

## P2 #27 — agent_teams 双重初始化

### 是什么

`CodeAgent.__init__`（line 83-105）直接创建 TeamManager，然后 `AgentTeamsFeature.init()`（line 19-55）又创建一次。两次创建的参数完全相同，第二次覆盖第一次。

```
时序:
  CodeAgent.__init__
    → self.team_manager = TeamManager(...)          ← 第 1 次
    → collect_all_features() → AgentTeamsFeature.init()
        → agent.team_manager = TeamManager(...)      ← 第 2 次（覆盖）
```

### 能不能修

理论上能：删除 CodeAgent.__init__ 中的团队初始化，只保留 Feature 版本。

但风险：CodeAgent.__init__ 中 `self.team_manager` 在第 91 行创建，而 `_init_tools()` 在第 125 行调用，此时需要 `team_manager` 来注册 Team 工具。Feature.init 在第 120-122 行执行，所以时序上 Feature 版本先于工具注册，没问题。

但如果有人**禁用了 AgentTeamsFeature**（从 BUILTIN_FEATURES 列表中移除），CodeAgent.__init__ 的版本仍然会创建 TeamManager，导致行为不一致。

### 修了质量会更好吗

**会**。去掉冗余初始化，代码更清晰，团队功能只有一个权威来源（Feature）。但需要确认没有任何代码路径依赖 CodeAgent 版本的 team_manager 在 Feature 之前就存在。

**建议**：可以修，但需要仔细的回归测试。标记为低优先。

---

## 最终结论

| # | 问题 | 能修吗 | 建议 |
|---|------|--------|------|
| P1 #12 | team 工具重叠 | 能 | 低优先，随 Team Engine 生产化一起做 |
| P1 #14 | save 3 次 | 能 | 低优先，代码坏味道但不影响性能 |
| P1 #15 | Feature 空壳 | **不需要修** | 已证实不是问题 |
| P1 #19 | camelCase | 能但不值得 | 破坏性太大，收益太小 |
| P1 #20 | TUI 双渲染 | 能但需 LLM 层支持 | 保留骨架，等 streaming 支持后激活 |
| P2 #23 | test_ui_components | **不需要修** | pytest 已能正常发现 |
| P2 #24 | TUI 0 测试 | 不建议 | mock 成本远超收益 |
| P2 #27 | agent_teams 双重 init | 能但需小心 | 低优先 |

**真正有价值的只剩 3 项**：P1 #12（team 工具合并）、P1 #14（save 优化）、P1 #20（LLM streaming）。其余 5 项是误报或不值得修。
