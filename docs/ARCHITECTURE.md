# Ridge Code 当前架构（2026-10-15）

本页只描述现役模块与已经验证的入口。带日期的设计稿用于追溯，不应取代源码、测试与生产验收记录。

## 部署边界

```text
Trusted local mode (single operator)                  Hosted (metadata only)
React Web / OpenTUI                                    HTTPS/OIDC bearer token
            |                                                    |
desktop/service/app.py                                 desktop/service/hosted_app.py
            |                                                    |
Application Services                                   PostgreSQL RLS membership
            |                                                    |
SessionController                                      Private session metadata
            |
CodeAgent → AgentLoop / ContextRuntime / ModelGateway
            |
  RuntimeServices ── MCP / Team / Session / Sandbox / Hooks / VCR
            |
  mandatory PolicyMiddlewarePipeline (Plan / Budget / Delegate)
            |
  ToolRegistry authorization → isolated provider or local trusted tools
```

本地 Agent 服务默认绑定 `127.0.0.1`。**切勿将本地 Web/API 直接暴露到公网或共享租户。** 托管服务当前只处理经过 OIDC 与 RLS 校验的会话元数据；尚未向多租户开放 Agent 执行。

## 现役目录

| 路径 | 真实职责 |
| --- | --- |
| `ridgecode/` | 统一的本地用户命令 `python -m ridgecode` |
| `desktop/web/` | React Web UI 与浏览器 E2E |
| `tui/` | Bun / Solid / OpenTUI 终端 UI |
| `desktop/service/` | local trusted API 与独立 hosted metadata API |
| `desktop/service/application/` | 路由之外的应用服务 |
| `agents/codeAgent.py` | Agent 入口、工具编排与模型服务协调 |
| `core/runtime/` | 显式 RuntimeServices、强制 Middleware、AgentLoop、任务租约与事件 |
| `core/context_engine/` | 消息历史、压缩、上下文、Trace |
| `core/team_engine/` | AgentTeam 协作、审批、任务板 |
| `core/sandbox/` | Docker、BoxLite、E2B 工具隔离能力（分别独立验收） |
| `tools/` | 工具目录、执行契约、MCP 适配、权限校验 |
| `core/skills/`、`core/plugin_loader.py` | Skills 与声明式插件 / Hook，旧 Python Feature 插件入口拒绝加载 |
| `core/persistence/`、`db/migrations/` | RLS 会话与持久任务队列 |
| `scripts/` | 评测、预检、发布证据、迁移检查使用的底层实现 |
| `tests/`、`.github/workflows/` | 回归与发布验收 |

旧 `core/features/`、Python TUI、任意 Python Feature 插件入口已经删除；**不要在新代码里恢复这些抽象。** Runtime Services 拥有资源与生命周期；Middleware 执行不可绕过的核心策略；Plugin 只提交显式授权的能力声明。

## 命令体系

从仓库根目录运行：

```bash
python -m ridgecode --help
python -m ridgecode setup
python -m ridgecode doctor --target all
python -m ridgecode serve
python -m ridgecode web      # 需要先 cd desktop/web && npm ci
python -m ridgecode tui      # 需要先 cd tui && bun install --frozen-lockfile
python -m ridgecode eval --json
```

完整命令和先决条件见 [README](../README.md) 与 [命令参考](COMMANDS.md)。开发者测试和带凭据的发布验收脚本仍属于受控内部工具，不应等同于交互式用户命令。

## 生产验收边界

CI 通过仅证明指定自动化测试通过；不能据此宣布共享租户 Agent 执行已经生产就绪。当前还需要真实托管 Runner、租户专属沙箱与凭据隔离、MCP 授权、长时间负载及故障恢复验收。任务队列的过期执行不会自动重放，避免重复调用不可逆工具。

## 设计与实现约束

- 安全内核、身份校验、资源归属、工具权限和租约 fencing 不可由插件替换。
- 新服务使用显式的依赖关系；不通过整数 `order` 或在全局 Agent 对象上注册任意插件对象来管理生命周期。
- 在安全路径中不把资源声明、权限校验或有状态管理藏在 Web 组件里。
- `docs/design/` 保留带日期的决策记录，但现役使用说明应优先更新本页和 `README.md`。
