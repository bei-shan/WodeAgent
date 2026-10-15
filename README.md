# Ridge Code

**Ridge** 是一个本地优先、可扩展的 AI 编程代理项目：使用 Python 实现 Agent 引擎，由同一套会话运行时同时服务 Web 和终端界面。项目目前处于持续开发阶段，适合个人开发、学习与实验，并非开箱即用的多用户 SaaS。

- **Agent 引擎**：Python 3.12+、ReAct、工具调用、可组合 Feature、模型路由
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
python -m pip install -r requirements.txt
cp .env.example .env
```

在 `.env` 中配置模型供应商、模型和凭据。也可以运行首次配置向导：

```bash
python scripts/first_run_wizard.py
```

`.env.example` 与 `core/config.py` 是配置项的主要参考；实际的持久化目录暂时仍沿用旧名称，以保持现有数据兼容。

### 2. 启动 Web

在仓库根目录运行服务端：

```bash
python -m uvicorn desktop.service.app:create_app --factory --host 127.0.0.1 --port 8000
```

另开终端运行前端：

```bash
cd desktop/web
npm ci
npm run dev
```

打开 [http://localhost:5173](http://localhost:5173)。前端通过开发代理连接本机 API，支持会话、流式消息、文件浏览、权限交互、模型配置与技能管理等界面。服务端提供 `/docs` API 文档。

**本地登录（可选）**：在私有 `.env` 设置 `WEB_AUTH_TOKEN` 后重启服务端，Web 会启用基于 Cookie 的本地登录流程；未设置时仍为无登录开发模式。参见 [本地 Web 认证设计](docs/design/2026-10-13-local-web-auth-phase1.md)。这不是公网部署或多用户权限控制方案。

### 3. 启动 TypeScript TUI（实验性）

先启动上述 Python 服务端，再运行：

```bash
cd tui-v2
bun install --frozen-lockfile
bun run typecheck
bun test
bun run dev
```

可以使用 `RIDGECODE_SERVER` 指定服务端地址；旧的 `WODEAGENT_SERVER` 继续作为兼容回退。启用服务端 `WEB_AUTH_TOKEN` 后，TUI 可使用 `RIDGECODE_AUTH_TOKEN` 设置相同令牌，通过 Bearer 请求头认证（兼容旧变量 `WODEAGENT_AUTH_TOKEN`）；具体限制见 TUI 文档。参见 [TUI v2 说明](tui-v2/README.md)。

旧 Python Rich/prompt_toolkit TUI 已退役，OpenTUI 是唯一维护的交互式终端客户端。常用会话、模型与 Plan/Teams 命令，以及 `/tree`、`/fork` 和可移植会话 `/export`、`/import` 已迁移。可移植归档不包含工具执行环境或密钥，不能视为完整快照备份。仍未迁移的旧 CLI 命令详见 [终端迁移清单](docs/design/2026-10-13-tui-command-migration.md)。

## 架构概览

```text
Web (desktop/web)          OpenTUI (tui-v2)
           \                   /
            FastAPI / runtime events
                     |
       core/runtime + core/events
                     |
         agents/codeAgent.py
                     |
      +--------------+--------------+
  Context Engine    Features      Tool Runtime
  + Sessions        + Teams       + MCP / Skills
                     |
            Model / LLM adapters
```

- `agents/`：Agent 主循环及 Feature 集成点
- `core/runtime/`、`core/events.py`：会话执行、生命周期事件与 UI 解耦
- `core/context_engine/`：历史、摘要、上下文构建、Trace
- `core/features/`、`core/team_engine/`：特性扩展与多代理协同
- `tools/`、`core/sandbox/`：工具与执行边界
- `desktop/service/application/`：Web 后端的应用服务层
- `desktop/web/`、`tui-v2/`：独立的 React 与 OpenTUI 界面

设计细节参见 [架构参考方案](docs/design/2026-10-13-reference-agent-architecture.md)、[TS TUI 分层设计](docs/design/2026-10-13-tui-v2-layered-architecture.md) 和 [工作区安全说明](docs/design/2026-10-13-descriptor-anchored-workspaces.md)。`docs/` 内一些较早的设计材料为历史记录，不应视为当前功能承诺。

## 本地运行的并发保护

Web 服务为正在执行的 Agent 回合设置进程内的并发上限：
`RIDGE_MAX_ACTIVE_TURNS=8`（默认 8，可配置 1–256）。当其他会话已占满容量时，新任务返回 HTTP 429 与 `Retry-After`，不创建无限排队任务；正在执行或等待权限批准的回合也会占用名额。健康接口显示当前活动数、上限和拒绝次数。详见 [并发准入设计](docs/design/2026-10-15-bounded-turn-admission.md)。

**这不是分布式任务队列或多租户配额**，多 Uvicorn Worker 会各自维护计数器；部署时仍应使用单进程受信任本地模式，等完成统一鉴权、共享数据库与分布式任务租约后再扩容。

## MCP SDK 与连接稳定性

默认使用 MCP Python SDK **2.x**；HTTP 采用新版 Streamable HTTP 客户端，同时保留独立 CI 通道测试 MCP 1.x 的兼容性。MCP stdio 子进程和 HTTP 会话由同一异步所有者管理，同一客户端连续同步工具调用会复用连接，不再每次启动新的 MCP Server。测试包含本地真实 stdio 和 Streamable HTTP 服务，覆盖初始化、调用、资源关闭及超时／取消。

这不表示已经验证所有第三方 MCP Server 的 OAuth、SSE 续传或跨网络故障场景。工具取消为尽力而为，远端已执行的副作用不能自动回滚。详见 [MCP SDK 版本说明](docs/design/2026-10-13-mcp-sdk-version-compat.md)。

## HTTP MCP OAuth 登录

已支持在项目 MCP 配置中显式声明 HTTP OAuth，并通过 `python scripts/mcp_oauth_login.py --server <name>` 在本地启动授权。使用 MCP 官方 SDK 的 PKCE、授权发现和令牌刷新。**这不是 Web 多用户 OAuth/OIDC 登录**；令牌保存在本机权限受限但未加密的项目缓存。配置与限制见 [MCP OAuth 文档](docs/design/2026-10-14-mcp-http-oauth.md)。

## 项目插件与 Skills

现支持在 Web **「插件」** 页面上传本地 ZIP 插件、启用/禁用、卸载，以及对包含 Python Feature / Hook 的插件单独设置代码信任。项目原有的 Skill、MCP、Feature 是三种不同的扩展层：插件包可以声明 Skills，但不会自动执行第三方脚本或联网安装包。

插件管理现在支持本地 ZIP 的 **SHA-256 校验、版本锁定、显式更新和上一版回滚**；更新或回滚会自动禁用插件并撤销代码信任。校验和不能替代发布者签名，当前不会自动下载远程插件。详见 [插件升级与回退说明](docs/design/2026-10-14-plugin-skills-lifecycle.md)。

插件包根目录使用 `plugin.json`（也兼容 `.claude-plugin/plugin.json`），并可包含 `skills/<name>/SKILL.md`。安装后默认**禁用**；启用只加载声明式 Skills。执行插件自定义代码需要同时设置显式信任与服务端 `RIDGE_ALLOW_PLUGIN_CODE=1`，此时是宿主机内执行代码，**不属于沙箱**。详见 [插件与 Skills 生命周期](docs/design/2026-10-14-plugin-skills-lifecycle.md)。

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

更完整的测试范围在 `.github/workflows/` 中定义；具体结果以 CI 为准。本地需要按所测试功能配置相应依赖，Docker 集成测试还需要 Docker。

阅读 [文档导航](docs/README.md) 查看最新架构与历史设计的区别；[CONTRIBUTING.md](CONTRIBUTING.md) 介绍开发约定；[品牌与旧命名兼容说明](docs/REBRANDING.md) 记录改名边界和后续迁移工作。

## v0.1 发布准备

已增加不联网、不读取密钥的 [本地环境预检](scripts/v01_preflight.py)，可以分别检查 Python、Web 和 Bun/OpenTUI 环境：

```bash
python scripts/v01_preflight.py --target core
python scripts/v01_preflight.py --target web --json
python scripts/v01_preflight.py --target tui --json
```

**v0.1.0 尚未正式发布**：自动化测试通过不代表真实模型与长期运行场景已验收。发布前检查项、人工验收、已知问题与回退说明见 [v0.1 发布准备清单](docs/releases/v0.1-readiness.md)。

## 当前限制

- Web 主要定位可信本机单用户场景；没有提供多租户 RBAC 或安全的公网托管方案。
- Docker 工具隔离为 opt-in 实验能力，不能替代完整的容器安全策略。
- OpenTUI 是唯一维护的终端客户端；仍有少数旧 Python CLI 命令需要通过版本化服务端 API 补齐。
- 模型可用性、推理能力以及工具实际权限取决于运行环境与配置。

**许可证**：[MIT](LICENSE)。
