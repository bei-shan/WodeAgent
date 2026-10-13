# 2026-06-30 稳定性收口设计

## 背景

项目已经越过玩具 agent 阶段，核心能力覆盖 ReAct 主循环、工具协议、上下文工程、SessionController、Web/TUI、子代理与 AgentTeams。当前阶段的主要风险不再是能力缺失，而是复杂度继续增长时，安装、测试、恢复语义和跨平台行为没有同步收口。

本阶段目标是把项目从“功能扩张期”切换到“稳定性收口期”：先保证新环境能装、核心测试能跑、失败行为可解释，再继续做结构拆分。

## 第一性原理

一个 code agent 框架要可持续迭代，至少要满足以下约束：

1. 可复现：按文档安装后，TUI、Web 和测试入口都能启动。
2. 可验证：主要测试在当前支持平台上稳定通过，失败用例代表真实问题而不是环境偶然性。
3. 可隔离：恢复状态、启动后台 worker、消费队列等动作必须有清晰边界。
4. 可组合：Feature、工具、运行时和 UI 之间通过明确接口协作，避免隐式副作用。
5. 可渐进：大文件和大服务可以逐步拆分，但每一步都要保持行为可验证。

## 当前证据

- `python -m pytest tests/ -q` 当前可执行，但有 4 个失败：3 个来自 Windows 下 `BashTool` 对 `pwd` 的 shell 语义不一致，1 个来自 Team restore 后 worker 自动消费 requeued work item。
- `desktop/web` 的生产构建通过，说明前端当前可编译。
- `desktop/service/app.py` 依赖 FastAPI/Uvicorn，但根 `requirements.txt` 没列 `fastapi`，干净 venv 无法导入 Web 服务。
- `TeamManager.import_state()` 同时承担“恢复快照”和“启动 worker”两个职责，导致状态恢复测试不确定。
- `CodeAgent`、`TeamManager`、`desktop/service/app.py` 仍偏大，但本阶段不做大拆分，先把边界固定住。

## 本阶段范围

### 1. 安装依赖收口

把 Web 服务实际需要的依赖加入 `requirements.txt`：

- `fastapi`
- `uvicorn`

`uvicorn` 虽然可能由 `mcp` 间接带入，但直接运行 Web 服务是项目公开入口，必须显式声明。

### 2. BashTool 跨平台语义收口

`BashTool` 的名字保留，但实际运行环境是平台默认 shell。为了兼容现有测试与 agent 习惯，提供最小 shell 兼容层：

- Windows 下简单命令 `pwd` 转换为 `cd`，保持 `shell=False` 的安全路径。
- Windows 下命令链中的独立 `pwd` 转换为 `cd`，让 `cd sub && pwd` 与 `CD sub && pwd` 成立。
- 不改变 `ls/cat/grep/find/rg` 等读搜命令的阻断策略。
- 不扩展到复杂 shell 语法模拟，只修正当前项目声明与测试已经要求的工作目录查询语义。

### 3. Team restore 语义收口

将 `TeamManager.import_state()` 调整为只做恢复动作：

- 读取已有 team。
- 将 `running` work item 重新入队为 `queued`。
- 恢复 processed message ids、inbox cursors、approval requests。
- 不自动启动 worker。

新增显式方法 `resume_workers()`：

- 遍历现有 team member。
- 为非 `lead` 成员启动 worker。
- 返回启动数量，便于后续 UI 或恢复流程明确调用。

这样恢复状态和消费队列分离，测试可以稳定验证 requeue，真实运行需要继续执行时也有显式入口。

### 4. 测试调整

修改与新语义相关的测试：

- restore 测试验证 `import_state()` 不自动消费队列。
- worker 恢复测试改为先 `import_state()` 再 `resume_workers()`。
- 保留已有并行 worker 测试，不改变 `fanout_work()` 自动启动 worker 的行为。

### 5. 暂不处理项

以下问题确认存在，但不在本次最小完善范围：

- `CodeAgent`、`TeamManager`、`desktop/service/app.py` 的系统性拆分。
- Web 鉴权、CORS 收敛、WebSocket 双向控制。
- TUI 完全切到 `SessionController`。
- AgentTeams worktree store 隔离策略。

## 验收标准

1. `python -m pytest tests/test_bash_tool.py tests/test_team_parallel_session_restore.py tests/test_agent_teams_integration.py tests/test_team_session_persistence.py -q` 通过。
2. `python -m pytest tests/ -q` 全量测试通过或只剩明确记录的环境依赖问题。
3. `cmd /c npm run build` 在 `desktop/web` 下通过。
4. `requirements.txt` 显式包含 Web 服务直接依赖。

## 后续建议

本阶段完成后，下一批应优先做三个结构化收口：

1. 拆 `desktop/service/app.py` 为 sessions/files/config/skills/mcp/hooks/team 路由模块。
2. 拆 `TeamManager` 的 worker lifecycle、snapshot restore、runtime display。
3. 让 `CodeAgent` 主循环进一步只保留 ReAct orchestration，把 session persistence、plan file 写入、style/model 切换继续 Feature 化。
