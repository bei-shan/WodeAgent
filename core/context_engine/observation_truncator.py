"""工具输出截断器（统一策略）

根据《工具输出截断设计文档》实现统一的工具输出截断策略。

本模块**完全替换**原有的 ToolResultCompressor：
- 不再为每个工具设计特化压缩
- 所有工具输出使用同一套截断规则
- 完整输出落盘保存，随时可回溯

截断规则：
- MAX_LINES = 2000 (默认)
- MAX_BYTES = 50KB (默认)
- 截断方向：head（默认，保留前 N 行）或 tail（保留后 N 行）

落盘策略：
- 目录：项目根目录下 tool-output/
- 文件名：tool_<timestamp>_<toolname>.json
- 保留 7 天，过期自动清理
"""

import json
import os
import re
import secrets
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

from core.env import load_env
from core.runtime.config_scope import active_config

if TYPE_CHECKING:
    from core.config import Config

load_env()

logger = logging.getLogger(__name__)


# =============================================================================
# 配置常量（可通过环境变量或 Config 对象覆盖）
# =============================================================================

_config: "Optional[Config]" = None


def set_truncator_config(config: "Config") -> None:
    """注入统一配置对象，优先于 os.getenv()。"""
    global _config
    _config = config


def _get_max_lines() -> int:
    config = active_config() or _config
    if config is not None:
        return config.tool_output_max_lines
    return int(os.getenv("TOOL_OUTPUT_MAX_LINES", "2000"))

def _get_max_bytes() -> int:
    config = active_config() or _config
    if config is not None:
        return config.tool_output_max_bytes
    return int(os.getenv("TOOL_OUTPUT_MAX_BYTES", "51200"))  # 50KB

def _get_truncate_direction() -> str:
    config = active_config() or _config
    if config is not None:
        return config.tool_output_truncate_direction
    direction = os.getenv("TOOL_OUTPUT_TRUNCATE_DIRECTION", "head").lower().strip()
    return direction if direction in {"head", "tail", "head_tail"} else "head"

def _get_head_tail_lines() -> int:
    config = active_config() or _config
    if config is not None:
        return config.tool_output_head_tail_lines
    value = os.getenv("TOOL_OUTPUT_HEAD_TAIL_LINES", "40")
    try:
        count = int(value)
    except ValueError:
        return 40
    return max(count, 1)

def _get_output_dir() -> str:
    config = active_config() or _config
    if config is not None:
        return config.tool_output_dir
    return os.getenv("TOOL_OUTPUT_DIR", "tool-output")

def _get_retention_days() -> int:
    config = active_config() or _config
    if config is not None:
        return config.tool_output_retention_days
    return int(os.getenv("TOOL_OUTPUT_RETENTION_DAYS", "7"))


# =============================================================================
# 观测输出截断器
# =============================================================================

class ObservationTruncator:
    """
    工具输出截断器
    
    对所有工具的输出执行统一的截断策略：
    1. 检查输出大小（行数 + 字节数）
    2. 超过阈值时截断 + 落盘
    3. 返回包含截断信息和文件路径的结果
    """
    
    def __init__(self, project_root: Optional[str] = None):
        """
        初始化截断器
        
        Args:
            project_root: 项目根目录，用于确定落盘路径
        """
        self._project_root = (Path(project_root) if project_root else Path.cwd()).resolve()
        relative = Path(_get_output_dir())
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("Tool output directory must be relative to project root")
        self._output_dir = self._project_root / relative
        
    def truncate(self, tool_name: str, raw_result: str) -> str:
        """
        对工具输出进行截断处理
        
        Args:
            tool_name: 工具名称
            raw_result: 工具返回的原始 JSON 字符串
            
        Returns:
            处理后的 JSON 字符串（可能已截断）
        """
        # 尝试解析 JSON
        parsed = None
        try:
            parsed = json.loads(raw_result)
        except json.JSONDecodeError:
            logger.warning("Failed to parse tool result as JSON; treating as plain text")

        # Tool result metadata is untrusted and cannot disable output bounds.
        # 使用可读文本进行尺寸判断（将 \\n 还原为真实换行）
        preview_source = self._normalize_text(raw_result)
        content_size = self._get_content_size(preview_source)
        if not self._exceeds_limits(content_size):
            return raw_result

        # 执行截断
        return self._do_truncate(tool_name, raw_result, preview_source, parsed, content_size)
    
    def _get_content_size(self, text: str) -> Dict[str, int]:
        """获取内容大小信息"""
        lines = text.count("\n") + 1
        bytes_count = len(text.encode("utf-8"))
        return {
            "lines": lines,
            "bytes": bytes_count,
        }
    
    def _exceeds_limits(self, size: Dict[str, int]) -> bool:
        """检查是否超过限制"""
        max_lines = _get_max_lines()
        max_bytes = _get_max_bytes()
        return size["lines"] > max_lines or size["bytes"] > max_bytes
    
    def _do_truncate(
        self,
        tool_name: str,
        raw_result: str,
        preview_source: str,
        parsed_result: Optional[Dict[str, Any]],
        original_size: Dict[str, int],
    ) -> str:
        """
        执行截断操作
        
        1. 保存完整输出到文件
        2. 截断内容
        3. 构建新的响应
        """
        max_lines = _get_max_lines()
        max_bytes = _get_max_bytes()
        direction = _get_truncate_direction()
        head_tail_lines = _get_head_tail_lines()
        
        # 1. 保存完整输出
        output_path = self._save_full_output(tool_name, raw_result)
        relative_path = str(output_path.relative_to(self._project_root)) if output_path else None
        
        # 2. 截断内容（基于可读文本）
        preview_text, kept_size = self._truncate_content(
            preview_source,
            max_lines,
            max_bytes,
            direction,
            head_tail_lines,
        )
        
        # 3. 构建截断后的响应（统一结构）
        status = "success"
        error = None
        stats = {}
        context = {}
        if isinstance(parsed_result, dict):
            status = parsed_result.get("status", "success")
            error = parsed_result.get("error")
            # Tool-supplied metadata is untrusted: only keep small scalar
            # diagnostics, never arbitrary nested payloads or control flags.
            def small_fields(value):
                if not isinstance(value, dict):
                    return {}
                result = {}
                for key, item in list(value.items())[:32]:
                    if not isinstance(key, str) or len(key) > 64 or key == "truncation_skip":
                        continue
                    if not isinstance(item, (str, int, float, bool, type(None))):
                        continue
                    if isinstance(item, str) and len(item) > 128:
                        continue
                    trial = {**result, key: item}
                    try:
                        if len(json.dumps(trial, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= 256:
                            result = trial
                    except (TypeError, ValueError):
                        continue
                return result
            stats = small_fields(parsed_result.get("stats"))
            context = small_fields(parsed_result.get("context"))

        status = "error" if status == "error" else "partial"

        truncated_result: Dict[str, Any] = {
            "status": status,
            "data": {
                "truncated": True,
                "truncation": {
                    "direction": direction,
                    "max_lines": max_lines,
                    "max_bytes": max_bytes,
                    "head_tail_lines": head_tail_lines if direction == "head_tail" else None,
                    "original_lines": original_size["lines"],
                    "original_bytes": original_size["bytes"],
                    "kept_lines": kept_size["lines"],
                    "kept_bytes": kept_size["bytes"],
                },
                "preview": preview_text,
            },
            "text": "",
            "stats": stats,
            "context": context,
        }

        if relative_path:
            truncated_result["data"]["truncation"]["full_output_path"] = relative_path

        # 保留错误字段
        if status == "error" and error:
            if isinstance(error, dict):
                truncated_result["error"] = {
                    "code": str(error.get("code") or "TOOL_ERROR")[:64],
                    "message": str(error.get("message") or "")[:160],
                }
            else:
                truncated_result["error"] = {
                    "code": "TOOL_ERROR", "message": str(error)[:160],
                }

        # 构建提示文本
        hint = self._build_hint(tool_name, relative_path, original_size)
        truncated_result["text"] = hint
        
        # 清理过期文件（低频执行）
        self._maybe_cleanup()
        
        # Bound the entire serialized result, not only the preview. Even a
        # malicious tool's stats/context/error cannot restore unbounded output.
        limit = max(2, int(max_bytes))
        for _ in range(64):
            reduced = json.dumps(truncated_result, ensure_ascii=False,
                                 separators=(",", ":"), default=str)
            if len(reduced.encode("utf-8")) <= limit:
                return reduced
            preview = truncated_result["data"]["preview"]
            if preview:
                preview = preview[:len(preview) // 2]
                truncated_result["data"]["preview"] = preview
                truncated_result["data"]["truncation"]["kept_bytes"] = len(preview.encode("utf-8"))
                truncated_result["data"]["truncation"]["kept_lines"] = preview.count("\n") + (1 if preview else 0)
                continue
            if truncated_result.get("stats") or truncated_result.get("context"):
                truncated_result["stats"] = {}
                truncated_result["context"] = {}
                continue
            if "error" in truncated_result:
                truncated_result.pop("error")
                continue
            if truncated_result.get("text") != "Tool output truncated":
                truncated_result["text"] = "Tool output truncated"
                continue
            if truncated_result["data"]["truncation"].pop("full_output_path", None):
                continue
            break
        for fallback in (
            json.dumps({"status": status, "data": {"truncated": True}, "text": "Tool output truncated"},
                       ensure_ascii=False, separators=(",", ":")),
            '{"status":"partial"}',
            "{}",
        ):
            if len(fallback.encode("utf-8")) <= limit:
                return fallback
        return "{}"
    
    def _truncate_content(
        self,
        content: str,
        max_lines: int,
        max_bytes: int,
        direction: str,
        head_tail_lines: int,
    ) -> Tuple[str, Dict[str, int]]:
        """
        按行数和字节数截断内容
        
        Returns:
            (截断后的内容, 保留的大小信息)
        """
        lines = content.split("\n")
        
        # 按行数截断
        if direction == "head_tail":
            if len(lines) > head_tail_lines * 2:
                kept_lines = (
                    lines[:head_tail_lines]
                    + ["... (truncated) ..."]
                    + lines[-head_tail_lines:]
                )
            else:
                kept_lines = lines
        elif direction == "tail":
            kept_lines = lines[-max_lines:] if len(lines) > max_lines else lines
        else:  # head
            kept_lines = lines[:max_lines] if len(lines) > max_lines else lines
        
        truncated = "\n".join(kept_lines)
        
        # 按字节数进一步截断
        encoded = truncated.encode("utf-8")
        if len(encoded) > max_bytes:
            if direction == "tail":
                # 从尾部保留
                truncated = encoded[-max_bytes:].decode("utf-8", errors="ignore")
            else:
                # 从头部保留
                truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        
        kept_size = {
            "lines": truncated.count("\n") + 1,
            "bytes": len(truncated.encode("utf-8")),
        }
        
        return truncated, kept_size

    def _normalize_text(self, raw_result: str) -> str:
        """将 JSON 字符串中的 \\n 还原为真实换行，提升可读性"""
        return raw_result.replace("\\n", "\n")
    
    def _save_full_output(self, tool_name: str, content: str) -> Optional[Path]:
        """Write private, unique output receipts; never trust a tool name as a path."""
        try:
            # Reject already-symlinked output directories and ancestors.
            # Hostile concurrent filesystem mutation still needs OS isolation.
            current = self._project_root
            for component in self._output_dir.relative_to(self._project_root).parts:
                current = current / component
                if current.is_symlink():
                    raise ValueError("Symlinked tool output directory")
                current.mkdir(exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(tool_name))[:40] or "tool"
            filename = f"tool_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}_{secrets.token_hex(8)}.json"
            filepath = self._output_dir / filename
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(filepath, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(content)
            logger.debug("Stored private tool output receipt")
            return filepath
        except (OSError, ValueError) as exc:
            logger.warning("Could not persist tool output receipt: %s", type(exc).__name__)
            return None

    def _build_hint(
        self,
        tool_name: str,
        output_path: Optional[str],
        original_size: Dict[str, int],
    ) -> str:
        """构建截断提示信息"""
        lines = original_size["lines"]
        bytes_kb = original_size["bytes"] / 1024
        
        hint = f"⚠️ 输出过大已截断 ({lines} 行, {bytes_kb:.1f}KB)"
        
        if output_path:
            hint += f"\n完整内容见: {output_path}"
            hint += "\n建议: 可用 Task 让子代理处理，或用 Read 分页读取 / Grep 搜索"
        
        return hint
    
    def _maybe_cleanup(self):
        """
        可能执行清理操作
        
        使用概率触发避免每次都检查
        """
        import random
        if random.random() > 0.1:  # 10% 概率触发
            return
        
        self._cleanup_expired_files()
    
    def _cleanup_expired_files(self):
        """清理过期的输出文件"""
        try:
            if not self._output_dir.exists():
                return
            
            retention_days = _get_retention_days()
            cutoff = datetime.now() - timedelta(days=retention_days)
            
            for filepath in self._output_dir.glob("tool_*.json"):
                try:
                    mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                    if mtime < cutoff:
                        filepath.unlink()
                        logger.debug("Deleted expired file: %s", filepath)
                except Exception as e:
                    logger.warning("Failed to delete %s: %s", filepath, e)
                    
        except Exception as e:
            logger.warning("Cleanup failed: %s", e)


# =============================================================================
# 单例与便捷函数
# =============================================================================

_truncator_instance: Optional[ObservationTruncator] = None


def get_truncator(project_root: Optional[str] = None) -> ObservationTruncator:
    """获取截断器单例"""
    global _truncator_instance
    if _truncator_instance is None or project_root is not None:
        _truncator_instance = ObservationTruncator(project_root)
    return _truncator_instance


def truncate_observation(
    tool_name: str,
    raw_result: str,
    project_root: Optional[str] = None,
) -> str:
    """
    截断工具输出的便捷函数
    
    Args:
        tool_name: 工具名称
        raw_result: 原始 JSON 结果字符串
        project_root: 项目根目录
        
    Returns:
        处理后的 JSON 字符串
    """
    truncator = get_truncator(project_root)
    return truncator.truncate(tool_name, raw_result)


# =============================================================================
# 兼容旧接口（过渡期）
# =============================================================================

def compress_tool_result(tool_name: str, raw_result: str) -> str:
    """
    兼容旧的 ToolResultCompressor 接口
    
    此函数保持旧接口兼容性，内部使用新的截断策略。
    
    Args:
        tool_name: 工具名称
        raw_result: 原始 JSON 结果字符串
        
    Returns:
        处理后的 JSON 字符串
    """
    return truncate_observation(tool_name, raw_result)
