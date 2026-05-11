"""代码内容搜索工具 (GrepTool)

遵循《通用工具响应协议》，返回标准化结构。
"""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, TypedDict

from prompts.tools_prompts.grep_prompt import grep_prompt
from ..base import Tool, ToolParameter, ToolStatus, ErrorCode


class MatchItem(TypedDict):
    """单条匹配结果结构"""
    file: str  # 相对于项目根目录的文件路径
    line: int  # 行号（从1开始）
    text: str  # 完整的行文本


class GrepTool(Tool):
    """搜索文件内容（优先使用 ripgrep，缺失则回退到 Python 实现）"""

    # 总是忽略的目录/文件列表
    ALWAYS_IGNORE = {
        ".git",          # Git 版本控制目录
        "node_modules",  # Node.js 依赖目录
        "dist",          # 分发目录
        "build",         # 构建输出目录
        "__pycache__",   # Python 字节码缓存
        ".venv",         # Python 虚拟环境
        "venv",          # Python 虚拟环境
        ".idea",         # JetBrains IDE 配置目录
        ".vscode",       # VS Code 配置目录
        ".DS_Store",     # macOS 系统文件
        ".hg",           # Mercurial 版本控制目录
        ".svn",          # Subversion 版本控制目录
        ".mypy_cache",   # mypy 类型检查缓存
        ".pytest_cache", # pytest 测试缓存
        ".ruff_cache",   # ruff linter 缓存
        ".tox",          # tox 测试环境目录
        ".cache",        # 通用缓存目录
        "site-packages", # Python 包目录
    }

    # 最大返回结果数
    MAX_RESULTS = 100

    # 搜索超时时间（秒）
    TIMEOUT_SEC = 2.0

    def __init__(self, name: str = "Grep", project_root: Optional[Path] = None):
        """
        初始化 Grep 工具

        Args:
            name: 工具名称，默认为 "Grep"
            project_root: 项目根目录，用于沙箱限制
        """
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        
        # 调用基类初始化（注入 project_root）
        super().__init__(
            name=name,
            description=grep_prompt,
            project_root=project_root,
        )
        
        # 保持向后兼容的内部变量
        self._root = self._project_root

    def run(self, parameters: Dict[str, Any]) -> str:
        """
        执行代码搜索操作

        Args:
            parameters: 包含以下键的字典：
                - pattern: 正则表达式模式（必需）
                - path: 搜索起始目录（默认为 '.'）
                - include: 文件过滤的 glob 模式（可选）
                - case_sensitive: 是否区分大小写（默认为 False）

        Returns:
            JSON 格式的响应字符串（遵循《通用工具响应协议》）
        """
        start_time = time.monotonic()
        
        # 保存原始参数用于 context.params_input
        params_input = dict(parameters)
        
        pattern = parameters.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Missing required parameter 'pattern'.",
                params_input=params_input,
            )

        path = parameters.get("path", ".")
        include = parameters.get("include")
        case_sensitive = parameters.get("case_sensitive", False)

        if include is not None and not isinstance(include, str):
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="include must be a string if provided.",
                params_input=params_input,
            )
        if not isinstance(case_sensitive, bool):
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="case_sensitive must be a boolean.",
                params_input=params_input,
            )

        # 路径解析与沙箱校验
        try:
            abs_root = self._resolve_search_root(path)
        except ValueError:
            return self.create_error_response(
                error_code=ErrorCode.ACCESS_DENIED,
                message="Access denied. Path must be within project root.",
                params_input=params_input,
            )
        except OSError as e:
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"Search failed ({e}).",
                params_input=params_input,
            )

        # 计算解析后的相对路径
        rel_root = str(abs_root.relative_to(self._root)) or "."

        if not abs_root.exists():
            return self.create_error_response(
                error_code=ErrorCode.NOT_FOUND,
                message=f"Search root '{path}' does not exist.",
                params_input=params_input,
                path_resolved=rel_root,
            )
        if not abs_root.is_dir():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=f"Search root '{path}' is not a directory.",
                params_input=params_input,
                path_resolved=rel_root,
            )

        matches: List[MatchItem] = []
        aborted_reason: Optional[str] = None
        fallback_used = False

        # 优先使用 ripgrep 进行搜索
        rg_available = shutil.which("rg") is not None
        if rg_available:
            try:
                matches = self._run_rg(
                    abs_root=abs_root,
                    pattern=pattern,
                    include=include,
                    case_sensitive=case_sensitive,
                )
            except subprocess.TimeoutExpired as e:
                aborted_reason = "timeout"
                output = getattr(e, "output", "") or ""
                matches = self._parse_rg_json_output(output)
            except ValueError as e:
                # 正则表达式错误
                time_ms = int((time.monotonic() - start_time) * 1000)
                return self.create_error_response(
                    error_code=ErrorCode.INVALID_PARAM,
                    message=f"Invalid regex pattern: {e}",
                    params_input=params_input,
                    path_resolved=rel_root,
                    time_ms=time_ms,
                )
            except Exception:
                # ripgrep 失败，回退到 Python
                fallback_used = True
                aborted_reason = "rg_failed"
        else:
            # ripgrep 不可用
            fallback_used = True
            aborted_reason = "rg_not_found"

        # ripgrep 不可用或失败时，使用 Python 实现
        if fallback_used:
            try:
                matches, py_aborted = self._run_python_search(
                    abs_root=abs_root,
                    pattern=pattern,
                    include=include,
                    case_sensitive=case_sensitive,
                    start_time=start_time,
                )
                if py_aborted:
                    aborted_reason = py_aborted
            except re.error as e:
                time_ms = int((time.monotonic() - start_time) * 1000)
                return self.create_error_response(
                    error_code=ErrorCode.INVALID_PARAM,
                    message=f"Invalid regex pattern: {e}",
                    params_input=params_input,
                    path_resolved=rel_root,
                    time_ms=time_ms,
                )
            except Exception as e:
                time_ms = int((time.monotonic() - start_time) * 1000)
                return self.create_error_response(
                    error_code=ErrorCode.INTERNAL_ERROR,
                    message=f"Search failed ({e}).",
                    params_input=params_input,
                    path_resolved=rel_root,
                    time_ms=time_ms,
                )

        # 按文件修改时间降序排序
        self._sort_matches_by_mtime(matches)

        # 截断结果
        truncated = False
        if len(matches) > self.MAX_RESULTS:
            matches = matches[: self.MAX_RESULTS]
            truncated = True

        # 计算搜索耗时
        time_ms = int((time.monotonic() - start_time) * 1000)

        # 构建响应
        return self._format_response(
            matches=matches,
            pattern=pattern,
            rel_root=rel_root,
            truncated=truncated,
            aborted_reason=aborted_reason,
            fallback_used=fallback_used,
            time_ms=time_ms,
            params_input=params_input,
        )

    def _resolve_search_root(self, path: str) -> Path:
        """解析搜索根目录路径"""
        input_path = Path(path)
        if input_path.is_absolute():
            root = input_path.resolve()
        else:
            root = (self._root / input_path).resolve()
        root.relative_to(self._root)  # 沙箱检查
        return root

    def _run_rg(
        self,
        abs_root: Path,
        pattern: str,
        include: Optional[str],
        case_sensitive: bool,
    ) -> List[MatchItem]:
        """使用 ripgrep 执行搜索"""
        rel_root = str(abs_root.relative_to(self._root)) or "."
        search_path = rel_root

        cmd = [
            "rg",
            "--json",
            "--no-heading",
            "--line-number",
            "--with-filename",
            "--color", "never",
        ]
        if not case_sensitive:
            cmd.append("-i")
        
        include_normalized = include.replace("\\", "/").strip() if include else None
        if include_normalized:
            cmd.extend(["--glob", include_normalized])

        # 基于 ALWAYS_IGNORE 做目录剪枝
        root_parts = set(abs_root.relative_to(self._root).parts)
        for entry in sorted(self.ALWAYS_IGNORE):
            if entry in root_parts:
                continue
            if entry.startswith("."):
                cmd.extend(["--glob", f"!**/{entry}/**"])
                cmd.extend(["--glob", f"!**/{entry}"])
            else:
                cmd.extend(["--glob", f"!**/{entry}/**"])

        cmd.extend(["--", pattern, search_path])

        result = subprocess.run(
            cmd,
            cwd=str(self._root),
            capture_output=True,
            text=True,
            timeout=self.TIMEOUT_SEC,
        )

        if result.returncode == 2:
            err = result.stderr.strip() or "ripgrep failed"
            raise ValueError(err)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.strip() or "ripgrep error")

        return self._parse_rg_json_output(result.stdout)

    def _parse_rg_json_output(self, output: str) -> List[MatchItem]:
        """解析 ripgrep 的 JSON 输出"""
        import json
        matches: List[MatchItem] = []
        if not output:
            return matches
        for line in output.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "match":
                continue
            data = obj.get("data") or {}
            path_text = (data.get("path") or {}).get("text")
            line_num = data.get("line_number")
            line_text = (data.get("lines") or {}).get("text")
            if not path_text or not line_num or line_text is None:
                continue
            file_path = Path(path_text)
            if file_path.is_absolute():
                try:
                    rel_file = file_path.resolve().relative_to(self._root).as_posix()
                except Exception:
                    rel_file = file_path.as_posix()
            else:
                rel_file = file_path.as_posix()
            matches.append({
                "file": rel_file,
                "line": int(line_num),
                "text": line_text.rstrip("\n"),
            })
        return matches

    def _run_python_search(
        self,
        abs_root: Path,
        pattern: str,
        include: Optional[str],
        case_sensitive: bool,
        start_time: float,
    ) -> tuple[List[MatchItem], Optional[str]]:
        """使用 Python 实现执行搜索（ripgrep 不可用时的回退方案）"""
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(pattern, flags=flags)
        matches: List[MatchItem] = []
        include_normalized = include.replace("\\", "/").strip() if include else None
        aborted_reason = None

        for current_root, dirs, files in os.walk(abs_root, topdown=True):
            dirs.sort()
            files.sort()

            # 剪枝
            dirs[:] = [d for d in dirs if d not in self.ALWAYS_IGNORE]

            for filename in files:
                if filename in self.ALWAYS_IGNORE:
                    continue

                # 检查超时
                if self._is_timed_out(start_time):
                    return matches, "timeout"

                rel_match_path = Path(current_root).resolve().relative_to(abs_root) / filename
                rel_match_posix = rel_match_path.as_posix()

                # 应用 include 过滤
                if include_normalized and not self._match_include(rel_match_posix, include_normalized):
                    continue

                rel_display_path = Path(current_root).resolve().relative_to(self._root) / filename
                rel_display_posix = rel_display_path.as_posix()

                file_path = Path(current_root) / filename
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
                        for line_no, line in enumerate(handle, start=1):
                            if regex.search(line):
                                matches.append({
                                    "file": rel_display_posix,
                                    "line": line_no,
                                    "text": line.rstrip("\n"),
                                })
                except (OSError, UnicodeError):
                    continue

                if self._is_timed_out(start_time):
                    return matches, "timeout"

        return matches, aborted_reason

    def _match_include(self, rel_posix: str, include_pattern: str) -> bool:
        """检查文件路径是否匹配 include glob 模式"""
        cleaned = self._strip_relative_prefix(include_pattern)
        path_obj = PurePosixPath(rel_posix)
        if path_obj.match(cleaned):
            return True
        if cleaned.startswith("**/"):
            return path_obj.match(cleaned[3:])
        return False

    def _strip_relative_prefix(self, pattern: str) -> str:
        """移除 glob 模式开头的 ./ 或 / 前缀"""
        cleaned = pattern
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        while cleaned.startswith("/"):
            cleaned = cleaned[1:]
        return cleaned

    def _is_timed_out(self, start_time: float) -> bool:
        """检查搜索是否超时"""
        return (time.monotonic() - start_time) > self.TIMEOUT_SEC

    def _sort_matches_by_mtime(self, matches: List[MatchItem]) -> None:
        """按文件修改时间降序排序匹配结果"""
        mtime_cache: Dict[str, float] = {}

        def get_mtime(rel_path: str) -> float:
            if rel_path not in mtime_cache:
                full_path = self._root / rel_path
                try:
                    mtime_cache[rel_path] = os.stat(full_path).st_mtime
                except OSError:
                    mtime_cache[rel_path] = 0
            return mtime_cache[rel_path]

        matches.sort(key=lambda m: (-get_mtime(m["file"]), m["file"], m["line"]))

    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义"""
        return [
            ToolParameter(
                name="pattern",
                type="string",
                description="Regex pattern to search (e.g. 'class\\s+User'). Required.",
                required=True,
            ),
            ToolParameter(
                name="path",
                type="string",
                description="Directory to search in (relative to project root). Defaults to '.'",
                required=False,
                default=".",
            ),
            ToolParameter(
                name="include",
                type="string",
                description="Glob pattern to filter files (e.g. '*.ts'). Highly recommended.",
                required=False,
                default=None,
            ),
            ToolParameter(
                name="case_sensitive",
                type="boolean",
                description="If true, search is case-sensitive. Default is false.",
                required=False,
                default=False,
            ),
        ]

    def _format_response(
        self,
        matches: List[MatchItem],
        pattern: str,
        rel_root: str,
        truncated: bool,
        aborted_reason: Optional[str],
        fallback_used: bool,
        time_ms: int,
        params_input: Dict[str, Any],
    ) -> str:
        """
        构建标准化响应（遵循《通用工具响应协议》）
        
        顶层字段仅包含：status, data, text, stats, context
        
        状态判定逻辑：
        - 使用 fallback → status="partial"（无论是否有结果）
        - 有结果 + 截断/超时 → status="partial"
        - 无结果 + 超时 → status="error" + error.code="TIMEOUT"
        - 其他成功 → status="success"
        """
        has_results = len(matches) > 0
        is_timeout_no_results = aborted_reason == "timeout" and not has_results
        # 修复：有结果 + 超时也应该是 partial（移除 aborted_reason != "timeout" 条件）
        is_partial = fallback_used or (truncated and has_results) or (aborted_reason is not None and has_results)
        
        # 构建 data.matches（对象数组：{file, line, text}）
        data: Dict[str, Any] = {
            "matches": matches,
            "truncated": truncated,
        }
        
        # 如果使用了 Python 回退
        if fallback_used:
            data["fallback_used"] = True
            if aborted_reason in ("rg_not_found", "rg_failed"):
                data["fallback_reason"] = aborted_reason
        
        # 构建 text（人类可读摘要）
        unique_files = len({m["file"] for m in matches})
        lines = []
        
        if has_results:
            lines.append(f"Found {len(matches)} matches in {unique_files} files for '{pattern}' in '{rel_root}'")
        else:
            lines.append(f"No matches found for '{pattern}' in '{rel_root}'")
        
        lines.append(f"(Sorted by mtime desc. Took {time_ms}ms)")
        
        # 添加状态说明
        if truncated:
            lines.append(f"[Truncated: Showing first {self.MAX_RESULTS} matches. Narrow pattern or path.]")
        
        if aborted_reason == "timeout":
            if has_results:
                lines.append("[Partial: Search timed out (>2s). Results are incomplete.]")
            else:
                lines.append("[Error: Search timed out (>2s) without finding results.]")
        
        if fallback_used:
            if aborted_reason == "rg_not_found":
                lines.append("[Info: ripgrep not available; used slower Python fallback search.]")
            elif aborted_reason == "rg_failed":
                lines.append("[Info: ripgrep failed; used Python fallback search.]")
        
        if has_results:
            lines.append("")
            for item in matches:
                lines.append(f"{item['file']}:{item['line']}: {item['text']}")
        
        text = "\n".join(lines)
        
        # 构建 extra_stats
        extra_stats = {
            "matched_files": unique_files,
            "matched_lines": len(matches),
        }
        
        # 构建 extra_context
        extra_context = {
            "pattern": pattern,
            "sorted_by": "mtime_desc",
        }
        
        # 根据状态选择响应类型
        if is_timeout_no_results:
            # 无结果且超时 → error
            return self.create_error_response(
                error_code=ErrorCode.TIMEOUT,
                message=text,
                params_input=params_input,
                time_ms=time_ms,
                path_resolved=rel_root,
                extra_context=extra_context,
            )
        elif is_partial:
            # 有结果但有折扣（截断/回退/超时）→ partial
            return self.create_partial_response(
                data=data,
                text=text,
                params_input=params_input,
                time_ms=time_ms,
                extra_stats=extra_stats,
                path_resolved=rel_root,
                extra_context=extra_context,
            )
        else:
            # 正常完成 → success
            return self.create_success_response(
                data=data,
                text=text,
                params_input=params_input,
                time_ms=time_ms,
                extra_stats=extra_stats,
                path_resolved=rel_root,
                extra_context=extra_context,
            )
