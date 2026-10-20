# Ridge · TypeScript TUI（实验性）

基于 **Bun + Solid + OpenTUI** 的终端客户端，复用现有 Python FastAPI Agent 引擎；不会在终端端嵌入、复制或重写 Python Agent。

## 本地启动

先在仓库根目录启动后端：

```bash
python -m ridgecode serve
```

然后在另一终端：

```bash
cd tui
bun install --frozen-lockfile
bun run typecheck
bun test
cd ..
python -m ridgecode tui
```

服务端地址默认 `http://127.0.0.1:8000`，可使用 `RIDGECODE_SERVER` 修改；只使用正式变量 `RIDGECODE_SERVER`。

**鉴权**：服务端使用 `WEB_AUTH_TOKEN` 时，TUI 端设置 `RIDGECODE_AUTH_TOKEN` 为相同值。HTTP 和 WebSocket 都通过 `Authorization: Bearer` 请求头认证，令牌不会出现在 URL 或终端日志。仅向可信本地服务发送凭据；远程服务器须使用 HTTPS/WSS。本地鉴权是单操作员保护，尚不是多用户 RBAC。

## 能力与模块

- 会话列表、历史加载、新会话、消息提交、中断、权限确认与 AskUser
- WebSocket 事件顺序、replay cursor、去重、缺口检测与重连
- `/new`、`/sessions`、`/use N`、`/rename TITLE`、`/pin`、`/delete confirm` 等会话命令
- `/models`、`/model PROFILE`、`/plan on|off`、`/teams on|off`、`/stop`、`/help` 等运行控制
- 清晰的分层，不覆写 `CodeAgent` 的私有方法

```text
src/protocol.ts      Wire DTO、事件解码
src/api/             HTTP / WebSocket 适配器
src/state/           纯状态投影
src/controller.ts    会话生命周期与命令编排
src/ui/              OpenTUI 视图与交互
src/index.tsx        启动入口
```

设计参见 [TUI v2 分层说明](../docs/design/2026-10-13-tui-v2-layered-architecture.md)。旧 Python TUI 已退役，OpenTUI 是唯一维护的终端界面。参见 [退役说明](../docs/design/2026-10-13-python-tui-retirement.md)。

会话树：`/tree` 展示包含节点 ID 的分支视图，`/fork 1234abcd` 从对应消息分叉；仅空闲会话允许分叉，原分支保留，并立即持久化当前光标。旧的 `/fork` 功能现已由服务端 API 正式支持。

### 会话导入／导出

`/export ./conversation.json` 在终端所在机器上创建**新文件**（如果文件已存在则失败，不会覆盖）；`/import ./conversation.json` 从本机读取受大小限制的文件，并在后端**新建会话**。仅保留用户／助手可见对话、树分支和标签，不包含工具输出、密钥、系统提示词或 Teams 状态；这是可分享对话记录，**不是**可完整恢复工具运行环境的备份。JSON 可能包含私密聊天内容，请只分享给可信对象。
