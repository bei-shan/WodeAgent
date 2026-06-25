# 待办计划

> 最新: 审计报告 32 条发现 → `docs/design/2026-06-26-audit-response-and-roadmap.md`

---

## 第 1 批 (本周, ~1小时)

| # | 问题 | 审计编号 | 状态 |
|---|------|---------|------|
| trace_logger 死代码 | P0 #2: token 累加在 `return` 后 | 📝 |
| CircuitBreaker 白名单 INVALID_PARAM | P0 #7 | 📝 |
| max_steps 加进 Config | P1 #18 | 📝 |
| 删除 tool_result_compressor.py | P2 #30 | 📝 |
| 删除 Agent._history dead state | P2 #28 | 📝 |

## 第 2 批 (本周, 1-2 天)

| # | 问题 | 审计编号 | 状态 |
|---|------|---------|------|
| Bash 长输出截断 | P0 #6 | 📝 |
| Bash 沙箱加固 | P0 #5 | 📝 |
| 删除 _is_minimax_backend 硬编码 | P1 #13 | 📝 |
| 删除双份工具定义 | P1 #9 | 📝 |

## 第 3 批 (下周, 3-5 天)

| # | 问题 | 审计编号 | 状态 |
|---|------|---------|------|
| Team Engine work_item 心跳 | P0 #3 | 📝 |
| Token 估算替换为 tiktoken | P1 #16 | 📝 |
| CodeAgent 拆分 | P1 #8 | 📝 |

## 已完成的计划

| 计划 | 设计文档 | 完成日期 |
|------|---------|---------|
| Pi Agent 学习 + 优化 Phase A-E | `2026-06-23-pi-agent-study-and-optimization-plan.md` | 2026-06-25 |
| 会话树 Phase B (Steps 1-6) | `2026-06-25-session-tree-design.md` | 2026-06-25 |
| 耦合度优化 Phase 1-3 | `2026-06-23-coupling-optimization-design.md` | 2026-06-23 |
| 架构重构 Phase 1-6 | `2026-06-22-codeagent-architecture-refactor.md` | 2026-06-22 |
| MCP 连接修复 | `160d857` | 2026-06-25 |

## 未来可能的计划

- LSP 集成（搁置 — 非核心需求）
- VCR fixtures e2e 验证
- TUI 流式渲染激活
- @skill 自动补全
