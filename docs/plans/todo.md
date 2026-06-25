# 全局进度追踪

> 最后更新: 2026-06-25

---

## ✅ 已完成

| 来源 | Phase | 内容 |
|------|-------|------|
| 耦合度优化 | 1 | Config 统一 55 项 |
| 耦合度优化 | 2 | ToolBootstrap 自动发现 (33 import→0) |
| 耦合度优化 | 3 | Config 注入 8 模块 |
| 架构重构 | 1 | AgentFeature 协议 + env_helpers |
| 架构重构 | 2 | 10 个 Feature 迁移 + MCPFeature |
| Pi 学习 | A | 提示词瘦身 (~19K→~2.2K tokens) |
| Pi 学习 | B | 会话树 (JSONL / fork / /tree / model_change / thinking) |
| Pi 学习 | C | Plan Mode 文件化 (PLAN.md) |
| Pi 学习 | D | Late Binding ContextBuilder |
| Pi 学习 | E | 工具描述内联 (33 prompt 文件) |
| MCP 修复 | — | 跨事件循环 stale session 检测 |
| 审计批量 | 1-5 | 17/32 条修复 |

---

## 🔴 待实施 — 架构重构 (P1-P2)

| Phase | 内容 | 步骤 | 状态 |
|-------|------|------|------|
| 架构重构 3 | ReAct 循环重构 | Steps 16-20 | ❌ |
| 架构重构 4 | 插件系统 | Steps 21-24 | ❌ (基础已实现，缺测试) |
| 架构重构 5 | 子代理流式 | Steps 25-28 | ❌ |
| 架构重构 6 | 体验优化 | MCP 连接状态显示 | ❌ |

### 架构重构 Phase 3 详细 (ReAct 循环瘦身)

| Step | 内容 | 难度 |
|------|------|------|
| 16 | 实现 `_collect_runtime_blocks()` — 统一 Feature 上下文收集 | 小 |
| 17 | 实现 `_invoke_llm_with_interception()` — VCR 拦截在此 | 中 |
| 18 | 重构 `_execute_tool()` 使用 Feature 拦截 | 中 |
| 19 | 添加工具耗时统计 | 小 |
| 20 | 废弃旧 /save /load API | 小 |

### 架构重构 Phase 4 详细 (插件系统)

| Step | 内容 | 难度 |
|------|------|------|
| 21-24 | Plugin loader 完善 + 测试 | 中 |

### 架构重构 Phase 5 详细 (子代理流式)

| Step | 内容 | 难度 |
|------|------|------|
| 25-28 | BackgroundTaskRunner 进度回调 + TUI 展示 | 大 |

---

## 🟡 待实施 — 审计剩余 (P1-P2)

| # | 问题 | 难度 |
|---|------|------|
| P1 #10 | MCPFeature 不注册 MCP 工具 — 搬过来 | 中 |
| P1 #11 | Hook + Feature 双层拦截合并 | 大 |
| P1 #12 | 14 team 工具重叠合并 | 中 |
| P1 #14 | 每次 run save 3 次优化 | 中 |
| P1 #20 | TUI 流式渲染激活 (streaming.py 是骨架) | 大 |
| P2 #21 | VCR fixtures 目录创建 | 小 |
| P2 #23 | test_ui_components.py 假测试重写 | 小 |
| P2 #24 | TUI 组件 0 测试 | 中 |
| P2 #27 | agent_teams 双重 init | 中 |
| P2 #31 | CLI 530 行 if-elif → SlashCommandRegistry | 中 |
| P2 #32 | @skill 自动补全 | 小 |

---

## 建议实施顺序

```
本周:
  1. 架构重构 Phase 3 (Steps 16-20) — ReAct 循环瘦身
  2. P2 #21 VCR fixtures 目录 + P2 #32 @skill 补全

下周:
  3. P1 #10 MCPFeature 接管工具注册
  4. P1 #11 Hook + Feature 双层拦截合并
  5. 架构重构 Phase 4 插件系统完善

长期:
  6. P1 #20 TUI 流式渲染激活
  7. 架构重构 Phase 5 子代理流式
```
