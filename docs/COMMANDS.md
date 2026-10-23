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

`--prune-empty` 额外删除没有任何消息的会话（存在 `.turn.json`、即有回合进行中的会话会被跳过），`--dry-run --prune-empty` 只列出候选。

支持 ACP 的编辑器（如 Zed）把上面的命令配置为 agent 启动命令即可；会话在进程内创建，进程退出即结束。该模式只面向本地受信任环境，不提供鉴权或多租户隔离。

## 长期记忆

Web 端「工作台 → 记忆」可开关记忆、选择后端（`off` / `sqlite`）、设置每次召回条数，并直接写入、搜索、删除记忆条目。

- 设置：`.mycodeagent/settings/memory.json`；数据：`.mycodeagent/memory/memories.sqlite3`（根目录可用 `MYCODEAGENT_DATA_DIR` 覆盖）
- 默认**关闭**；关闭时 Agent 的工具列表里不会出现 `Memory`，启用后新建的会话才带上它
- **召回**：每回合开始前用用户消息里的词（中文按二元组切分）检索记忆，作为不可信参考注入 context，和 `CODE_LAW` 走同一通道
- **自动记忆**（默认关闭，需手动打开）：回合成功结束后用 compact 模型抽取最多 1–3 条可复用事实，已存在的相似条目跳过；后台线程执行，不阻塞回合、不改变回合结果
- 记忆按工作区（项目路径哈希）隔离，仅本地单用户；`mem0` 需要外部客户端与服务端授权，界面暂不提供

## 工作区指令文件

`AGENTS.md`（opencode 约定）、`CLAUDE.md`（Claude Code 兼容）、`CODE_LAW.md`（旧版 Ridge Code 约定）会被自动读取，按顺序拼成一段**不可信参考**注入每轮 context。单文件上限 12k 字符、合计 20k，超出截断。

**`CODE_LAW.md` 不是必须的**，保留它只是向后兼容；新项目请写 `AGENTS.md`。三份同时存在时会重复，建议只留一份。

```bash
python -m ridgecode init            # 扫描仓库生成 AGENTS.md
python -m ridgecode init --stdout   # 只预览，不落盘
python -m ridgecode init --force    # 覆盖已有 AGENTS.md
```

生成器只读 `package.json`、`Makefile`、pyproject/requirements、目录结构和已有规则文件，不调用模型；不确定的地方留 `<!-- TODO -->` 由人补。

## 模型与 API Key

Web 端「工作台 → 模型」可以新增/覆盖模型配置：名称、模型 ID、provider、base URL、API Key。

- 存储：`.mycodeagent/settings/models.json`（权限 0600，根目录可用 `MYCODEAGENT_DATA_DIR` 覆盖）
- API Key 只在本机保存；`GET /api/settings/models` 与 `/api/models` 只返回 `***末四位`，不返回明文
- 留空 API Key 保存时保留原值，方便只改模型 ID
- 与 `.env` 中 `MODEL_PROFILES` 同名时，**这里的配置优先**；改完新建会话即可选用
- 卡片上的「测试」会发一次极小的请求（`max_tokens=8`），返回耗时与错误原因

### 联网搜索

「工作台 → 模型 → 联网搜索」可选 Tavily / Brave / Serper / Exa / SearXNG / DuckDuckGo：

- 前四家需要各自密钥（`TAVILY_API_KEY`、`BRAVE_API_KEY`、`SERPER_API_KEY`、`EXA_API_KEY`）
- SearXNG 只需实例地址（`http(s)://...`）；DuckDuckGo 不需要密钥
- 选择写入 `.mycodeagent/settings/search.json`；`WEB_SEARCH_PROVIDER` 环境变量优先级更高
- 任一 provider 的凭据缺失时，`WebSearch` 返回明确的配置提示，而不是静默失败

### 密钥存储

- 优先系统凭据库（Windows Credential Locker / macOS Keychain），服务名 `ridgecode`
- 没有可用凭据库时退回 `.mycodeagent/settings/keys.json`，权限 0600
- 环境变量是最后兜底；模型密钥不再写进 `models.json`

## 工具清单

每个工具在 `tools/tool_specs.py` 里有一条声明：`kind`（read/write/exec/network/agent/control）、`read_only`、`approval`（auto/ask/deny）、`exposure`（core/deferred）、`output_budget`、`group`。权限提示、UI 分组、并发策略和 prompt 组装都以这张表为准，不再各自硬编码。

- **每轮展开的只有 14 个 core 工具**，其余（`LS`、`MultiEdit`、`EnterPlanMode`、worktree、`Workflow`、`BrowserUse`、全部团队工具等）只以一行 "Also available: ..." 出现，模型仍可调用
- 工具返回的 `text` 受 `output_budget` 约束，超出会截断并附"用 offset/limit 收窄"的提示；`data` 保持完整，不影响程序化调用
- 「工作台 → 工具」列出全部工具及来源分组、只读/需确认/按需标签

## 上下文压缩

分两段执行，先便宜后昂贵：

1. **修剪工具输出**（不调用模型）：估算上下文超过 `CONTEXT_WINDOW × 0.5` 时，把 `TOOL_PRUNE_KEEP_ROUNDS`（默认 3）轮之前的工具结果替换为「已省略 N 字符，需要时重跑」，最近几轮原文保留
2. **生成摘要**：整请求估算（含工具 schema）超过 `CONTEXT_WINDOW × 0.8` 且减掉 4096 输出预留后，保留最近 `MIN_RETAIN_ROUNDS`（默认 10）轮，把更早历史压成结构化摘要

手动压缩：

```bash
curl -X POST http://127.0.0.1:8000/api/sessions/<sid>/compact \
  -H 'Content-Type: application/json' -d '{"focus":"保留 API 设计细节"}'
```

TUI 里是 `/compact [FOCUS]`。`focus` 会作为一条「用户指定的压缩焦点」拼进摘要输入，指向要保留的内容。

压缩后摘要末尾会附一个 `<files_touched>` 清单（仅路径，最多 20 条），用于恢复"改过哪些文件"的方位感；**不自动重读文件内容**，需要细节时由模型按路径 `Read` 指定行。

参数可在「工作台 → 模型 → 上下文压缩」里改（写入 `.mycodeagent/settings/context.json`，对新会话生效）；Web 顶栏也有「压缩上下文」按钮。

### 度量压缩参数

不调用模型，用合成会话对比不同配置：

```bash
python -m scripts.context_metrics --json
python -m scripts.context_metrics --rounds 80 --tool-chars 1500 --text-chars 1500
```

60 轮、每轮 12k 字符工具输出、128k 窗口的实测：

| 场景 | 修剪条数 | 压缩次数 | 摘要字符 | 峰值 token | 结束 token |
|---|---|---|---|---|---|
| 当前默认（0.8 / 修剪 0.5） | 53 | 0 | 0 | ~69k | ~70k |
| 关闭修剪 | 0 | 4 | 7.7k | ~104k | ~79k |
| 提前压缩（0.6） | 53 | 0 | 0 | ~69k | ~70k |
| 延后压缩（0.9） | 53 | 0 | 0 | ~69k | ~70k |

文本型会话（80 轮、1.5k 工具 + 1.5k 文本）里两者都会压缩 1 次：开修剪结束 ~58k，关修剪 ~100k。

结论：**维持 0.8 / 0.5 默认值**。修剪足以把工具密集型会话压在摘要阈值以下，摘要只在文本持续累积时兜底；0.9 会把文本型会话峰值推到 ~117k，离窗口上限太近，不建议作为默认。

安全性：摘要超时、返回空、超过输出预算、或连续失败 3 次时都会**保留原始历史**，不执行破坏性截断；旧摘要参与新摘要合并，只保留一条；原始用户目标以 `<task_continuity>` 锚点跨多次压缩保留。

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
