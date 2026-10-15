# Ridge Code 品牌与兼容策略

2026-10-13 起，对外产品名称统一为 **Ridge Code**（界面短名 **Ridge**）。

## 已调整

- 根目录 README、贡献规范以及主要 Web/TUI 标题采用新品牌。
- Web npm 包标识调整为 `ridgecode-web`，TUI 私有包标识为 `@ridgecode/tui`。
- TUI v2 新增 `RIDGECODE_SERVER` 环境变量入口。
- 删除根目录 `CLAUDE.md`：其内容与现状存在偏差，开发约定转移到 `CONTRIBUTING.md` 与现行设计/测试。
- 源码中的框架类 `CodeAgent` 仍为有效的内部 API 名称，它不是对外品牌。

## 暂不迁移的兼容标识

| 标识 | 当前处理 | 原因 |
| --- | --- | --- |
| `.mycode/`、`.mycodeagent/` | 保留 | 已有插件、技能和会话数据路径 |
| `MYCODEAGENT_DATA_DIR` | 保留 | 现有 Web 服务环境配置 |
| `WODEAGENT_SERVER`、`WODEAGENT_AUTH_TOKEN` | 已删除旧回退 | 仅接受 `RIDGECODE_SERVER`、`RIDGECODE_AUTH_TOKEN` |
| `wodeagent-auth-expired` | 保留在前端内部事件 | 避免破坏现有组件通讯 |
| `agents/codeAgent.py` 与 `CodeAgent` | 保留 | Python 导入和公共调用链 |
| GitHub 仓库 `bei-shan/WodeAgent` | **尚未改名** | 仓库设置层面另行操作 |

不要仅为消除旧字符串而直接改动持久化路径、导入路径或协议标识。后续如迁移，应提供数据备份、双读/回退和验证测试。

## GitHub 仓库改名（待执行）

在仓库 Settings → General → Repository name 中，将 `WodeAgent` 改为 `ridgecode`（先确认名称可用）。这一步会改变克隆地址、徽章、Web 端源码链接和部署配置；改名后应同步更新 README 的克隆示例、`desktop/web/src/App.tsx` 的 `GITHUB_URL`，以及 CI、远程仓库、文档中使用的旧 URL。GitHub 一般会对旧仓库 URL 提供重定向，但不应仅依赖重定向完成集成迁移。

本次修改仅涉及代码仓库中的文件，并不代表 GitHub 仓库本身已改名。

## 新版 CLI（2026-10-15）

本地命令统一从仓库根目录执行 `python -m ridgecode --help`。OpenTUI 只读取 `RIDGECODE_SERVER` 与 `RIDGECODE_AUTH_TOKEN`；旧 `WODEAGENT_*` 环境变量别名不再作为命令入口的一部分。历史仓库与磁盘路径仍需在单独的数据迁移中处理，不能通过删除已有数据目录冒充清理。详见 [命令参考](COMMANDS.md)。
