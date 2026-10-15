# Ridge Code：多租户与可插拔 Memory 架构（2026-10-15）

> 状态：**设计 + 独立 Memory 核心原型；尚未接入 Agent 回合、Web 设置或正式多租户鉴权**。
> 基于 `bei-shan/WodeAgent` 的 `main` 实际代码。任何“高并发就绪/生产多租户已完成”的说法均不成立。

## 1. 当前审计：能并行，不等于能水平扩展

| 已有机制（核实路径） | 边界 / 风险 |
| --- | --- |
| `core/runtime/session_controller.py`: 每个会话专用 worker thread，同一会话一次只允许一个 turn；事件日志驻留进程 | 无全局活跃 turn 上限、无按租户配额；多个 Web worker 的会话及事件不可相互发现 |
| `core/runtime/tool_scheduler.py`: 只读/安全工具的批次有限并发，默认 4 | 这是单回合工具线程池，不是全局 admission control |
| `core/session_manager.py`: JSON snapshots + 单一 `index.json` | `_load_index→_write_index` 读-改-写无跨线程/进程事务；`<sid>.tmp` 共用临时名；并发写会丢更新或覆盖。**首先解决此数据安全问题** |
| `desktop/service/local_auth.py`: 单操作者 Token/Cookie | 不是 OIDC、不是租户身份、没有角色、没有资源级授权 |
| `core/sandbox/`: Docker / BoxLite 可选执行隔离 | 主机工具、插件、网络策略、文件挂载及租户级沙箱身份仍需强制治理 |
| `scripts/agent_soak.py`: 单机离线重复会话与泄漏检测 | 不是多租户压力测试、跨进程崩溃恢复或真实模型服务 SLO 验证 |

### 生产高并发必需的不变量

1. Auth Gateway 校验 OIDC/OAuth2 token；**服务器**构造 `Principal(tenant_id,user_id,roles)`。禁止信任请求中自报的 `X-Tenant-ID` 或模型工具参数。
2. 资源 `session, project, artifact, workspace, sandbox, plugin, credential, memory, job, trace, SSE` 都必须从授权 Principal 派生所有权；拒绝跨租户 IDOR，包括导出、fork、恢复、权限批准和后台任务回调。
3. PostgreSQL 事务承载 sessions、turns、task 状态、幂等键、配置与审计；租户键参与唯一约束/索引/查询谓词，可结合 PostgreSQL RLS 做纵深防御。SQLite 只允许本地单机。
4. API Gateway 无状态，执行通过 durable queue / worker pool，按 `tenant,user,project,model,tool,sandbox` 做公平准入和资源令牌。全局、租户、用户 3 级并发上限，活跃 lease + fencing token + heartbeat + TTL，释放/重试必须幂等。
5. 持久事件流和重放游标替代纯内存 SSE journal；API 副本间共享状态。可选 Redis 做原子配额 / stream bridge，不能把 Redis 当作唯一不可恢复的任务账本。
6. 每个 turn、sandbox、MCP 和 LLM 请求设预算与取消边界；满载返回明确 429/503 + Retry-After 或可取消的排队任务。所有队列有上限，shutdown drain，超时释放 permit。
7. Sandbox 以租户/会话身份启动并只装载其工作区，默认拒绝宿主执行、共享容器卷、跨租户插件执行、任意网络出口；凭据按租户/工作区密钥引用按需注入，绝不让模型看到。
8. 验收必须包括并发创建/更新/删除、100+ 活跃 turn 的阶梯压测、混合长任务、租户越权、故障注入、模型限流、worker crash/restart、SSE 跨副本重连；报告 P50/P95/P99、错误率、吞吐、排队时长、RSS、FD、租约遗留与成本。

建议的部署演进：本地单实例 → Postgres + queue 单 worker → 独立 worker pool + 统一准入 → 多 API 副本、Redis lease/stream、隔离沙箱和 OIDC。**不要直接提高 uvicorn workers 数量**，目前那会复制进程内状态，产生不一致。

## 2. 记忆层不是聊天历史的别名

维度（与供应商无关）：

- **Working / session state**：当前上下文、工具结果、待批准权限、历史分支；保留在 Session/Checkpoint，不由 Memory 向量库替代。
- **Episodic**：带任务/时间/来源的成功与失败经历、决策理由和回合摘要。
- **Semantic facts**：用户偏好、项目约束、稳定事实；自动抽取必须经过脱敏、去重、冲突解决。
- **Procedural / skills**：可复用 workflow、工具说明及具体执行经验；审批后才能变为可执行 Skill。
- **Knowledge resources**：代码仓库、文档索引、知识图谱；由 project/knowledge 服务维护，按需引用。

各层独立生命周期、检索预算、失效与审核；不要把长期记忆直接混到 `history_manager` 的压缩逻辑中。

## 3. 开源项目对标与取舍

| 参考 | 值得借鉴 | 不能直接照搬 |
| --- | --- | --- |
| [Mem0](https://github.com/mem0ai/mem0) | 按 user / agent / run 切分、事实抽取、去重、混合检索、SDK 与 hosted/OSS | 上述 scope 是语义归属而非 Ridge 的授权；不同版本过滤语义与异步写入不同，需适配器 |
| [OpenCode](https://github.com/anomalyco/opencode) | 项目 `AGENTS.md` / Skills 约定、会话上下文按需装载 | 文件规则不等同自动化长时记忆；不要假设已有原生完整长期记忆 |
| [claude-code-best/claude-code](https://github.com/claude-code-best/claude-code) 与 Claude Code 记忆习惯 | `MEMORY.md` / agent / project 作用域、可读写透明文件、渐进披露 | Markdown 是好的人类可读层，但不能作为多用户 ACL/数据库边界 |
| [DeerFlow](https://github.com/bytedance/deer-flow) | `MemoryManager` 与 backend registry、middleware/tool 模式、DeerMem、Mem0、OpenViking 接入、失败策略 | 其 OpenViking 官方适配文档明确是单用户 USER key 模式；不能据此推断租户隔离已具备 |
| [TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | L0 对话 → L1 原子 → L2 场景 → L3 Persona、资产化、Memory Hub、private/team/restricted ACL、Skills/Knowledge | Hub / Proxy / Skill 审核属于新子系统；不宜嵌进 Agent 核心循环 |
| [OpenViking](https://github.com/volcengine/OpenViking) | `viking://` 虚拟目录；Abstract L0 → Overview L1 → Details L2 渐进读取；session commit 和资源/记忆/Skill 统一目录 | 若用于商业托管，需要专门核对 AGPL-3.0 许可证及 SDK/server 边界，和远端 USER API key/身份映射 |

说明：TencentDB 的 L0–L3 是**记忆形成层级**，OpenViking 的 L0–L2 是**内容详细程度**；名称相同但含义不同。

## 4. Ridge 的 Provider 策略

选择 **一个主记忆后端 + 可选只读知识源**，不要默认多后端同时写入。否则重复记忆、撤回和过期数据难以追踪。

Provider ID：

| ID | 计划能力 | 目前 |
| --- | --- | --- |
| `off` | 完全关闭长期记忆（默认） | 已有空实现 |
| `sqlite` | 私有 scope、CRUD 的第一阶段、无外部服务 | **独立后端原型**；尚未接入回合 |
| `mem0` | Self-hosted / Platform，提取、语义搜索 | 适配器待完成 |
| `openviking` | 官方 SDK + USER key，会话 commit 与分层读取 | 适配器待完成 |
| `tencentdb` | TencentDB MemoryCore/Hub HTTP/SDK，后续支持团队资产 ACL | 适配器待完成 |

首选策略：**个人本地默认 off（用户同意后 sqlite）；多人部署默认 Postgres 元数据 + 独立授权 Memory service，可选 Mem0；大型知识库/资源优先试 OpenViking；团队资产协作考虑 TencentDB**。供应商不决定安全边界；Ridge 决定授权和作用域。

建议 API 契约：

```python
MemoryScope(tenant_id, user_id, project_id, agent_id)  # 必须从鉴权上下文派生
backend.add(scope, content, metadata={...})
backend.search(scope, query, limit=8)
backend.delete(scope, record_id)
```

未来扩展事件：`memory.capture`（异步队列/敏感信息过滤）→ `memory.extract`（事实生成）→ `memory.review` → `memory.commit` → `memory.retrieve`（只返回授权命中）→ `memory.inject`（预算/来源/不可信标记）→ `memory.expire`。写入必须携带幂等键，检索结果保留来源、时间与过期版本，不能覆盖 system 指令。跨租户共享必须另做 Asset ACL 审批，**不得**通过改变 `user_id` 来绕过私有作用域。

### 这次代码完成与未完成的边界

新建 `core/memory/`：
- 显式验证 `MemoryScope` 四维身份，不存在 `default tenant`；
- 进程内 `MemoryProviderRegistry`，未知/未配置 provider **拒绝选择**、默认 off；
- `SQLiteMemoryBackend` 通过 SQLite WAL 保存私有作用域事实，scope 参与每个搜索/删除 SQL 条件，使用查询绑定，不依赖模型提供过滤条件；
- `tests/test_memory_provider_selection.py`：身份校验、跨租户/跨用户/项目/agent 拒绝、并发写、SQL 通配查询、后端故障默认关闭。

**未接线**：Web/API 选择页、持久后端配置、Agent prompt 注入/异步回合写入、Mem0/OpenViking/Tencent 真正 SDK 适配、数据迁移、租户鉴权、分布式配额和多节点数据库。当前代码只是**安全的可验证起点**，不是宣称已交付多后端长期记忆。

## 5. 可执行的分阶段实施顺序

**P0：防止现有数据损坏**：修复 `SessionManager` 索引读改写和快照并发问题；增单进程全局/每会话 admission；确保旧目录兼容备份；测试同一会话写冲突、线程/FD/RSS 背压。

**P1：身份与存储（先于跨租户开放）**：OIDC Principal、tenant/project membership、资源 ACL；PostgreSQL 持久事务和迁移；身份到沙箱/文件/工具/MCP/trace/SSE 的端到端强制传播；拒绝任何可伪造 tenant header。

**P2：Memory 正式功能**：服务器可信身份决定 `MemoryScope`，存储 Provider 选择到用户/工作区配置；Web 可查看当前 provider、读取/删除/导出其拥有的记忆；接入 Agent recall（prompt token/字符预算、注入内容标记）与任务后异步 capture；优先完成 Mem0 适配和本地分级检索，再接 OpenViking SDK，最后 TencentDB Memory Hub；失效、冲突、软删除以及 provider migration。

**P3：生产吞吐**：durable turns、Postgres outbox/队列、Redis/NATS/Temporal 等选一种实现可恢复 worker；公平调度、租户配额、跨副本 lease；隔离 Docker/VM 池与预热；跨进程 SSE 恢复，压测与安全渗透。

## 6. 最低验收门槛

- 自动化覆盖同一 user ID 但不同 tenant，所有 CRUD/项目/SSE/沙箱不得互访；
- 32–128 并发任务阶梯压测，拒绝策略可观测，队列长度与服务恢复有界；**不以一个未实测的 QPS 数字承诺生产能力**；
- 混合故障：鉴权服务失联/Redis 失联/Provider 超时、写部分成功、重试、进程和沙箱崩溃；必须无跨租户泄漏、无 silent fallback；
- Memory 质量评估：精确命中率、过时记忆比例、来源可解释性、prompt token 开销、撤回/删除一致性，敏感信息不落日志；
- 实验环境逐阶段验收通过后再发布功能开关。实际服务端口、模型凭证、后端配额不进入测试日志。

此 ADR 不把单机 WAL 或作用域字段误称为多租户安全。多租户真正成立必须靠**每条请求的已认证主体 + 数据库/对象存储/沙箱/后台任务的全链路授权**。
