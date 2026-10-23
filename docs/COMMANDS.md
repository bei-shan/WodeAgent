# Ridge Code 命令参考

所有日常用户命令从**仓库根目录**执行。当前 Ridge Code 是源码运行项目，还没有安装为独立系统命令；统一入口是 `python -m ridgecode`。

## 一次性安装

```bash
python3.12 -m pip install -r requirements.txt
python -m ridgecode setup          # 交互配置当前目录的 .env；已存在时拒绝覆盖
python -m ridgecode doctor --target core
```

修改已有 `.env` 请优先使用编辑器；只有确认想替换时，才使用 `python -m ridgecode setup --force`。向导写入的密钥文件使用受限权限和原子替换；不要提交到 Git。

## 启动本地 Web

在终端 A（仓库根目录）：

```bash
python -m ridgecode serve
```

此命令只启动**受信任的本地单用户 Agent API**，默认监听 `127.0.0.1:8000`；命令拒绝 `0.0.0.0` 和非 loopback 绑定，避免误当作生产托管服务。

首次启动前在终端 B 安装 Web 依赖，然后从仓库根目录启动：

```bash
cd desktop/web && npm ci && cd ../..
python -m ridgecode web
```

Vite 会显示浏览器访问地址，停止服务请使用 Ctrl+C。

## 启动 OpenTUI

```bash
cd tui && bun install --frozen-lockfile && cd ..
python -m ridgecode tui
```

OpenTUI 使用已经启动的本地 Agent API。按需配置 `RIDGECODE_SERVER`、`RIDGECODE_AUTH_TOKEN`。TUI 与 Web 是客户端，不会自动创建独立的 Hosted Worker。

## 作为编辑器后端（ACP）

```bash
python -m ridgecode acp --cwd /path/to/workspace
```

以 stdio 上的 JSON-RPC 提供 [Agent Client Protocol](https://agentclientprotocol.com) 子集：`initialize`、`session/new`、`session/load`、`session/prompt`、`session/cancel`。Agent 回复通过 `session/update`（`agent_message_chunk`）流式发送，工具调用以 `tool_call` / `tool_call_update` 通知转发。

需要用户确认的文件/命令操作会以 `session/request_permission` 反向请求编辑器，客户端返回 `selected` 才放行；超时、取消或未实现该请求一律按拒绝处理（fail closed）。

`session/load` 支持跨进程恢复：进程重启后带上 `sessionId` 与 `cwd`，服务器会从磁盘快照重建历史并继续使用同一会话（恢复不会覆盖原快照）。

Agent 需要向用户提问时，会发送非标准的 `session/request_input`（参数为 `{sessionId, prompt}`，期望返回 `{answer}`）。未实现该方法、返回错误或超时的客户端会得到空回答，回合继续而不是卡死。只支持标准 ACP 的编辑器可以忽略这条扩展。

## 会话库维护

```bash
python -m ridgecode maintain --dry-run   # 只报告
python -m ridgecode maintain             # 修复并迁移
python -m ridgecode maintain --json      # 机器可读
```

审计每个会话的快照与 append-only 日志是否一致：日志落后就追加，内容分歧就按快照重发，缺少日志的旧会话补建日志，旧 schema 快照顺带迁移。**快照和历史树永远不会被改写**；日志超前的会话交给读路径处理，不在这里截断。`--sessions-dir` 可指向其他目录。

支持 ACP 的编辑器（如 Zed）把上面的命令配置为 agent 启动命令即可；会话在进程内创建，进程退出即结束。该模式只面向本地受信任环境，不提供鉴权或多租户隔离。

## 检查、评测和测试

```bash
python -m ridgecode --help
python -m ridgecode doctor --target all --json
python -m ridgecode eval --json
python -m ridgecode soak --iterations 10 --json
python -m pytest tests/ -q
cd desktop/web && npm ci && npm run build && cd ../..
cd tui && bun install --frozen-lockfile && bun run typecheck && bun test
```

`soak` 是**离线模拟模型**资源稳态测试；真实 30 分钟基准可用 `--duration-seconds 1800`，不能替代真实模型、分布式负载或多租户验收。

## 内部维护/发布门禁

位于 `scripts/` 的独立脚本不是第二套交互 CLI：CI 继续直接调用它们以绑定测试范围和证据。包含 `scripts/release_acceptance.py`、`scripts/staging_tenant_acceptance.py`、`scripts/mcp_oauth_login.py` 和 `scripts/check_docs_links.py`。它们分别处理严格发布证据、双账号元数据验收、本地 MCP OAuth 以及文档链接检查。

带真实云计算开销、密钥或外部环境依赖的操作必须由批准的 CI 工作流明确开启；**不要将组件测试的 PASS 转化成托管 Agent 生产 GO**。具体发布证据见 [生产验收门禁](releases/production-gates.md)。

## 不应使用的旧入口

- 不再存在 `core/features/`、Python Rich TUI 和 Python Feature 插件接口。
- `RIDGE_ALLOW_PLUGIN_CODE` 不再授予任何代码扩展权限；本地声明式 Hook 只接受 `RIDGE_ALLOW_TRUSTED_HOOKS=1` 与逐插件指纹信任。
- `python scripts/first_run_wizard.py`、`python scripts/v01_preflight.py` 是底层维护实现。用户使用 `python -m ridgecode setup` 和 `python -m ridgecode doctor`。
- `desktop/service/hosted_app.py` 目前是 OIDC + PostgreSQL RLS 的**元数据 API**，不是可执行多租户 Agent 的生产服务。
