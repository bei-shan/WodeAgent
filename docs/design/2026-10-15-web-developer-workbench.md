# Ridge Web 开发工作台：GFM、Git Diff、执行历史与 Docker TTY

此版本在既有 ChatGPT 风格对话外壳上补齐四类能力，同时保持会话隔离与本地鉴权的边界。

## Markdown / GFM

- 前端使用锁定版本的 `marked` 解析 CommonMark 与 GFM（表格、任务列表、删除线等）。
- HTML 片段一律经过 DOMPurify 清洗；禁止嵌入图片、iframe、SVG、媒体以及远程资源追踪。
- 代码围栏使用专门代码组件，支持复制和轻量语法着色。
- Markdown 渲染对单条响应设有 200,000 字符上限，避免超长响应持续占据浏览器。
- 此能力不执行模型输出的脚本。

## 真正的 Git Diff

`GET /api/sessions/{sid}/git/diff?path=...` 比较文件当前状态与会话内 Git 仓库 `HEAD` 的已跟踪版本。

- 会话工作区必须包含**真实 .git 目录**；不向上搜索项目仓库，也不解析 `.git` worktree 指针。
- 文件路径先经安全的 descriptor-based 工作区服务检验；不允许 `..`、绝对路径及符号链接逃逸。
- `git diff HEAD -- :(literal)<relativePath>` 通过参数数组执行，不经过宿主机 shell，并禁用 external diff/textconv。
- 限制文件大小、Git 超时和返回内容大小。无 `HEAD` 或未初始化 Git 仓库时提供明确的不可用结果。
- 浏览器原有的「对比最新」仍是快照 Diff；**Git Diff 不等于文件快照比较**。未跟踪文件无 HEAD 差异。

## 运行记录持久化

`GET /api/sessions/{sid}/execution-history` 返回 SQLite 保存的每会话最多 200 个工具调用摘要。

- 只保存工具名、调用 ID、步骤、运行状态，以及有界/脱敏的 Docker 或 BoxLite stdout、stderr、exit code。
- 不持久化任意命令参数、模型提示词、MCP 原始输出或所有流式 token。
- SQLite 单次事务，重启后可读取，删除会话时删除对应记录。
- 文件位于 `MYCODEAGENT_DATA_DIR/execution-history.sqlite3`，限制为 `0600`；日志脱敏不是完美的秘密检测，敏感生产环境应保护磁盘与备份。
- 原始 EventJournal 仍然是内存环形队列；这里的持久化仅针对工具摘要。

## Docker 实验性交互 TTY

默认**禁用**。使用真实的 Docker guest TTY，不使用宿主机 shell 作为后备。

启用前提：

```sh
WEB_AUTH_TOKEN=<至少24位随机访问令牌>
WEB_SANDBOX_TERMINAL_ENABLED=true
```

必须由操作员预先安装 Docker 并预加载 `python:3.12-alpine` 镜像。WebSocket：
`/api/sessions/{sid}/terminal`。Capability 接口：`GET /api/sandbox/terminal-capabilities`。

- 强制本地 Cookie/Bearer 授权及 WebSocket Origin/Host 检查。
- 同时至多 2 个交互终端、每会话最多 1 个；最长 600 秒。
- Docker 使用禁网络、非 root、只读根文件系统、`--cap-drop=ALL`、资源限制及专用 session 工作区绑定。
- 终端输入、输出各有限额，断线后强制终止 Docker CLI 进程组并尽力清理 daemon 容器。
- **现有前端是简易命令控制台，不是 ANSI 全屏 xterm 终端仿真**；不支持 BoxLite PTY、后台重连、会话迁移或多租户 RBAC。
- 该功能需要真实 Docker 硬件集成/安全验收；自动化仅覆盖鉴权拒绝、运行时边界和 Web UI 逻辑，不可将其视为已获得生产级交互终端验收。

## 测试

```sh
python -m pytest tests/test_web_developer_workbench.py tests/test_web_service.py tests/test_workspace_service.py -q
cd desktop/web && npm ci && npm run build && npx playwright test
```
