# Ridge Code 当前架构（2026-10-13）

> 本页记录当前仓库的主要模块边界，不是 API 稳定性保证。此项目处于持续重构中，存在过渡层。详细变更见 `docs/design/`，实际行为以源码及回归测试为准。

## 组件与调用方向

```text
React Web (desktop/web/)              OpenTUI (tui-v2/)
                  \                       /
                      FastAPI service (desktop/service/)
                                   |
                    Application Services (application/)
                                   |
                      SessionController + Events
                          (core/runtime/, core/events.py)
                                   |
                    Agent runtime (agents/, core/runtime/)
                                   |
            Context | Features / Teams | Tools / Sandbox | LLM
```

| 模块 | 当前职责 | 重构时约束 |
| --- | --- | --- |
| `desktop/web/` | React UI、API/WebSocket 客户端 | 避免把会话状态逻辑继续堆在巨型 `App.tsx` |
| `tui-v2/` | Bun/Solid/OpenTUI 终端 UI | 传输、状态投影、控制器和视图分离 |
| `desktop/service/app.py` | FastAPI 路由、认证和 WebSocket | 新业务流程放入 `application/`，路由保持薄层 |
| `desktop/service/application/` | 会话、设置、工作区、turn 业务编排 | 不依赖 UI 的状态实现 |
| `core/runtime/` 与 `core/events.py` | AgentLoop、session、事件与执行协调 | 不调用 React/OpenTUI 组件 |
| `agents/codeAgent.py` 与 `core/features/` | Agent 主循环编排与可插拔能力 | 避免再向主类叠加工具/上下文细节 |
| `core/context_engine/` | 历史树、摘要、上下文与 Trace | 明确上下文压缩及溢出行为 |
| `tools/` 与 `core/sandbox/` | 工具注册、权限、执行与实验性隔离 | 权限拒绝、路径边界和竞态测试优先 |
| `core/team_engine/` | 多 Agent 协作、任务板与持久化 | 避免与 session 层形成双向依赖 |

## 扩展组件

- `core/skills/skill_loader.py`：内置与用户 Skills，以及启用的插件 Skills（只读、按需加载）
- `core/plugin_loader.py`：本地项目插件 ZIP 清单、生命周期、启用和显式代码信任
- `core/features/`：内置 AgentFeature 及经双重授权的 Python 插件扩展；插件命令 Hook 不覆盖用户 hooks.json
- `tools/mcp/`：独立的 MCP 传输/工具协议与权限边界，不等同于插件代码

## 安全边界

- Web 的 `WEB_AUTH_TOKEN` 是本地单用户鉴权；Web 登录与 TUI Bearer 认证机制不同，不构成多租户权限体系。
- TypeScript TUI 设置 `RIDGECODE_AUTH_TOKEN`（兼容 `WODEAGENT_AUTH_TOKEN`）匹配服务端令牌；参见 `tui-v2/README.md`。
- Docker / BoxLite micro-VM 均为显式启用的实验性执行后端，仅隔离 Agent 可见的 Bash 调用；默认本机执行模式不能视为强隔离。参见 [BoxLite 后端](design/2026-10-15-boxlite-vm-provider.md)。
- 旧目录 `.mycode/`、`.mycodeagent/` 和既有 Python 导入路径暂时保留兼容。

## 当前结构性债务（不等同于可以删除）

- 旧 Python Rich/prompt_toolkit TUI 已退役。OpenTUI 的基础启动、会话树和可移植归档已有真实 PTY/HTTP 测试；旧 `/style`、`/budget` 等专有命令仍需可复用服务端 API 后才能迁移。
- Web `App.tsx`、`desktop/service/app.py`、`agents/codeAgent.py`、`core/team_engine/manager.py` 仍较大，应逐步拆分职责并保留回归测试。
- 运行时协议、持久化格式和权限模型需要独立版本化，不宜在「文档清理」时顺手破坏兼容。

## 验证入口

Python：`python -m pytest tests/ -q`；Web：`cd desktop/web && npm ci && npm run build`；TUI：`cd tui-v2 && bun install && bun run typecheck && bun test`。运行条件及最新检查结果以 [GitHub Actions](../.github/workflows/) 为准。
