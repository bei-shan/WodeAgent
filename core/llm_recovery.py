"""Classify model-provider errors and bound retries to safe requests only.

LLM operations are read-only requests to the provider: retrying *before*
a response streams is safe locally. Once any stream chunk has arrived,
retrying would duplicate visible text and potentially diverge tool calls.
Tool executions are intentionally outside this policy.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass

from core.exceptions import HelloAgentsException


@dataclass(frozen=True)
class ErrorPolicy:
    kind: str
    retryable: bool
    message: str


_MESSAGES = {
    "rate_limited": "模型服务正在限流，请稍后重试",
    "provider_unavailable": "模型服务暂时不可用，请稍后重试",
    "network": "模型连接失败，请检查网络或服务地址",
    "context_overflow": "上下文超出模型限制，请压缩对话后重试",
    "unauthorized": "模型认证失败，请检查 API 配置",
    "invalid_request": "模型请求无效，请检查模型和参数设置",
    "stream_interrupted": "模型流式响应中断，已保留当前内容，未自动重试",
    "output_truncated": "模型输出因长度限制被截断，已保留当前内容，任务未完成",
    "output_filtered": "模型服务因内容过滤拦截了输出，任务未完成，未自动重试",
    "empty_response": "模型未返回有效回答，任务未完成",
    "step_limit": "无法在限定步数内完成这个任务",
    "provider_error": "模型请求失败，请检查服务状态与日志",
}


class ModelInvocationError(HelloAgentsException):
    def __init__(self, kind: str, retryable: bool = False, *, partial_messages=None):
        self.kind = kind
        self.retryable = retryable
        # Private diagnostic history for worker owners; never a final result.
        self.partial_messages = partial_messages
        super().__init__(_MESSAGES.get(kind, _MESSAGES["provider_error"]))


def incomplete_response_kind(finish_reason: str | None) -> str | None:
    """Map supported provider stops that invalidate both text and tool calls."""
    return {"length": "output_truncated", "content_filter": "output_filtered"}.get(finish_reason)


def classify_model_error(exc: BaseException) -> ErrorPolicy:
    if isinstance(exc, ModelInvocationError):
        return ErrorPolicy(exc.kind, exc.retryable, str(exc))
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    name = type(exc).__name__.lower()
    # Status takes precedence over text: providers sometimes include
    # misleading words such as "retry" in a permanent validation error.
    kind = "provider_error"
    if status == 429:
        kind = "rate_limited"
    elif status in {408, 409, 425, 500, 502, 503, 504}:
        kind = "provider_unavailable"
    elif status in {401, 403}:
        kind = "unauthorized"
    elif status in {400, 413, 422}:
        kind = "invalid_request"
    elif status is None and isinstance(exc, (TimeoutError, ConnectionError, socket.timeout, OSError)):
        kind = "network"
    elif status is None and any(p in name for p in ("timeout", "connection", "network")):
        kind = "network"

    # Provider-specific prompt-too-long errors are often HTTP 400.
    message = str(exc).lower()[:700]
    if status in {400, 413, 422, None} and any(word in message for word in (
        "context_length_exceeded", "prompt is too long",
        "context window", "maximum context length", "too many tokens",
    )):
        kind = "context_overflow"

    return ErrorPolicy(kind, kind in {"rate_limited", "provider_unavailable", "network"}, _MESSAGES[kind])


def retry_delay(attempt: int, base: float, max_seconds: float = 8.0) -> float:
    """Deterministic bounded exponential backoff, preserving fast tests."""
    return min(max_seconds, max(0.0, base) * (2 ** min(max(0, attempt), 10)))
