"""智能文件浏览器工具 (list_files / LS)

遵循《通用工具响应协议》，返回标准化结构。
"""

import os
import fnmatch
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from prompts.tools_prompts.list_file_prompt import LS_prompt
from ..base import Tool, ToolParameter, ToolStatus, ErrorCode


class ListFilesTool(Tool):
    """安全的目录浏览工具，支持分页与过滤"""

    # 默认忽略的目录/文件（非隐藏文件类）
    DEFAULT_IGNORE = {
        "node_modules",  # Node.js 依赖目录
        "target",        # Java/Scala 构建输出目录
        "build",         # 通用构建输出目录
        "dist",          # 分发目录
        "venv",          # Python 虚拟环境
        "__pycache__",   # Python 字节码缓存
        ".git",          # Git 版本控制目录
        ".hg",           # Mercurial 版本控制目录
        ".svn",          # Subversion 版本控制目录
        ".idea",         # JetBrains IDE 配置目录
        ".vscode",       # VS Code 配置目录
        ".DS_Store",     # macOS 系统文件
        ".venv",         # Python 虚拟环境（另一种命名）
    }

    def __init__(
        self,
        name: str = "LS",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
    ):
        """
        初始化文件列表工具

        Args:
            name: 工具名称，默认为 "LS"
            project_root: 项目根目录，用于沙箱限制
            working_dir: 工作目录，用于解析相对路径
        """
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        
        # 调用基类初始化（注入 project_root 和 working_dir）
        super().__init__(
            name=name,
            description=LS_prompt,
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        
        # 保持向后兼容的内部变量
        self._root = self._project_root

    def run(self, parameters: Dict[str, Any]) -> str:
        """
        执行文件列表操作

        Args:
            parameters: 包含以下键的字典：
                - path: 要列出的目录路径（默认为 '.'）
                - offset: 分页起始索引（默认为 0）
                - limit: 返回的最大条目数（默认为 100）
                - include_hidden: 是否包含隐藏文件（默认为 False）
                - ignore: 要忽略的 glob 模式列表（默认为空）

        Returns:
            JSON 格式的响应字符串（遵循《通用工具响应协议》）
        """
        start_time = time.monotonic()
        
        # 保存原始参数用于 context.params_input
        params_input = dict(parameters)
        
        path = parameters.get("path", ".")
        offset = parameters.get("offset", 0)
        limit = parameters.get("limit", 100)
        include_hidden = parameters.get("include_hidden", False)
        ignore = parameters.get("ignore") or []  # 避免可变默认值问题

        # 参数校验
        if not isinstance(offset, int) or offset < 0:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="offset must be a non-negative integer.",
                params_input=params_input,
            )
        if not isinstance(limit, int) or limit < 1 or limit > 200:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="limit must be an integer between 1 and 200.",
                params_input=params_input,
            )
        if not isinstance(ignore, list):
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="ignore must be a list of glob patterns.",
                params_input=params_input,
            )

        # 路径解析与沙箱校验
        try:
            input_path = Path(path)
            if input_path.is_absolute():
                target = input_path.resolve()
            else:
                target = (self._working_dir / input_path).resolve()

            # 沙箱安全检查
            target.relative_to(self._root)
        except ValueError:
            return self.create_error_response(
                error_code=ErrorCode.ACCESS_DENIED,
                message="Access denied. Path must be within the project root.",
                params_input=params_input,
            )
        except OSError as e:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=f"Invalid path - {e}",
                params_input=params_input,
            )

        # 计算解析后的相对路径
        rel_path = "."
        try:
            rel_path = str(target.relative_to(self._root)) or "."
        except Exception:
            rel_path = str(target)

        if not target.exists():
            return self.create_error_response(
                error_code=ErrorCode.NOT_FOUND,
                message=f"Path '{path}' does not exist.",
                params_input=params_input,
                path_resolved=rel_path,
            )
        if not target.is_dir():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=f"'{path}' is a file, not a directory. Use 'Read' tool to view its content.",
                params_input=params_input,
                path_resolved=rel_path,
            )

        # 列出目录内容
        try:
            items = self._list_items(target, include_hidden, ignore)
        except PermissionError:
            return self.create_error_response(
                error_code=ErrorCode.ACCESS_DENIED,
                message=f"Permission denied accessing '{path}'.",
                params_input=params_input,
                path_resolved=rel_path,
            )
        except OSError as e:
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"Failed to list directory - {e}",
                params_input=params_input,
                path_resolved=rel_path,
            )

        # 计算分页范围
        total = len(items)
        start = offset if offset < total else total
        end = min(offset + limit, total)
        page_items = items[start:end]

        # 统计各类条目数量
        dirs_count = sum(1 for i in items if i["type"] == "dir")
        files_count = sum(1 for i in items if i["type"] == "file")
        links_count = sum(1 for i in items if i["type"] == "link")

        # 计算耗时
        time_ms = int((time.monotonic() - start_time) * 1000)

        # 构建响应
        return self._format_response(
            rel_path=rel_path,
            total=total,
            dirs_count=dirs_count,
            files_count=files_count,
            links_count=links_count,
            start=start,
            end=end,
            items=page_items,
            params_input=params_input,
            time_ms=time_ms,
        )

    def _list_items(self, target: Path, include_hidden: bool, ignore: List[str]):
        """
        列出目录条目，应用过滤规则

        Args:
            target: 要列出的目标目录路径
            include_hidden: 是否包含隐藏文件
            ignore: 要忽略的 glob 模式列表

        Returns:
            包含文件信息的字典列表，每个字典包含 name, type, path, is_dir 键
        """
        items = []
        with os.scandir(target) as it:
            for entry in it:
                name = entry.name
                # 条目相对于 root / target 的路径（用于 ignore glob 匹配）
                # 注意：不使用 resolve()，保留原始路径，避免 symlink 指向目标路径
                try:
                    entry_path_obj = Path(entry.path)
                    # 不使用 resolve()，直接计算相对路径
                    entry_rel_root = entry_path_obj.relative_to(self._root).as_posix()
                except Exception:
                    entry_rel_root = name
                entry_rel_target = Path(name).as_posix()

                # include_hidden=False 时，跳过隐藏文件和默认忽略列表
                if not include_hidden:
                    if name.startswith("."):
                        continue
                    if name in self.DEFAULT_IGNORE:
                        continue

                # 用户自定义 ignore 模式匹配
                if ignore and self._matches_ignore(name, entry_rel_root, entry_rel_target, ignore):
                    continue

                is_symlink = entry.is_symlink()

                # 判断是否为目录
                if is_symlink:
                    is_dir = self._symlink_points_to_dir_safe(entry)
                else:
                    is_dir = entry.is_dir()

                # 确定条目类型
                if is_symlink:
                    item_type = "link"
                elif is_dir:
                    item_type = "dir"
                else:
                    item_type = "file"

                # 条目的相对路径（用于 data.entries）
                entry_path = entry_rel_root

                items.append({
                    "name": name,
                    "type": item_type,
                    "path": entry_path,
                    "is_dir": is_dir,
                })

        # 排序：目录在前，文件在后，同类型按名称字母顺序排序
        items.sort(key=lambda x: (0 if x["is_dir"] else 1, x["name"].lower()))
        return items

    def _matches_ignore(self, name: str, rel_root: str, rel_target: str, patterns: List[str]) -> bool:
        """检查条目是否匹配任一 ignore 模式"""
        for pattern in patterns:
            if "/" in pattern or "\\" in pattern:
                if fnmatch.fnmatch(rel_root, pattern) or fnmatch.fnmatch(rel_target, pattern):
                    return True
                if pattern.startswith("**/"):
                    if fnmatch.fnmatch(name, pattern[3:]):
                        return True
                    if fnmatch.fnmatch(rel_root, pattern[3:]) or fnmatch.fnmatch(rel_target, pattern[3:]):
                        return True
            else:
                if fnmatch.fnmatch(name, pattern):
                    return True
        return False

    def _symlink_points_to_dir_safe(self, entry) -> bool:
        """安全检查 symlink 是否指向目录（必须在沙箱内）"""
        try:
            resolved = Path(entry.path).resolve()
            resolved.relative_to(self._root)
            return resolved.is_dir()
        except (ValueError, OSError):
            return False

    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义"""
        return [
            ToolParameter(
                name="path",
                type="string",
                description="Directory path to list (relative to project root or absolute within it)",
                required=False,
                default=".",
            ),
            ToolParameter(
                name="offset",
                type="integer",
                description="Pagination start index (>=0)",
                required=False,
                default=0,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="Max items to return (1-200)",
                required=False,
                default=100,
            ),
            ToolParameter(
                name="include_hidden",
                type="boolean",
                description="Whether to include hidden files (starting with '.')",
                required=False,
                default=False,
            ),
            ToolParameter(
                name="ignore",
                type="array",
                description="Optional list of glob patterns to ignore",
                required=False,
                default=None,
            ),
        ]

    def _format_response(
        self,
        rel_path: str,
        total: int,
        dirs_count: int,
        files_count: int,
        links_count: int,
        start: int,
        end: int,
        items: List[dict],
        params_input: Dict[str, Any],
        time_ms: int,
    ) -> str:
        """
        构建标准化响应（遵循《通用工具响应协议》）
        
        顶层字段仅包含：status, data, text, stats, context
        """
        # 判断是否截断
        truncated = end < total
        
        # 构建 data.entries（对象数组，每项包含 path 和 type）
        entries = [{"path": item["path"], "type": item["type"]} for item in items]
        
        # 构建 data
        data = {
            "entries": entries,
            "truncated": truncated,
        }
        
        # 构建 text（人类可读摘要）
        lines = []
        lines.append(f"Listed {len(entries)} entries in '{rel_path}'")
        lines.append(f"(Total: {total} items - {dirs_count} dirs, {files_count} files, {links_count} links)")
        
        if truncated:
            remaining = total - end
            lines.append(f"[Truncated: Showing {start}-{end} of {total}. {remaining} more items available.]")
            lines.append(f"Use offset={end} to view next page.")
        
        lines.append("")
        for item in items:
            # 显示格式：path + 类型标记
            display = item["path"]
            if item["type"] == "dir":
                display += "/"
            elif item["type"] == "link":
                display += "@"
            lines.append(display)
        
        text = "\n".join(lines)
        
        # 构建 extra_stats
        extra_stats = {
            "total_entries": total,
            "dirs": dirs_count,
            "files": files_count,
            "links": links_count,
            "returned": len(entries),
        }
        
        # 根据截断状态选择 success 或 partial
        if truncated:
            return self.create_partial_response(
                data=data,
                text=text,
                params_input=params_input,
                time_ms=time_ms,
                extra_stats=extra_stats,
                path_resolved=rel_path,
            )
        else:
            return self.create_success_response(
                data=data,
                text=text,
                params_input=params_input,
                time_ms=time_ms,
                extra_stats=extra_stats,
                path_resolved=rel_path,
            )
