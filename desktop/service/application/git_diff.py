"""Bounded, read-only Git diff for a session-local repository.

Never infer an ancestor project repository: only a real .git directory inside
the active session workspace is eligible. No shell, hooks, external diff or
textconv. Untracked files cannot have a HEAD diff and are reported explicitly.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess

from .errors import InvalidInputError, ForbiddenError
from .workspace import WorkspaceService

MAX_DIFF_BYTES = 128 * 1024
MAX_WORK_FILE_BYTES = 1024 * 1024


class GitDiffService:
    def __init__(self, workspace: WorkspaceService):
        self.workspace = workspace

    def diff(self, sid: str, path: str) -> dict:
        parts = self.workspace._parts(path)
        if not parts or len(parts) > 32 or len(path.encode("utf-8")) > 1024:
            raise InvalidInputError("Select a single workspace file for Git diff")
        if parts[0] == ".git":
            raise ForbiddenError("Git metadata is not a browsable file")
        # Reuse no-follow descriptor traversal for the user-supplied path.
        with self.workspace._open_file(sid, path) as (_, handle):
            if os.fstat(handle.fileno()).st_size > MAX_WORK_FILE_BYTES:
                raise InvalidInputError("File is too large for a Git diff")
        with self.workspace._root_fd(sid) as fd:
            repo_root = os.path.realpath(f"/proc/self/fd/{fd}") if os.path.isdir("/proc/self/fd") else (
                str(self.workspace.workspace_base / sid)
            )
            try:
                # A worktree .git pointer can reference an external repository.
                # Do not follow it, and do not fall back to ancestor discovery.
                meta = os.stat(".git", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return {"available": False, "reason": "当前会话工作区不是 Git 仓库", "patch": ""}
            if not stat.S_ISDIR(meta.st_mode):
                return {"available": False, "reason": "仅支持会话目录内部的 Git 仓库", "patch": ""}
            if not shutil.which("git"):
                return {"available": False, "reason": "服务端未安装 Git", "patch": ""}

            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": "/nonexistent",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
            }
            # :(literal) prevents a user filename from being interpreted as
            # a Git pathspec glob or magic expression.
            args = [
                "git", "--no-pager", "-c", "core.hooksPath=/dev/null",
                "-c", "diff.external=", "-c", "core.quotePath=false",
                "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                "--no-renames", "--unified=3", "HEAD", "--",
                ":(literal)" + "/".join(parts),
            ]
            try:
                result = subprocess.run(
                    args, cwd=repo_root, env=env, capture_output=True,
                    timeout=5, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return {"available": False, "reason": "Git Diff 无法安全完成", "patch": ""}
        if result.returncode != 0:
            return {"available": False, "reason": "无法读取 HEAD 对应的 Git 版本", "patch": ""}
        if len(result.stdout) > MAX_DIFF_BYTES:
            return {"available": False, "reason": "Git Diff 超过 128 KiB 展示上限", "patch": ""}
        patch = result.stdout.decode("utf-8", errors="replace")
        return {
            "available": True, "path": "/".join(parts), "patch": patch,
            "reason": "工作文件相对 HEAD 没有已跟踪差异" if not patch else "",
            "base": "HEAD", "tracked": bool(patch),
        }
