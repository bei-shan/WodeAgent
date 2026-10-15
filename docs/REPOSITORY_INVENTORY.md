# 仓库清理与保留理由（2026-10-15）

## 审查原则

本轮以 GitHub `main` 树为基线，盘点 **635 个受 Git 跟踪的文件**，按职责划分为生产代码、执行工具、测试、运维/CI、用户文档、架构设计历史。禁止仅凭命名为 `legacy`、`old`、`v01` 就删除仍被 CI、测试或真实调用链使用的代码。清理的目标是让**现役入口唯一、安全边界明确、过期文件不再冒充当前事实**。

## 本轮明确删除的内容

- `docs/archive/` 共 **23** 个纯历史交接、2026 年 6 月路线图、过期逐工具 MVP 设计文档；历史可以通过 Git 版本追溯，不应与当前架构导航并列。
- 旧 `docs/design/2026-06-29-feature-system-reflections.md`：描述已废弃的 AgentFeature 体系；现有服务/中间件架构以 `docs/ARCHITECTURE.md` 为准。
- 根目录空 `__init__.py`：没有实际 Python 包装行为，反而模糊仓库根与安装包边界。
- OpenTUI 入口里 `WODEAGENT_SERVER`、`WODEAGENT_AUTH_TOKEN` 两个旧环境变量回退；只接受 `RIDGECODE_SERVER`、`RIDGECODE_AUTH_TOKEN`。

## 命令与入口整理

日常命令统一为 `python -m ridgecode`：

- `setup [--force]`：显式向导；保护已有 `.env`，新配置原子写入且权限受限；
- `doctor [--target core|web|tui|all] [--json]`：本地离线环境检查；
- `serve [--host 127.0.0.1] [--port 8000]`：受信任本地 API，拒绝非 loopback 绑定；
- `web`、`tui`：使用固定工作目录和无 Shell 的进程调用启动 UI；
- `eval`、`soak`：独立离线评测与模拟稳态测试。

`scripts/` 中的底层实现仍由测试、CI 和发布证据任务依赖；本轮**不删除** `release_acceptance.py`、`staging_tenant_acceptance.py`、`mcp_oauth_login.py`、`agent_eval.py`、`agent_soak.py`、`v01_preflight.py`。它们不会作为第二套用户命令写入 README。

## 保留目录与原因

| 路径 | 是否保留 | 判断依据 |
| --- | --- | --- |
| `agents/`、`core/runtime/` | 保留 | 当前 AgentLoop、会话生命周期、权限、中间件及持久 Worker 租约 |
| `core/context_engine/`、`prompts/` | 保留 | 会话历史、压缩、模型提示词依赖 |
| `core/team_engine/`、`core/sandbox/` | 保留 | Agent Teams 及多种隔离供应商的现役实现与回归 |
| `core/skills/`、`skills/` | 保留 | 内置 Skill 和相关素材，删除会改变用户能力 |
| `tools/` | 保留 | 内置工具、注册表、MCP 生命周期及权限机制 |
| `desktop/service/`、`desktop/web/` | 保留 | 本地/托管元数据 API 与 React UI |
| `tui-v2/` | 保留 | 唯一维护的交互式终端客户端 |
| `db/migrations/`、`core/persistence/` | 保留 | 已部署的数据定义不能在源码整理时直接删除 |
| `tests/`、`.github/workflows/` | 保留 | 当前回归、租户隔离、资源稳态和发布证据依赖 |
| `requirements*.txt`、锁文件 | 保留 | 各运行模式的可复现依赖 |
| `docs/design/` | 选择保留 | 大部分为带日期的历史决策或现行安全协议；不作为使用手册 |
| `docs/archive/` | 删除 | 已过时、与运行不连接的纯快照 |
| `.mycode/`、`.mycodeagent/` | 不清理用户磁盘数据 | 它们是被忽略的现存项目状态，删除需要独立数据迁移和备份计划 |
| `mcp_servers.json` | 保留 | 项目 MCP 配置样例；不能仅凭“可选”判定无用 |

## 生产与风险提示

本轮是**静态仓库结构、文档和命令入口**整理，不是托管多租户 Agent 的生产验收。旧命令和环境别名清理是明确的不兼容变更。生产级交付仍需真实 Runner、租户专属沙箱/密钥、权限撤销与并发/故障测试。不要把删除历史文件误当成这些功能已经实现。

维护门禁：`tests/test_repository_hygiene.py` 约束删除内容不能复活，`tests/test_ridgecode_cli.py` 约束 CLI 公网监听和密钥覆盖，`scripts/check_docs_links.py` 检查现役文档链接。更完整的验收以 GitHub Actions 和真实环境证据为准。
