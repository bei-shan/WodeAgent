# Git Worktree 会话隔离功能设计文档

> 状态：草稿 → 待审阅
> 日期：2026-06-18
> 参考：Claude Code CLI `EnterWorktree`/`ExitWorktree` 模型 + `learn-claude-code/s12`
> 目标：实现 Claude Code 风格的全会话 worktree 切换，EnterWorktree→ExitWorktree 构成完整隔离工作周期

---

## 零、审阅结论

| 决策点 | 结论 |
|--------|------|
| 隔离粒度 | **会话级**。EnterWorktree 后整个 Agent 的 project_root 切换到 worktree 目录 |
| 进入方式 | `EnterWorktree(name)` 创建新 worktree / `EnterWorktree(path)` 进入已有 |
| 退出方式 | `ExitWorktree(action="keep"|"remove", discard_changes=False)` |
| 目录结构 | `.worktrees/{name}/` 下（默认），可通过 env 配置 |
| baseRef 策略 | `worktree.baseRef=fresh`（基于 origin/main）/ `head`（基于当前 HEAD） |
| 子代理继承 | 子代理自动使用当前 worktree 的 project_root |
| 自动清理 | 空 worktree（无 diff）退出时自动 remove，无需 user 显式选择 |

---

## 一、背景与动机

### 1.1 当前问题

MyCodeAgent 所有的文件修改都直接发生在项目工作区。即使用户想尝试一个实验性的重构，Agent 的改动也会直接污染工作区——没有"试运行后回滚"的能力。

### 1.2 Claude Code 的做法

```
用户: "帮我重构 auth 模块"
  ↓
Agent 调用 EnterWorktree(name="auth-refactor")
  → git worktree add -b wt/auth-refactor .worktrees/auth-refactor HEAD
  → Agent 的 project_root 切换到 .worktrees/auth-refactor/
  → 所有后续文件操作都在 worktree 中
  ↓
Agent 在 worktree 中完成重构
  ↓
Agent 调用 ExitWorktree(action="keep")
  → project_root 恢复为项目根目录
  → worktree 保留，用户可审查: git diff wt/auth-refactor
  → 满意则 git merge wt/auth-refactor
```

**核心理念**：Enter → 隔离工作 → Exit。整个过程就像在一个独立分支上工作，不满意可以完全不 merge。

### 1.3 目标行为

```
用户启动 Agent（project_root = ~/projects/myapp/）

用户说: "试试把登录改成 OAuth，但先不要提交"
  ↓
Agent: EnterWorktree(name="try-oauth")
  → 创建 git worktree .worktrees/try-oauth/
  → project_root 切换为 .worktrees/try-oauth/
  → 此后 Read/Write/Edit/Bash 都在 worktree 内操作
  ↓
Agent 在 worktree 中修改 auth.py, 添加 oauth.py...
  ↓
Agent: ExitWorktree(action="keep")
  → project_root 恢复为 ~/projects/myapp/
  → worktree 保留，分支 wt/try-oauth 保留
  ↓
用户: "看看改了什么"  →  Agent: Bash("git diff wt/try-oauth")
用户: "不错，合并"    →  Agent: Bash("git merge wt/try-oauth")
```

---

## 二、架构设计

### 2.1 模块结构

```
core/worktree/
├── __init__.py          # 导出 WorktreeManager, WorktreeError
├── manager.py           # WorktreeManager — git worktree 生命周期管理
└── store.py             # WorktreeStore — .worktrees/index.json 持久化

tools/builtin/
├── enter_worktree.py    # EnterWorktreeTool — 创建/进入 worktree
├── exit_worktree.py     # ExitWorktreeTool  — 退出/清理 worktree

prompts/tools_prompts/
├── enter_worktree_prompt.py
└── exit_worktree_prompt.py

agents/codeAgent.py      # 集成点：project_root 动态切换
```

### 2.2 核心集成点：project_root 动态切换

```python
class CodeAgent:
    def __init__(self, ...):
        # 原始项目根目录，永不改变
        self._original_project_root = Path(config.project_root).resolve()
        # 当前活动的 project_root（可能在 worktree 中）
        self.project_root = self._original_project_root

        # Worktree 管理器
        self._worktree_manager = WorktreeManager(self._original_project_root)
        # 当前活动的 worktree（None = 在主项目中）
        self._active_worktree: Optional[dict] = None

    def enter_worktree(self, name: str | None = None, path: str | None = None):
        """切换会话到 worktree 目录。

        如果 name 传入 → 创建新 worktree
        如果 path 传入 → 进入已有 worktree
        """
        if path is not None:
            wt = self._worktree_manager.get_by_path(path)
        elif name is not None:
            wt = self._worktree_manager.create(name=name)
        else:
            raise WorktreeError("INVALID_PARAM", "name or path required")

        # 切换 project_root——所有工具自动跟随
        self.project_root = Path(wt["path"]).resolve()
        self._active_worktree = wt

        # 更新 PermissionGate 的 project_root
        self._inject_permission_gate()

        # 通知日志
        self.logger.info("Entered worktree: %s (%s)", wt["name"], wt["path"])

    def exit_worktree(self, action: str = "keep", discard_changes: bool = False):
        """退出 worktree，恢复原始 project_root。"""
        if self._active_worktree is None:
            raise WorktreeError("CONFLICT", "not in a worktree")

        wt_name = self._active_worktree["name"]

        try:
            if action == "remove":
                self._worktree_manager.remove(wt_name, discard_changes=discard_changes)
            else:
                self._worktree_manager.keep(wt_name)
        finally:
            # 无论清理是否成功，都恢复 project_root
            self.project_root = self._original_project_root
            self._active_worktree = None
            self._inject_permission_gate()

        self.logger.info("Exited worktree: %s (action=%s)", wt_name, action)
```

### 2.3 为什么子代理自动继承

由于所有工具（Read、Write、Edit、Bash、Task 等）都通过 `self._project_root` 解析路径，一旦 `enter_worktree` 切换了 `project_root`：

- 主 Agent 的文件操作 → 自动在 worktree 中
- Task oneshot 子代理 → 继承当前的 `project_root` → 也在 worktree 中
- AgentTeams worker → 同样继承 → 也在 worktree 中

**零额外集成成本。**

---

## 三、WorktreeManager 设计

```python
class WorktreeError(Exception):
    """Worktree 操作错误，code 可映射到 Tool ErrorCode。"""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code      # INVALID_PARAM | NOT_FOUND | CONFLICT | INTERNAL_ERROR
        self.message = message


class WorktreeManager:
    """管理 git worktree 的创建、进入、退出、清理。

    所有 git 操作通过 subprocess 执行。目录结构：

        .worktrees/
        ├── index.json           # 所有 worktree 的注册表
        ├── try-oauth/           # git worktree 目录
        ├── auth-refactor/       # git worktree 目录
        └── ...

    命名规范：
        branch 名称: wt/{name}
        worktree 路径: .worktrees/{name}/
    """

    def __init__(
        self,
        project_root: Path,
        store_dir: str | None = None,
        base_ref: str = "head",  # "fresh" | "head"
    ):
        self._root = Path(project_root).resolve()
        dir_name = store_dir or os.getenv("WORKTREE_STORE_DIR", ".worktrees")
        self._worktrees_dir = self._root / dir_name
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._store = WorktreeStore(self._worktrees_dir)
        self._base_ref_mode = base_ref.lower()

    # ---- Public API ----

    def create(self, name: str) -> dict:
        """创建新 git worktree。

        1. 验证 name
        2. 查找是否已存在同名 worktree
        3. 计算 base_ref
        4. git worktree add -b wt/{name} .worktrees/{name} {base_ref}
        5. 写入 index.json
        6. 返回 entry dict
        """
        name = self._validate_name(name)

        # 检查重复
        existing = self._store.find(name)
        if existing and existing.get("status") in ("active", "kept"):
            raise WorktreeError(
                "CONFLICT",
                f"worktree '{name}' already exists. Use EnterWorktree(path=...) to enter it.",
            )

        # 计算 base_ref
        base_ref = self._resolve_base_ref()

        # 执行 git worktree add
        branch = f"wt/{name}"
        wt_path = self._worktrees_dir / name
        self._run_git([
            "worktree", "add", "-b", branch,
            str(wt_path), base_ref,
        ])

        entry = {
            "name": name,
            "path": str(wt_path.resolve()),
            "branch": branch,
            "base_ref": base_ref,
            "status": "active",
            "created_at": time.time(),
            "removed_at": None,
            "kept_at": None,
        }
        self._store.save(entry)
        return dict(entry)

    def get_by_path(self, path: str) -> dict:
        """通过路径查找已有 worktree（用于 EnterWorktree(path=...)）。
        验证 path 确实在 git worktree list 中。
        """
        resolved = str(Path(path).resolve())
        for entry in self._store.load_all():
            if entry.get("path") == resolved:
                return dict(entry)
        raise WorktreeError("NOT_FOUND", f"no worktree registered at: {path}")

    def remove(self, name: str, *, discard_changes: bool = False) -> dict:
        """删除 worktree 及其分支。

        如果 discard_changes=False 且有未提交变更 → 拒绝删除 (CONFLICT)
        如果 discard_changes=True → git worktree remove --force
        """
        name = self._validate_name(name)
        entry = self._store.find(name)
        if entry is None:
            raise WorktreeError("NOT_FOUND", f"worktree not found: {name}")

        wt_path = Path(entry["path"])
        if not discard_changes and not self.is_clean(name):
            raise WorktreeError(
                "CONFLICT",
                f"worktree '{name}' has uncommitted changes. "
                "Use discard_changes=true to force remove.",
            )

        # git worktree remove
        args = ["worktree", "remove"]
        if discard_changes:
            args.append("--force")
        args.append(str(wt_path))
        self._run_git(args)

        # 删除分支（worktree remove 不会自动删分支）
        try:
            self._run_git(["branch", "-D", entry["branch"]])
        except WorktreeError:
            pass  # 分支可能已被删除

        entry["status"] = "removed"
        entry["removed_at"] = time.time()
        self._store.save(entry)
        return dict(entry)

    def keep(self, name: str) -> dict:
        """标记 worktree 为保留（不删除，不做任何 git 操作）。"""
        name = self._validate_name(name)
        entry = self._store.find(name)
        if entry is None:
            raise WorktreeError("NOT_FOUND", f"worktree not found: {name}")

        entry["status"] = "kept"
        entry["kept_at"] = time.time()
        self._store.save(entry)
        return dict(entry)

    def is_clean(self, name: str) -> bool:
        """检查 worktree 是否有未提交变更。"""
        entry = self._store.find(name)
        if entry is None:
            raise WorktreeError("NOT_FOUND", f"worktree not found: {name}")
        try:
            result = self._run_git(
                ["status", "--porcelain"],
                cwd=Path(entry["path"]),
            )
            return result.strip() == ""
        except WorktreeError:
            return False

    def has_changes(self, name: str) -> bool:
        """是否有任何变更（包括 untracked）。等同于 not is_clean。"""
        return not self.is_clean(name)

    def list_all(self) -> list[dict]:
        """列出所有 worktree（按创建时间排序）。"""
        entries = self._store.load_all()
        entries.sort(key=lambda e: e.get("created_at", 0))
        return entries

    def list_git_worktrees(self) -> list[str]:
        """运行 git worktree list，返回原始输出行列表。"""
        try:
            output = self._run_git(["worktree", "list"])
            return output.strip().splitlines()
        except WorktreeError:
            return []

    # ---- Internal ----

    def _resolve_base_ref(self) -> str:
        """根据 baseRef 配置计算基准引用。"""
        if self._base_ref_mode == "fresh":
            # 检测默认分支名
            try:
                default_branch = self._run_git([
                    "rev-parse", "--abbrev-ref", "origin/HEAD"
                ]).strip()
                # origin/HEAD → refs/remotes/origin/main
                return default_branch
            except WorktreeError:
                pass
        return "HEAD"

    def _validate_name(self, name: str) -> str:
        """校验 worktree 名称。

        规则：字母、数字、点、下划线、短横线。每段 ≤ 64 字符。
        拒绝：.. / \ 空格
        """
        if not name or not name.strip():
            raise WorktreeError("INVALID_PARAM", "name is required")
        name = name.strip()
        if len(name) > 64:
            raise WorktreeError("INVALID_PARAM", "name must be <= 64 characters")
        # 拒绝路径穿越
        for char in ("/", "\\", "..", " ", ":", "*", "?", '"', "<", ">", "|"):
            if char in name:
                raise WorktreeError(
                    "INVALID_PARAM",
                    f"name contains invalid character: '{char}'",
                )
        import re
        if not re.match(r"^[A-Za-z0-9._-]+$", name):
            raise WorktreeError(
                "INVALID_PARAM",
                "name may only contain letters, digits, dots, underscores, and dashes",
            )
        return name

    def _run_git(
        self,
        args: list[str],
        cwd: Path | None = None,
        timeout_s: float = 30.0,
    ) -> str:
        """执行 git 命令，返回 stdout，失败抛出 WorktreeError。"""
        import subprocess

        cmd = ["git"] + args
        work_dir = cwd or self._root
        try:
            result = subprocess.run(
                cmd,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                raise WorktreeError(
                    "INTERNAL_ERROR",
                    f"git {' '.join(args)} failed: {stderr}",
                )
            return result.stdout
        except subprocess.TimeoutExpired:
            raise WorktreeError("TIMEOUT", f"git {' '.join(args)} timed out")
        except FileNotFoundError:
            raise WorktreeError("INTERNAL_ERROR", "git is not available on this system")
```

---

## 四、WorktreeStore 设计

```python
class WorktreeStore:
    """持久化 worktree 索引到 .worktrees/index.json。"""

    def __init__(self, worktrees_dir: Path):
        self._dir = Path(worktrees_dir)
        self._path = self._dir / "index.json"

    def load_all(self) -> list[dict]:
        """加载所有 worktree 条目。"""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return list(data.get("worktrees", []))
        except (json.JSONDecodeError, OSError):
            return []

    def find(self, name: str) -> dict | None:
        """按名称查找 worktree。"""
        for entry in self.load_all():
            if entry.get("name") == name:
                return dict(entry)
        return None

    def save(self, entry: dict) -> dict:
        """保存或更新 worktree 条目。

        按 name 匹配：找到 → 更新；未找到 → 追加。
        """
        entries = self.load_all()
        found = False
        for i, existing in enumerate(entries):
            if existing.get("name") == entry.get("name"):
                entries[i] = dict(entry)
                found = True
                break
        if not found:
            entries.append(dict(entry))

        self._dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"worktrees": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return dict(entry)
```

---

## 五、工具设计

### 5.1 EnterWorktreeTool

```
名称: EnterWorktree
描述: 创建或进入一个 git worktree，将 Agent 的工作目录切换到其中。
      此后所有文件操作（Read/Write/Edit/Bash）都在 worktree 内生效。

参数:
  - name: (可选) 新 worktree 的名称。自动创建对应的 git worktree 和 wt/{name} 分支
  - path: (可选) 已有 worktree 的路径，直接进入

  注意: name 和 path 二选一

行为:
  - name 传入 → WorktreeManager.create(name)
  - path 传入 → WorktreeManager.get_by_path(path)
  - 进入后: CodeAgent.project_root 切换为 worktree 路径
  - 所有工具（Read/Write/Edit/Bash/Task）自动在 worktree 中操作
  - 子代理（Task/AgentTeams）继承当前 worktree

成功响应:
  data: { name, path, branch, base_ref, created_at }
  text: "Entered worktree 'try-oauth' at .worktrees/try-oauth/"

错误:
  - CONFLICT: 同名 worktree 已存在
  - NOT_FOUND: path 不是注册的 worktree
  - INVALID_PARAM: name 格式不合法
```

### 5.2 ExitWorktreeTool

```
名称: ExitWorktree
描述: 退出当前 worktree，恢复原始项目目录。

参数:
  - action: (必填) "keep" | "remove"
    - "keep": 保留 worktree 和分支（供后续审查/merge）
    - "remove": 删除 worktree 和分支
  - discard_changes: (可选) 仅 action="remove" 时有效。默认 false。
    - false: worktree 有未提交变更时拒绝删除
    - true: 强制删除（丢弃所有变更）

行为:
  - action="keep" → WorktreeManager.keep() + 恢复 project_root
  - action="remove" + discard_changes=false + 有变更 → 拒绝
  - action="remove" + discard_changes=true → 强制删除
  - 空 worktree（无任何变更）→ 自动 remove，无需显式指定

成功响应:
  data: { previous_worktree: name, action, previous_project_root }
  text: "Exited worktree 'try-oauth' (kept). Merge with: git merge wt/try-oauth"

错误:
  - CONFLICT: 不在 worktree 中
  - CONFLICT: remove 时有未提交变更且 discard_changes=false
  - NOT_FOUND: worktree 不存在
```

---

## 六、CodeAgent 集成

### 6.1 初始化

```python
# codeAgent.py __init__
def __init__(self, ...):
    # ... 现有初始化 ...
    self.project_root = Path(config.project_root).resolve()
    self._original_project_root = self.project_root

    # Worktree 管理器（始终初始化）
    base_ref = os.getenv("WORKTREE_BASE_REF", "fresh")
    store_dir = os.getenv("WORKTREE_STORE_DIR", ".worktrees")
    self._worktree_manager = WorktreeManager(
        project_root=self._original_project_root,
        store_dir=store_dir,
        base_ref=base_ref,
    )
    self._active_worktree: Optional[dict] = None

def enter_worktree(self, name=None, path=None):
    # ... 如前所述 ...

def exit_worktree(self, action="keep", discard_changes=False):
    # ... 如前所述 ...
```

### 6.2 工具注册

```python
# _register_builtin_tools 中
self.tool_registry.register_tool(
    EnterWorktreeTool(
        project_root=self.project_root,
        code_agent=self,  # 需要回调 code_agent.enter_worktree
    )
)
self.tool_registry.register_tool(
    ExitWorktreeTool(
        project_root=self.project_root,
        code_agent=self,
    )
)
```

### 6.3 会话持久化

```python
# save_session 中
if self._active_worktree:
    snapshot["active_worktree"] = {
        "name": self._active_worktree["name"],
        "path": self._active_worktree["path"],
    }

# load_session 中
if snapshot.get("active_worktree"):
    wt = snapshot["active_worktree"]
    self.enter_worktree(path=wt["path"])
```

---

## 七、与现有系统关系

### 7.1 Soft Sandbox

进入 worktree 后，`PermissionGate` 的 `project_root` 也随之更新。worktree 内的文件操作受其 project_root 的 sandbox 约束。

子代理通过 `subagent_gate()` 共享授权缓存的行为不变。

### 7.2 AgentTeams

AgentTeams 的 worker 在 `ExecutionService` 中通过 `project_root` 运行。如果主 Agent 在 worktree 中，worker 自然继承 worktree 的工作目录——无需额外集成。

### 7.3 TaskTool

Task oneshot 子代理的 `SubagentRunner` 构造函数接收 `project_root`。当主 Agent 在 worktree 中时，`self._project_root` 已经是 worktree 路径，子代理自动隔离。

---

## 八、工具间的完整工作流示例

```
1. 用户: "试试把认证模块改成 OAuth"

2. Agent ← LLM 决策: 这是一个实验性改动，应该用 worktree
   → EnterWorktree(name="try-oauth")

3. CodeAgent.project_root → .worktrees/try-oauth/

4. Agent 调用 Read("src/auth.py") → 读取 .worktrees/try-oauth/src/auth.py
   Agent 调用 Write("src/oauth.py") → 写入 .worktrees/try-oauth/src/oauth.py
   Agent 调用 Bash("pytest")     → 在 .worktrees/try-oauth/ 中运行

5. Agent 完成修改后:
   → ExitWorktree(action="keep")
   → 回复用户: "已完成 OAuth 改造，修改在 worktree 'try-oauth'。
                 审查: git diff wt/try-oauth
                 合并: git merge wt/try-oauth"

6. 用户满意:
   → Bash("git merge wt/try-oauth")
   → WorktreeManager.remove("try-oauth")  # 合并后清理
```

---

## 九、错误处理矩阵

| 场景 | 错误码 | 消息 |
|------|--------|------|
| 非 git 仓库 | INTERNAL_ERROR | "git is not available / not a git repository" |
| 同名 worktree 已存在 | CONFLICT | "worktree 'X' already exists. Use EnterWorktree(path=...) to enter it." |
| name 含非法字符 | INVALID_PARAM | "name may only contain..." |
| path 不是注册的 worktree | NOT_FOUND | "no worktree registered at: /path" |
| 未在 worktree 中调用 ExitWorktree | CONFLICT | "not in a worktree" |
| remove 时有变更且不强制 | CONFLICT | "worktree has uncommitted changes. Use discard_changes=true." |
| git 命令超时 | TIMEOUT | "git ... timed out" |

---

## 十、测试策略

### 10.1 核心 WorktreeManager（mock git）

```python
def test_create_worktree_mock_git(tmp_path):
    with patch.object(WorktreeManager, '_run_git') as mock_git:
        mock_git.return_value = ""
        mgr = WorktreeManager(tmp_path)
        entry = mgr.create("test-wt")
        assert entry["name"] == "test-wt"
        assert entry["branch"] == "wt/test-wt"
        assert entry["status"] == "active"

def test_create_duplicate_rejected(tmp_path):
    ...

def test_remove_with_changes_rejected(tmp_path):
    ...

def test_remove_discard_changes_allowed(tmp_path):
    ...

def test_is_clean(tmp_path):
    ...
```

### 10.2 工具测试

```python
def test_enter_worktree_tool_protocol():
    ...

def test_exit_worktree_keep_protocol():
    ...

def test_exit_worktree_not_in_worktree_errors():
    ...

def test_name_validation_rejects_path_traversal():
    ...
```

### 10.3 集成测试

```python
def test_enter_worktree_switches_all_tools_project_root():
    # 进入 worktree 后，Write/Read 操作的文件路径全部指向 worktree
    ...

def test_subagent_inherits_worktree():
    # Task oneshot 子代理自动使用 worktree project_root
    ...
```

---

## 十一、实施计划

| Phase | 内容 | 文件 |
|-------|------|------|
| 1 | `WorktreeManager` + `WorktreeStore` | `core/worktree/manager.py`, `store.py`, `__init__.py` |
| 2 | `EnterWorktreeTool` + `ExitWorktreeTool` | `tools/builtin/enter_worktree.py`, `exit_worktree.py` |
| 3 | `CodeAgent` 集成（`enter_worktree`/`exit_worktree` + project_root 切换） | `agents/codeAgent.py` |
| 4 | Prompt 文件 + 注册 | 2 个 prompt 文件，`codeAgent._register_builtin_tools` |
| 5 | 会话持久化 | `codeAgent.save_session` / `load_session` |
| 6 | 测试 | 3 个测试文件 |

---

## 十二、配置项

```bash
# Worktree 功能开关（默认开启）
# WORKTREE_ENABLED=true

# Worktree 存储目录（默认 .worktrees）
# WORKTREE_STORE_DIR=.worktrees

# baseRef 策略: fresh（基于 origin/HEAD）| head（基于当前 HEAD）
# WORKTREE_BASE_REF=fresh
```
