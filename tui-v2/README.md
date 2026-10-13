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

**安全限制**：仅连接受信任的本机服务。当前 TUI v2 尚未接入 `WEB_AUTH_TOKEN` 的网页登录流程；启用服务端认证时，相关 API 请求可能收到 401，因此不能视为已支持认证服务。不要将未认证后端绑定公网地址。

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
