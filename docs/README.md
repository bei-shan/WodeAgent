# 文档导航

> **状态说明（2026-10-14）**：本页是 Ridge Code 文档的阅读入口。架构和操作说明优先看当前源码、测试及 CI；带日期的设计稿是当时的设计决策，不代表现行 API 或安全保证。

## 现在应该读什么

| 目的 | 推荐文档 |
| --- | --- |
| 安装、运行 Web/TUI | [根目录 README](../README.md)、[TypeScript TUI](../tui-v2/README.md) |
| 模块边界、依赖方向、尚未完成的迁移 | [当前架构](ARCHITECTURE.md) |
| 开发约定和测试 | [CONTRIBUTING](../CONTRIBUTING.md)、[CI](../.github/workflows/) |
| 工具返回协议 | [通用工具响应协议](通用工具响应协议.md)（设计参考；以实现与协议测试为准） |
| 本地鉴权 | [Web 认证设计](design/2026-10-13-local-web-auth-phase1.md)、[TUI 鉴权](design/2026-10-13-ridge-tui-local-auth.md) |
| Sandbox 与 VM | [Docker 命令隔离](design/2026-10-13-docker-sandbox-phase1.md)、[容器租约](design/2026-10-15-docker-lease-reaper.md)、[BoxLite micro-VM](design/2026-10-15-boxlite-vm-provider.md) |
| 工作区访问安全 | [Descriptor-anchored workspace](design/2026-10-13-descriptor-anchored-workspaces.md) |
| 项目外文件授权作用域 | [按读取/修改分离的权限缓存](design/2026-10-14-operation-scoped-permissions.md) |
| 品牌与旧配置兼容 | [品牌迁移说明](REBRANDING.md) |
| 生产化缺口与优先级 | [2026-10-14 生产审计与 Backlog](AUDIT_2026-10-14_PRODUCTION.md) |
| 仓库历史清理 | [2026-10-13 审计记录](AUDIT_2026-10-13.md) |

## Skills 与插件

- [插件与 Skills 加载设计](design/2026-10-14-plugin-skills-lifecycle.md)：ZIP 安装、启用/信任、动态发现、资源安全与当前限制

## 稳定版发布准备

- [v0.1 准备清单](releases/v0.1-readiness.md)：自动化门禁、真实环境验收与当前阻塞项
- [v0.1 会话运行时加固](design/2026-10-14-v01-lifecycle-stabilization.md)：取消交互、避免运行中删除、原子终止状态

## 重构设计资料

- [2026-10-13 核心架构参考](design/2026-10-13-reference-agent-architecture.md)
- [2026-10-13 TypeScript TUI 分层](design/2026-10-13-tui-v2-layered-architecture.md)
- [2026-10-13 Python TUI 退役](design/2026-10-13-python-tui-retirement.md)
- [2026-10-13 AgentLoop / ContextRuntime](design/2026-10-13-agent-loop-context-runtime.md)
- [2026-10-13 Application Services](design/2026-10-13-application-services-refactor.md)
- [2026-10-13 工具运行时拆分](design/2026-10-13-tool-runtime-extraction.md)
- [2026-10-13 上下文、DeerFlow 与安全比较](design/2026-10-13-context-deerflow-sandbox-auth.md)

**历史归档**：[docs/archive/README.md](archive/README.md)。早期的逐工具 MVP 设计、旧交接材料和 2026 年 6 月路线图已集中归档；不再作为实时的项目状态或实施任务源。

## 维护规则

1. 新的架构性决定在 `docs/design/YYYY-MM-DD-*.md` 留记录，并附上代码与测试路径。
2. 正在维护的系统事实写在 `ARCHITECTURE.md`；过时或被取代的方案标为历史，避免出现多个「最终方案」。
3. 大量临时截图、生成网页、实验输出不纳入产品文档；必要时使用独立 release 附件，不直接塞入源码仓库。
4. 设计稿不应声称 CI「当前通过」，除非指向实际可验证的运行结果。
