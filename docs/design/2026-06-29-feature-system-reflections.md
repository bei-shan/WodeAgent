# Feature 系统反思：跨项目对比之后的改进点

**日期**：2026-06-29
**触发**：用户提问"我们这个 feature 是什么意思，为什么这么选" → 顺手把 MyCodeAgent 的 `AgentFeature` 跟 Hermes-agent / Pi-coding-agent / Kode-Agent 三个参考项目做了一次横向对比扫描（基于实际源码，不是道听途说）。
**结论**：我们的架构形态最像 Pi 的 Extension 思路，比 Hermes / Kode 都更彻底地把横切关注点抽干净了。但有 4 个"可以抄"的优点和 4 个"可以收"的过度抽象，记在这里待办。

---

## 1. 当前 AgentFeature 协议复盘

7 个 lifecycle hook：

```python
class AgentFeature:
    order: int  # 排序权重，越小越先 init

    def init(self, agent) -> None: ...
    def post_init(self, agent) -> None: ...
    def runtime_blocks(self, agent, step) -> list[str]: ...
    def pre_tool_use(self, agent, tool_name, tool_input) -> dict | None: ...
    def post_tool_use(self, agent, tool_name, tool_input, result) -> list[str]: ...
    def llm_intercept(self, agent, messages, tools, tool_choice, fallback) -> Any: ...
    def cleanup(self, agent) -> None: ...
```

11 个内建 feature（order 20→100）：WorktreeFeature / MCPFeature / AgentTeamsFeature / DelegateModeFeature / BudgetFeature / PlanModeFeature / BackgroundTaskFeature / OutputStyleFeature / HookFeature / VCRFeature / SessionFeature。

主循环对 `self._features` 三种统一迭代：
- `runtime_blocks` 正向拼接 → 喂进系统提示
- `llm_intercept` reversed 套娃 → VCR 落最外层
- `pre/post_tool_use` 正向短路 → DelegateMode / PlanMode 在这里拦截

PluginLoader 复用同一个 ABI，把 `.mycode/plugins/<name>/` 里的 hooks / skills / output_styles / 自定义 Python feature 用三个 wrapper（`_PluginHookFeature@86` / `_PluginSkillFeature@15` / `_PluginOutputStyleFeature@81`）适配进协议。

---

## 2. 横向对比（基于源码扫描，不是 README）

| 维度 | MyCodeAgent | Hermes-agent | Pi-coding-agent | Kode-Agent |
|---|---|---|---|---|
| **扩展机制** | AgentFeature ABC（11 内建 + plugin wrapper），uniform order | PluginManager + register(ctx)，god class 不可继承 | ExtensionAPI（事件订阅 + 注册回调），无基类 | 目录式 plugin + JSON hook（**不能加 LLM 工具**） |
| **lifecycle hooks** | **7 个** | **17 个**（最多但大半 observer） | **30+ 个**（最细，强类型） | **7 个**（仅 hook，主循环 hardcoded） |
| **主类规模** | `codeAgent.py` ~1.3k 行，`__init__` ~40 行 | `run_agent.py` ~4100 行 god class | `agent-session.ts` 拆 + extension 系统 | `query.ts` 中央循环 |
| **工具表组装** | ToolBootstrap 自动发现 + DI 注入 feature provider | 模块 import 自注册 + toolset allow/deny | union(内建+extension+custom) + name allow/deny | 静态 import + per-context 过滤 |
| **mode 过滤** | **真过滤**（Plan/Delegate 在 pre_tool_use 拦） | 无 mode，plan 靠 toolset 名近似 | 无 mode 概念 | **假过滤**（plan 只改 permission + prompt） |
| **LLM 自主切换模型** | **否**（同三家） | 否 | 否 | 否（但有 `AskExpertModel` 咨询其他模型） |
| **上下文压缩** | hardcoded 在 `_init_core` | **可被 plugin 替换**（`register_context_engine`） | `session_before_compact` 事件 | hardcoded |

**架构形态归属**：MyCodeAgent 跟 **Pi 的 Extension 思路** 最像——uniform 接口 + 事件式 hook + 主循环退化为对 feature 的迭代。但我们用 Python ABC + order，比 Pi 的 TypeScript 事件订阅更显式、更可测（每个 feature 是一个类，单独 pytest）。Hermes 是反例：hook 多达 17 个但主类没拆，加新内建能力还是要改 god class。

---

## 3. 待办：值得抄过来的优点（4 项）

### 3.1 Hermes 的 transform_* hooks → 我们的 `post_tool_use` 应能改 result 本身

**现状**：`post_tool_use` 返回 `list[str]`，只能附加 system_messages，**不能改 result**。

**Hermes 做法**：
- `transform_tool_result(tool_name, result) -> result` —— 可重写工具输出（脱敏、压缩、注释）
- `transform_terminal_output(text) -> text` —— 改终端显示
- `transform_llm_output(content) -> content` —— 改 LLM 文本输出

**改造方案**：扩 `AgentFeature` 协议，加一个返回值：
```python
def post_tool_use(self, agent, tool_name, tool_input, result) -> PostToolResult:
    # PostToolResult = {"system_messages": list[str], "rewritten_result": dict | None}
    ...
```
向后兼容：原来返回 `list[str]` 的视为 `{"system_messages": ..., "rewritten_result": None}`。

**用例**：HookFeature 可以让 `.mycode/hooks.json` 里的脚本输出工具结果改写版（敏感字段脱敏、超长输出折叠成 ... + reference）；VCR 可以注入 mock 结果而不必从 LLM 层拦。

**优先级**：P2（没强需求就别动协议）。

### 3.2 Hermes 的 `register_context_engine` → ContextEngineFeature

**现状**：`core/context_engine/{history_manager, context_builder, summary_compressor}` 是 hardcoded 在 `CodeAgent._init_core()` 的，没有 feature 化，plugin 没法替换。

**Hermes 做法**：`PluginContext.register_context_engine(name, factory)` 让插件整体替换 ContextCompressor。多个 plugin 注册时按 priority 选。

**改造方案**：抽 `ContextEngineFeature`，order=10（最先 init，先于 worktree 等）：
```python
class ContextEngineFeature(AgentFeature):
    order = 10
    def init(self, agent):
        agent.history_manager = self.build_history_manager()
        agent.context_builder = self.build_context_builder()
        agent.summary_compressor = self.build_summary_compressor()
```
默认实现内建；plugin 可继承覆盖。

**用例**：研究"用别的 LLM 做摘要"、"换 Sliding Window 替代 LLM summary"、"分支感知压缩"时不必动主类。

**优先级**：P2（动核心，得想好向后兼容）。

### 3.3 Pi 的 `model_select` / `thinking_level_select` 事件

**现状**：模型切换是 `/model` slash → `agent.switch_model()` 直接调，**没有事件让 feature 反应**。

**Pi 做法**：每次 `setModel()` 发 `model_select` 事件，extension 可监听做 budget 清零、context window 调整、output style 跟模型变。

**改造方案**：`CodeAgent.switch_model()` 末尾加一句 `self._dispatch_feature_event("model_changed", old, new)`，AgentFeature 加一个可选 hook：
```python
def on_model_changed(self, agent, old_model: str, new_model: str) -> None: ...
```
- BudgetFeature 切到便宜模型时给 token 池补充
- OutputStyleFeature 跟随模型 family 切风格
- AgentTeamsFeature 通知 workers 切到新模型

**优先级**：P1（直接给 budget / output_style 解锁实用能力）。

### 3.4 Hermes 的 `kind: standalone/backend/exclusive/platform/model-provider` 分类

**现状**：所有 Feature 平等，没有"独占"语义。如果两个 plugin 都想替换 ContextEngine，没有冲突检测。

**Hermes 做法**：plugin manifest 声明 `kind`，`exclusive` 类型同名只允许一个，安装时检测。

**改造方案**：跟 §3.2 一起做。`ContextEngineFeature` 标记 `kind = "exclusive"`，PluginLoader 加载时检测，多个就报错。

**优先级**：P2（先有 §3.2 才有这个需求）。

---

## 4. 待办：可以收的过度抽象（4 项）

### 4.1 `cleanup(agent)` 协议是 dead code — 选一个方向收掉

**现状**：协议表面有 `cleanup(self, agent)`，但 `CodeAgent.close()` **没真的迭代 `self._features` 调它**——HookFeature/SessionFeature.cleanup 的等价逻辑是被 close 直接写死调用的。

**两种修法**：
- **A**（推荐）：让 close 真的迭代 features 调 cleanup，把 inline 的 SessionEnd / team shutdown / mcp close 搬进各自 feature.cleanup
- **B**：把 cleanup 从协议里删掉，文档明说 close 是 hardcoded teardown

**我倾向 A**——更对称，新 feature 加 teardown 不用改主类。但 B 更小改动。

**优先级**：P1（dead code 风险，且简单）。

### 4.2 DelegateModeFeature 和 PlanModeFeature 重复度极高

**现状**：两者都是"mode 标志 + 工具白名单 + pre_tool_use 检查"。

**改造方案**：
```python
class ToolWhitelistFeature(AgentFeature):
    """Base: mode flag + tool allowlist + pre_tool_use gate."""
    mode_attr: str   # "delegate_mode" or "_in_plan_mode"
    allowlist: set[str]

class DelegateModeFeature(ToolWhitelistFeature):
    mode_attr = "delegate_mode"
    allowlist = {"TeamCreate", "SendMessage", ...}

class PlanModeFeature(ToolWhitelistFeature):
    mode_attr = "_in_plan_mode"
    allowlist = {"Read", "Grep", ...}
```

**优先级**：P3（重复但稳定，不痒不痛）。

### 4.3 runtime_blocks 没有 priority 机制

**现状**：runtime_blocks 是 `list[str]`，feature 之间无法决定块的相对顺序，全靠 feature.order 间接决定。

**潜在问题**：如果将来要做"team 事件块必须在 budget 状态块之前"或"plan 提示必须在所有其他 runtime 块之上"，目前没法。

**改造方案**：先不动。如果将来真出现顺序约束，改成 `list[tuple[int, str]]` 加排序，或者协议改成 `dict[str, str]` 让框架按固定 section 名拼。

**优先级**：P4（YAGNI，记下来等需求驱动）。

### 4.4 llm_intercept 只有 VCR 一个用户，是否值得保留？

**现状**：`llm_intercept` 用 reversed 迭代套娃语义，比其他 6 个 hook 都复杂。**唯一用户是 VCRFeature**。

**两种方向**：
- 等等看：如果未来加 LLM 缓存 / 中间件 / 多模型 fallback chain，这套 hook 直接就能用
- 退化：改成 `agent._llm_wrapper: Optional[Callable]` 一个简单字段，把协议从 7 减到 6，VCR 直接挂到这个字段上

**我倾向等等看**——Hermes 的 fallback chain 就是这种 use case，挺有可能补一个。但要记在心里：**这个 hook 已经存在 6 个月，仍是孤儿**，明年这时候还没第二个用户，就该收掉。

**优先级**：P4（观察项）。

---

## 5. 行动建议（按 ROI 排序）

| 优先级 | 项 | 工作量 | 收益 |
|---|---|---|---|
| **P1** | §4.1 cleanup dead code 修复（方向 A） | 半天 | 消除协议骗局，新 feature teardown 路径清晰 |
| **P1** | §3.3 on_model_changed 事件 | 半天 | 立刻给 budget / output_style 解锁实用响应 |
| **P2** | §3.1 post_tool_use 改 result | 1 天 + 兼容期 | HookFeature 用户可写脱敏脚本 |
| **P2** | §3.2 ContextEngineFeature | 2-3 天 + 兼容期 | 解锁压缩策略实验 |
| **P2** | §3.4 plugin kind 分类（跟 §3.2 一起） | 0.5 天（跟 §3.2 合并） | 防 plugin 冲突 |
| **P3** | §4.2 ToolWhitelistFeature 基类 | 半天 | 代码漂亮，无功能影响 |
| **P4** | §4.3 runtime_blocks priority | 不动 | 需求驱动 |
| **P4** | §4.4 llm_intercept 观察 | 不动 | 等用例 |

**建议先做 P1 两项**（一天搞定，干净利落），然后看哪个 P2 有实际需求驱动再启动。

---

## 6. 注脚：模型切换为什么不暴露成工具（2026-06-29 已修）

跟参考项目对齐前，我们曾把 `SwitchModel` 暴露在 function-calling schema 里，让 LLM 可以自主切换模型。**对比之后发现 Hermes / Pi / Kode 三家都不这么做**——模型切换是用户策略，不是 LLM 决策。已在 commit `df24b4f` 后续 patch 中删除 `tools/builtin/switch_model.py`，保留 `/model` slash command 路径。详见同次 commit 的 README 注脚。
