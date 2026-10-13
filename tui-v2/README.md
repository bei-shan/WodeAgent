# Ridge · TypeScript TUI（实验性）

基于 **Bun + Solid + OpenTUI** 的终端客户端，复用现有 Python FastAPI Agent 引擎；不会在终端端嵌入、复制或重写 Python Agent。

## 本地启动

先在仓库根目录启动后端：

```bash
python -m uvicorn desktop.service.app:create_app --factory --host 127.0.0.1 --port 8000
```

然后在另一终端：

```bash
cd tui-v2
bun install
bun run typecheck
bun test
bun run dev
```

服务端地址默认 `http://127.0.0.1:8000`，可使用 `RIDGECODE_SERVER` 修改；为了兼容旧配置，仍接受 `WODEAGENT_SERVER`，新变量优先。

**鉴权**：服务端使用 `WEB_AUTH_TOKEN` 时，TUI 端设置 `RIDGECODE_AUTH_TOKEN` 为相同值（旧变量 `WODEAGENT_AUTH_TOKEN` 仍兼容）。HTTP 和 WebSocket 都通过 `Authorization: Bearer` 请求头认证，令牌不会出现在 URL 或终端日志。仅向可信本地服务发送凭据；远程服务器须使用 HTTPS/WSS。本地鉴权是单操作员保护，尚不是多用户 RBAC。

## 能力与模块

- 会话列表、历史加载、新会话、消息提交、中断、权限确认与 AskUser
- WebSocket 事件顺序、replay cursor、去重、缺口检测与重连
- 清晰的分层，不覆写 `CodeAgent` 的私有方法

```text
src/protocol.ts      Wire DTO、事件解码
src/api/             HTTP / WebSocket 适配器
src/state/           纯状态投影
src/controller.ts    会话生命周期与命令编排
src/ui/              OpenTUI 视图与交互
src/index.tsx        启动入口
```

设计参见 [TUI v2 分层说明](../docs/design/2026-10-13-tui-v2-layered-architecture.md)。原 Python TUI 仍保留作为兼容入口。
