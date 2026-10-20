# Ridge Code

**Ridge** 是一个本地优先、可扩展的 AI 编程代理项目：使用 Python 实现 Agent 引擎，由同一套会话运行时服务 Web 和终端界面。项目目前处于持续开发阶段，适合个人开发、学习与实验，并非开箱即用的多用户 SaaS。

- **Agent 引擎**：Python 3.12+、ReAct、工具调用、Runtime Services / Middleware、模型路由
- **交互界面**：React/TypeScript Web 与 TypeScript/OpenTUI 终端（单一终端实现，仍在迭代）
- **执行能力**：代码读写、命令执行、MCP、Skills、子代理与 Agent Teams
- **工具执行安全**：核心工具在执行边界校验参数；只读工具按明确声明安全性有限并发，修改与交互型工具串行执行
- **上下文管理**：会话树、压缩、工具输出截断、Trace 与流式事件
- **安全选项**：工作区路径边界、工具权限、可选的本地 Web 登录、实验性 Docker 工具隔离

> **安全范围**：默认是受信任的本机单用户开发工具。未配置认证时不要把后端开放到公网；Docker 隔离需要显式启用，默认本地模式的工具仍可能在宿主机执行。不要把 API Key 提交到仓库。

## 快速开始

需要 **Python 3.12+**。Web 前端需要 Node.js 20+ 与 npm；新版 TUI 需要 Bun。

### 1. 安装与配置

```bash
git clone https://github.com/bei-shan/WodeAgent.git
cd WodeAgent
python3.12 -m pip install -r requirements.txt
python -m ridgecode setup
python -m ridgecode doctor --target core
```

配置命令会交互式写入 `.env`，已有文件默认不覆盖；确认需要替换时使用 `python -m ridgecode setup --force`。也可以将 `.env.example` 复制为 `.env` 并自行编辑。密钥不要提交到 Git。

### 2. 启动 Web

终端 A（项目根目录）：

```bash
python -m ridgecode serve
```

终端 B（项目根目录）：

```bash
cd desktop/web && npm ci && cd ../..
python -m ridgecode web
```

打开 [http://localhost:5173](http://localhost:5173)。本地 Agent API 只能绑定回环地址，**不是多租户公网服务**。设置私有 `.env` 的 `WEB_AUTH_TOKEN` 后重启，可启用本地单用户 Web 登录。

### 3. 启动 OpenTUI

先启动 Python API，再从根目录运行：

```bash
cd tui-v2 && bun install --frozen-lockfile && cd ..
python -m ridgecode tui
```

可用 `RIDGECODE_SERVER` 指定服务端；设置了 `WEB_AUTH_TOKEN` 时，TUI 使用相同的 `RIDGECODE_AUTH_TOKEN`。详见 [TUI 文档](tui-v2/README.md)。

### 4. 日常检查与评测

```bash
python -m ridgecode --help
python -m ridgecode doctor --target all
python -m ridgecode eval --json
python -m pytest tests/ -q
```

命令完整参考、受信任本地模式与生产验收边界见 [命令参考](docs/COMMANDS.md)。

## 当前架构概览

```text
React Web / OpenTUI
        |
local FastAPI + Application Services
        |
SessionController / AgentLoop / ContextRuntime
        |
RuntimeServices: MCP, Session, Team, Sandbox, Hooks
        |
mandatory Middleware: Plan, Budget, Delegate
        |
ToolRegistry / Sandbox / ModelGateway
```

- `ridgecode/`：唯一的本地日常命令入口。
- `agents/`、`core/runtime/`：Agent 编排、生命周期、策略和资源服务。
- `core/context_engine/`、`core/team_engine/`：上下文和团队运行时。
- `core/skills/`、`core/plugin_loader.py`：Skills、声明式插件与授权 Hook。
- `tools/`、`core/sandbox/`：工具注册、权限与隔离执行。
- `desktop/service/`、`desktop/web/`、`tui-v2/`：服务端和两个客户端。
- `db/`、`core/persistence/`：多租户元数据与持久队列基础设施。

已删除旧 Python AgentFeature 框架与旧 Python TUI；托管 API **仅提供元数据服务，尚不能执行生产级共享租户 Agent 回合**。准确的目录职责见 [现役架构](docs/ARCHITECTURE.md)。

## 本地运行的并发保护

Web 服务为正在执行的 Agent 回合设置进程内的并发上限：
`RIDGE_MAX_ACTIVE_TURNS=8`（默认 8，可配置 1–256）。当其他会话已占满容量时，新任务返回 HTTP 429 与 `Retry-After`，不创建无限排队任务；正在执行或等待权限批准的回合也会占用名额。健康接口显示当前活动数、上限和拒绝次数。详见 [并发准入设计](docs/design/2026-10-15-bounded-turn-admission.md)。

**这不是分布式任务队列或多租户配额**，多 Uvicorn Worker 会各自维护计数器；部署时仍应使用单进程受信任本地模式，等完成统一鉴权、共享数据库与分布式任务租约后再扩容。

## 工具命令审批与附件

Ridge Code 的工具执行分为受信任本地主机执行和 Docker/BoxLite/E2B 沙箱执行。工作目录限制**不等于**进程沙箱：

- `RIDGE_BASH_REQUIRE_SANDBOX=true`：禁止普通本机 Bash 工具执行，必须配置实际隔离执行后端；不能因为沙箱不可用自动回退本机。
- `RIDGE_BASH_APPROVAL_MODE=strict`：非只读且未经审批的本机命令，在没有交互式审批代理时直接拒绝。Web 会话有审批通道时，逐条请求具体命令授权；授权不自动继承给下一条命令。
- Windows 本机命令由 Job Object 负责整棵进程树的超时与取消清理，并有独立 Windows CI；此能力不隔离文件权限或网络。
- 工作区 Write/Edit/MultiEdit 在支持的 POSIX 系统中使用目录文件描述符锚定、协作式锁及原子发布，防止符号链接父目录切换越界。

Web 端上传图片/文档时，文件先进入会话工作区之外的私有暂存目录，经过已配置的 ClamAV 扫描后才会独占发布到 Agent 工作区。上传成功后收到会话绑定的随机附件凭证（不是文件路径权限）；消息只提交凭证，服务端核对上传记录、文件身份和大小，拒绝跨会话或被修改的文件。PNG/JPEG/GIF/WebP 必须通过图片解码和像素上限验证，再作为视觉消息传入当前轮次的**每次模型调用**；UTF-8 文本、PDF、DOCX 可按上限提取为模型文本内容，其他二进制保持路径引用。图片字节和原始文档不会写入会话历史；模型不支持视觉输入时会拒绝或报错。PDF/DOCX 支持独立 Docker 转换：先执行 `docker build -f deploy/document-converter/Dockerfile -t ridge-document-converter:local .`，再设置 `RIDGE_DOCUMENT_CONVERTER_MODE=docker` 与 `RIDGE_DOCUMENT_REQUIRE_SANDBOX=1`，转换容器无网络、无工作区挂载、非 root 且有 CPU/内存/PID/超时限制；容器不可用会拒绝，**不会回退宿主机**。

单个上传限制 25 MiB，单次消息最多 4 个附件、总共 8 MiB，图片最多 4 MiB / 1600 万像素，文本最多 96 KiB，PDF/DOCX 文档解析上限 3 MiB；上传凭证两小时过期。随机凭证的哈希和会话/文件身份信息保存在本地 SQLite，可跨进程恢复；每会话最多 128 份有效上传、100 MiB 有效占用，整库上限 1 GiB。服务启动和每小时执行过期清理，临时失败会重试。可使用 `RIDGE_ATTACHMENT_SCAN_REQUIRED=1` 和 `RIDGE_CLAMD_UNIX_SOCKET=/run/clamav/clamd.ctl` 强制在签发凭证前通过 ClamAV 扫描，扫描服务不可用即拒绝。完整设计和生产边界见 [命令安全和附件处理](docs/design/2026-10-20-command-security-and-attachments.md)。

## MCP SDK 与连接稳定性

默认使用 MCP Python SDK **2.x**；HTTP 采用新版 Streamable HTTP 客户端，同时保留独立 CI 通道测试 MCP 1.x 的兼容性。MCP stdio 子进程和 HTTP 会话由同一异步所有者管理，同一客户端连续同步工具调用会复用连接，不再每次启动新的 MCP Server。测试包含本地真实 stdio 和 Streamable HTTP 服务，覆盖初始化、调用、资源关闭及超时／取消。

这不表示已经验证所有第三方 MCP Server 的 OAuth、SSE 续传或跨网络故障场景。工具取消为尽力而为，远端已执行的副作用不能自动回滚。详见 [MCP SDK 版本说明](docs/design/2026-10-13-mcp-sdk-version-compat.md)。

## HTTP MCP OAuth 登录

已支持在项目 MCP 配置中显式声明 HTTP OAuth，并通过 `python scripts/mcp_oauth_login.py --server <name>` 在本地启动授权。使用 MCP 官方 SDK 的 PKCE、授权发现和令牌刷新。**这不是 Web 多用户 OAuth/OIDC 登录**；令牌保存在本机权限受限但未加密的项目缓存。配置与限制见 [MCP OAuth 文档](docs/design/2026-10-14-mcp-http-oauth.md)。

## 项目插件与 Skills

现支持在 Web **「插件」** 页面上传本地 ZIP 插件、启用/禁用、卸载，以及对声明式命令 Hook 单独设置显式信任。Runtime Services、Middleware 与 Plugin 是互相独立的边界；不再接受 Python Feature 插件入口，也不会自动执行第三方 Python/JS/TS 脚本或联网安装包。

插件管理现在支持本地 ZIP 的 **SHA-256 校验、版本锁定、显式更新和上一版回滚**；更新或回滚会自动禁用插件并撤销代码信任。校验和不能替代发布者签名，当前不会自动下载远程插件。详见 [插件升级与回退说明](docs/design/2026-10-14-plugin-skills-lifecycle.md)。

插件包根目录使用 `plugin.json`（也兼容 `.claude-plugin/plugin.json`），并可包含 `skills/<name>/SKILL.md`。安装后默认**禁用**；启用只加载声明式 Skills。旧 `custom_features` 声明会直接拒绝。启用命令 Hook 必须同时有逐插件的内容指纹信任和服务端 `RIDGE_ALLOW_TRUSTED_HOOKS=1`；命令 Hook 在受信任的本地宿主执行，**不是沙箱**，不在托管 Worker 内加载。旧 `RIDGE_ALLOW_PLUGIN_CODE` 已失效。详见 [插件与 Skills 生命周期](docs/design/2026-10-14-plugin-skills-lifecycle.md)。

## 可选 Langfuse / OpenTelemetry 追踪

本地 JSONL/HTML Trace 默认保持独立。远程追踪**默认关闭**，仅导出 Agent turn、模型调用和工具调用的层级、耗时、状态、工具/模型名称和 Token 计数，不导出提示词、文件内容、工具参数/输出或异常堆栈。安装可选依赖：

```bash
python -m pip install -r requirements-observability.txt
```

在私有 `.env` 中选择一个后端：`AGENT_TELEMETRY_BACKEND=langfuse`，并设置 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL`；或者设置 `AGENT_TELEMETRY_BACKEND=otlp` 与 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`（HTTP/protobuf traces URL）。两种后端互斥。

详细配置、数据外发边界和测试说明参见 [Langfuse/OpenTelemetry 接入设计](docs/design/2026-10-13-optional-otel-langfuse-tracing.md)。

Web 侧边栏新增 **Trace** 面板：展示当前会话的本地 Trace ID、调用树、耗时、Token 用量和运行健康度。设置 `TRACE_INPUT_USD_PER_MILLION` 与 `TRACE_OUTPUT_USD_PER_MILLION` 后可显示 USD **估算费用**，不配置就不计算。运行健康度不是答案正确率。详见 [Web Trace 分析设计](docs/design/2026-10-13-web-trace-timeline.md)。

## 测试和开发

```bash
# Python 重点回归
python -m pytest tests/test_web_service.py tests/test_session_manager.py tests/test_session_tree.py tests/test_llm_streaming.py -q

# Web 类型检查和构建
cd desktop/web && npm ci && npm run build

# 新版 TUI 测试（另一个终端）
cd tui-v2 && bun install && bun run typecheck && bun test
```

Web 样式构建使用 Tailwind CSS 4；支持的浏览器最低版本为 Safari 16.4、Chrome 111、Firefox 128。

更完整的测试范围在 `.github/workflows/` 中定义；具体结果以 CI 为准。本地需要按所测试功能配置相应依赖，Docker 集成测试还需要 Docker。

阅读 [文档导航](docs/README.md) 查看最新架构与历史设计的区别；[CONTRIBUTING.md](CONTRIBUTING.md) 介绍开发约定；[品牌与旧命名兼容说明](docs/REBRANDING.md) 记录改名边界和后续迁移工作。

## v0.1 发布准备

已增加不联网、不读取密钥的 [本地环境检查](docs/COMMANDS.md)，可以分别检查 Python、Web 和 Bun/OpenTUI 环境：

```bash
python -m ridgecode doctor --target core
python -m ridgecode doctor --target web --json
python -m ridgecode doctor --target tui --json
```

**v0.1.0 尚未正式发布**：自动化测试通过不代表真实模型与长期运行场景已验收。发布前检查项、人工验收、已知问题与回退说明见 [v0.1 发布准备清单](docs/releases/v0.1-readiness.md)。

## 当前限制

- Web 主要定位可信本机单用户场景；没有提供多租户 RBAC 或安全的公网托管方案。
- Docker 工具隔离为 opt-in 实验能力，不能替代完整的容器安全策略。
- OpenTUI 是唯一维护的终端客户端；仍有少数旧 Python CLI 命令需要通过版本化服务端 API 补齐。
- 模型可用性、推理能力以及工具实际权限取决于运行环境与配置。

**许可证**：[MIT](LICENSE)。
