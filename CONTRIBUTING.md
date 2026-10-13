# 参与 Ridge Code 开发

本项目以 **实现和测试为准**，不再通过特定模型厂商的根目录指令文件维护工程约束。`README.md` 负责快速开始，`docs/design/` 保存设计决策与历史背景，`.github/workflows/` 中的 CI 文件描述持续验证范围。

## 模块边界

- Python Agent 逻辑保留在 `agents/`、`core/`、`tools/`；UI 不应通过覆写 Agent 私有方法实现功能。
- `core/runtime/` 负责会话生命周期和事件传播；`desktop/service/application/` 负责 API 业务流程；HTTP/WS 路由避免直接承载复杂业务逻辑。
- `tui-v2/src/api/` 是传输适配层，`src/state/` 负责纯状态投影，`src/controller.ts` 编排行为，`src/ui/` 只渲染和转发用户意图。
- 保持必要的旧环境变量与数据目录兼容；影响磁盘内容、会话或用户设置的命名迁移需另起 PR，附升级与回退说明。
- 任何访问文件、执行命令或改变权限的代码，都应有覆盖路径越界、竞争条件和拒绝路径的测试。

## 提交前检查

```bash
python -m pytest tests/ -q
cd desktop/web && npm ci && npm run build
cd ../../tui-v2 && bun install && bun run typecheck && bun test
```

完整测试需要相应依赖；Docker 集成场景使用仓库 CI 中独立的工作流命令。不能执行的测试应在 PR 中写明原因，不要宣称已通过。

## PR 约定

1. 明确问题、影响模块和行为变化；拆分纯文档变更与功能性迁移。
2. 描述如何手动复现与验证；若涉及事件 DTO、磁盘格式、权限或工具协议，说明兼容性。
3. 不提交 `.env`、密钥、会话记录或本机缓存。
4. 使用 `feat:`、`fix:`、`refactor:`、`docs:` 等前缀命名提交。

历史架构资料不是绝对规则：如与现行源码矛盾，请修正相关文档并补充回归测试。
