# MyCodeAgent 改进追踪

> 最后更新：2026-06-18
> 当前分支：`fix/mcp-lazy-connect-thread-safe`（基于 `main`，11 commits）

---

## 一、已完成（全部清空）

### MCP 集成（8 个 bug 全部修复）

| # | 严重度 | Bug | 修复方式 | Commit |
|---|--------|-----|----------|--------|
| 1 | 🔴 | `connect_mode` 默认 `"manual"` 无懒连接 | 后台 daemon 线程 + `_PendingServer` + `retry_pending_server()` | `70d7851` |
| 2 | 🔴 | MCP 客户端非线程安全 | `threading.Lock` + 双重检查 + 每次调用独立 event loop | `70d7851` |
| 3 | 🟡 | `connect()` 竞态 | 双重检查锁定（快速路径无锁 + 慢路径锁内再检查） | `70d7851` |
| 4 | 🟡 | `adapter.py` 死代码 | `client.py` 抛标准异常，catch 分支自动激活 | `492c120` |
| 5 | 🟡 | stdio 连接失败子进程泄漏 | 并发竞态已消除（锁） | `70d7851` |
| 6 | 🟡 | 无 MCP 调用超时 | `asyncio.wait_for` + `MCP_CALL_TIMEOUT`(30s) + `MCP_LIST_TOOLS_TIMEOUT`(60s) | `492c120` |
| 7 | 🟡 | `_load_from_files` 静默吞异常 | 分级日志 | `492c120` |
| 8 | 🟢 | `close_sync()` 关闭共享 event loop | 每次调用独立临时 loop | `70d7851` |
| 9 | — | `connect()` 无超时（子进程可永久挂起） | `asyncio.wait_for` 包裹 transport open + session init | `ea4d3b8` |
| 10 | — | MCP 超时时间太短（5s，npx 首次需 10-30s） | `MCP_CONNECT_TIMEOUT` 默认 30s | `ea4d3b8` |
| 11 | — | MCP 超时消息显示在用户对话界面 | `warning` → `info`（info 级别默认不输出） | `3c80f2c` |

### 代码质量

| # | 问题 | 修复方式 | Commit |
|---|------|----------|--------|
| 12 | `_extract_*` 在 3 个文件中重复 | 抽取 `core/response_parser.py` | `cc4fe6a` |
| 13 | `task.py` 5 个死代码方法 | 删除 | `cc4fe6a` |
| 14 | diff 预览格式不一致 | `edit_file.py` 去掉 `lstrip()` | `7836a67` |
| 15 | `_parse_frontmatter` 重复 | `skill.py` 从 `skill_loader` 导入 | `7836a67` |
| 16 | `_react_loop` ~280 行 | 拆为 5 个方法 | `7836a67` |
| 17 | `datetime.utcnow()` 已废弃 | 改为 `datetime.now(timezone.utc)` | `8ef3126` |

### 安全加固

| # | 问题 | 修复方式 | Commit |
|---|------|----------|--------|
| 18 | BashTool `shell=True` + 命令注入 | 阻止命令替换、绝对路径重定向；`shell=False`+`shlex.split` | `3dc5df6` |
| 19 | WriteTool 临时文件残留 | `try/finally` 清理 | `3dc5df6` |
| 20 | ReadTool._mtime_cache 无限增长 | `_MAX_CACHE_SIZE=1000`，超限淘汰 | `7836a67` |

### 跨平台兼容

| # | 问题 | 修复方式 | Commit |
|---|------|----------|--------|
| 21 | Windows CRLF 换行符不一致 | `write_text()` → `open(path, 'w', newline='')` | `36dac94` |
| 22 | 路径分隔符 `\` vs `/` | `as_posix()` 统一 POSIX 格式 | `36dac94` |

### 可观测性 + 测试

| # | 修复 | Commit |
|---|------|--------|
| 23 | `TraceSpan` 结构化 Tracing | `802c01a` |
| 24 | 提示词 docstring 统一 | `802c01a` |
| 25 | Write tool parametrize 边界测试 (+9) | `802c01a` |
| 26 | 端到端测试 (+8) | `2b8e418` |

---

## 二、待办（已清空）

无。所有 P0/P1/P2/P3 项已完成。

剩余低优先级观察项（非必须）：

- 23 处 `pragma: no cover`（防御性 `except Exception`，属正常工程实践）
- `test_grep_success_no_matches` 一个已知 GrepTool 行为差异（`partial` vs `success`）
- 动态工具提示词裁剪、ToolRegistry 拆分（投入产出比低，已搁置）

---

## 三、分支状态

```
3c80f2c fix: demote MCP pending/timeout messages from warning to info
ea4d3b8 fix: increase MCP connect timeout and add connect-level timeout protection
36dac94 fix: cross-platform CRLF and path separator compatibility
8ef3126 fix: replace deprecated datetime.utcnow()
2b8e418 test: end-to-end agent tests with ScriptedMockLLM
802c01a feat: structured tracing, prompt docstrings, parametrized boundary tests
7836a67 refactor: P2 maintainability improvements
3dc5df6 fix: BashTool hardening, WriteTool temp cleanup, Skills tests, SendMessage prompt
cc4fe6a refactor: extract shared LLM response parsing to core/response_parser.py
492c120 fix: MCP call timeout with asyncio.wait_for and config error logging
70d7851 fix: MCP lazy connect with background threads and thread-safe client
848463d docs: add MyCodeAgent to references          ← main
```

---

## 四、测试覆盖

| 类别 | 测试数 | 变化 |
|------|--------|------|
| MCP client | 19 | **新增** |
| MCP loader | 24 | **新增** |
| E2E Agent | 8 | **新增** |
| Bash 工具 | 22 | **+11** |
| Skills | 12 | **+9** |
| Write parametrize | 9 | **+9** |
| Trace logger | 7 | **+4** |
| 其他（25 类） | ~530 | 无变化 |
| **总计** | **~630** | +84 |

## 五、整体评级

**当前：A-**

11 commits，31 文件，+3162/-750 行。
MCP 可靠性、安全性、代码质量、跨平台兼容、测试覆盖全部完成。
待办已清空。
