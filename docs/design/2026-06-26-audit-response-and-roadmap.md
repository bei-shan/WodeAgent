# 审计报告对照 + 修订路线图

> 日期: 2026-06-26 | 来源: 外部审计报告 32 条发现

---

## 一、已修复项 ✅

| 审计 # | 问题 | 修复 commit | 说明 |
|--------|------|------------|------|
| P0 #1 | 会话树主路径不调用 | `06a0681` `4553b90` | v2 snapshot now passes cursor_id/history_entries/labels; `/tree` `/fork` `/thinking` commands added; `to_messages_branch()` available |
| P0 #4 | 三套持久化并存 | `4cbc0cb` | SessionStore v2 defines tree fields; JsonlSessionStore implemented (Pi-compatible); SessionManager is main path |
| 提示词超大 | Phase A `8fd259d` | L1+工具提示词 ~19K→~2.2K tokens (88% reduction) |
| 工具硬编码 import | Phase 2 `b5eaf10` | ToolBootstrap auto-discovery, 33 imports → 0 |
| 配置散落 55 os.getenv() | Phase 1-3 | Config 统一 55 项, 8 模块注入 |
| Plan Mode 纯内存 | Phase C `408c804` | ExitPlanMode writes PLAN.md to project root |
| ContextBuilder 大缓存 | Phase D `1b9a142` | Late Binding, 删除 `_cached_system_messages`, 实时组装 |
| 工具描述冗余文件 | Phase E `7841eb0` | 33 prompt 文件描述内联, 不再从 prompts/tools_prompts 加载 |
| MCP 连接超时 | `160d857` | 跨事件循环 stale session 检测 |
| README/CLAUDE.md 不一致 | `0cac44d` | 修复 |
| P1 #16 L1 prompt | Phase A | 153 行→57 行, 2.3K chars |

---

## 二、P0 致命（需本周修复）

| 审计 # | 问题 | 当前状态 | 工作量 |
|--------|------|---------|--------|
| **P0 #2** | trace_logger total_usage 在 `return` 后死代码 → 所有 session_summary 永远是 0 | **确认**。`trace_logger.py:216-226` — `total_usage` property 在 `return dict(self._total_usage)` 后还有 token 累加逻辑，无法执行 | 5 分钟 |
| **P0 #3** | work_item 无心跳/超时 → worker 崩了永久卡 running | **确认**。`team_engine/store.py:294-315` — 无 heartbeat_ts、无 watchdog sweep | 2 天 |
| **P0 #7** | CircuitBreaker INVALID_PARAM 计入 failure → 模型连续传错参数误熔断 | **确认**。`tools/registry.py:299-302` 所有 status=error 都算 failure，不区分错误码 | 30 分钟 |
| **P0 #5** | Bash 软沙箱可被 `python -c "open(...)"` 绕过 | **确认**。bash.py 安全规则不拦截 python 写文件 | 1 天 |
| **P0 #6** | Bash 长输出不截断 → 爆 LLM 上下文 | **确认**。注释自承"MVP 不截断" | 1 小时 |

---

## 三、P1 高优（本周或下周）

| 审计 # | 问题 | 当前状态 | 工作量 |
|--------|------|---------|--------|
| **P1 #8** | CodeAgent 是 god class (1400+ 行) | 部分修复：Feature 协议已拆分 11 个 feature，SlashCommand 还在 chat_test_agent.py 530 行 if-elif | 2-3 天 |
| **P1 #9** | DELEGATION_ALLOWED_TOOLS / PLAN_MODE_TOOLS 双份定义 | **确认**。CodeAgent L52,70 定义一份，Feature 再 set 一份 | 1 小时 |
| **P1 #10** | MCPFeature 不注册 MCP 工具 | **确认**。注册在 `codeAgent._register_mcp_tools`，feature 只管理状态 | 1 天 |
| **P1 #11** | Hook + Feature 双层拦截 | **确认**。`_execute_tool` 两套接口 | 1 天 |
| **P1 #12** | 14 个 team 工具有 3 对重叠 | 设计取舍。可合并但不紧急 | 1 天 |
| **P1 #13** | `_auto_detect_provider` 200+ 行启发式 + `_is_minimax_backend` 硬编码域名 | **确认** | 2 小时 |
| **P1 #14** | 每次 run 至少 save 3 次 | 设计取舍。V1 用全量快照 | 1 天 |
| **P1 #15** | 3 个 Feature 是空壳/半空壳 | MCPFeature(不注册)、DelegateModeFeature(只常量)、WorktreeFeature.cleanup(pass) | 1 天 |
| **P1 #16** | Token 估算用 chars//3，中文严重低估 | **确认**。`history_manager.py:388-409` | 1 天 |
| **P1 #17** | 4 个 subagent prompt 重复度 ~70% | **确认**。Phase A 已瘦身但未去重 | 2 小时 |
| **P1 #18** | `max_steps=50` 硬编码无配置 | **确认**。`codeAgent.py:100` — `self.max_steps = 50` | 5 分钟 |
| **P1 #19** | `codeAgent.py` camelCase 违反 PEP8 | 文件命名历史遗留，5 个文件受影响 | 1 小时 |
| **P1 #20** | TUI 双渲染：streaming.py 是骨架，实际跑 EnhancedUI | **确认**。`agent.run()` 同步阻塞 | 2 天 |

---

## 四、P2 中低（卫生/一致性问题）

| 审计 # | 问题 | 工作量 |
|--------|------|--------|
| P2 #21 | VCR fixtures 目录不存在 | 创建目录 + 1 个 fixture |
| P2 #22 | `once` 和 `new_episodes` 行为相同 | 实现差异 |
| P2 #23 | test_ui_components.py 是 __main__ 演示 | 改写 |
| P2 #24 | PermissionDialog/StreamingResponse/MentionCompleter 0 测试 | 加测试 |
| P2 #25 | Plugin loader 静默吞异常 | 加日志 |
| P2 #26 | ALWAYS_IGNORE 三处重复 | 抽常量 |
| P2 #27 | agent_teams 重复初始化 (CodeAgent + Feature) | 收敛 |
| P2 #28 | Agent._history dead state | 删除 |
| P2 #29 | Config 字段双重声明 | 重构 |
| P2 #30 | tool_result_compressor.py 弃用未删 | 删除 |
| P2 #31 | CLI 入口 530 行 if-elif | SlashCommandRegistry |
| P2 #32 | @skill 自动补全缺失 | 加补全 |

---

## 五、与现有设计文档的对齐

### 已完成

| 设计文档 | 对应审计项 | 状态 |
|---------|----------|------|
| `2026-06-23-coupling-optimization-design.md` | P1 #8-13 部分覆盖 | ✅ Phase 1-3 |
| `2026-06-23-pi-agent-study-and-optimization-plan.md` | P1 #16-17 | ✅ Phase A-E |
| `2026-06-25-session-tree-design.md` | P0 #1, P0 #4 | ✅ Phase B |

### 需补充的设计文档

| 新文档 | 覆盖审计项 | 优先级 |
|-------|-----------|--------|
| `2026-06-26-p0-critical-fixes.md` | P0 #2, #5, #6, #7 | **本周** |
| `2026-06-26-team-engine-production.md` | P0 #3, P1 #12 | 下周 |
| `2026-06-26-codeagent-split.md` | P1 #8, #9, #11, #15 | 下周 |
| `2026-06-26-quality-guardrails.md` | P1 #16, #18, #20, P2 | 长期 |

---

## 六、修订优先级（建议执行顺序）

```
第 1 批 (半天):
  ├── P0 #2: trace_logger 死代码 → 5 分钟
  ├── P0 #7: CircuitBreaker 白名单 INVALID_PARAM → 30 分钟
  ├── P1 #18: max_steps 加进 Config → 5 分钟
  ├── P2 #30: 删除 tool_result_compressor.py → 1 分钟
  └── P2 #28: 删除 Agent._history dead state → 1 分钟

第 2 批 (1-2 天):
  ├── P0 #6: Bash 长输出截断
  ├── P0 #5: Bash 沙箱加固 (python -c 写文件拦截)
  ├── P1 #13: 删除 _is_minimax_backend 硬编码域名
  └── P1 #9: 删除双份工具定义

第 3 批 (3-5 天):
  ├── P0 #3: Team Engine work_item 心跳
  ├── P1 #16: Token 估算替换为 tiktoken
  ├── P1 #8: CodeAgent 拆分
  └── P1 #15: Feature 空壳填充/删除
```
