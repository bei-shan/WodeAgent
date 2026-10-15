"""Deterministic, transactional lifecycle for the existing AgentFeature API.

Feature remains an *optional composition adapter*, not a security boundary
or a public plugin ABI. Dependencies should eventually be injected by a
runtime container rather than written to arbitrary Agent attributes.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from core.features.base import AgentFeature

log = logging.getLogger(__name__)


class FeatureLifecycle:
    """Initialize once, post-initialize in order, dispose entered units in LIFO.

    Mark a feature as entered BEFORE invoking init(), because init() itself
    can allocate resources and then raise. Cleanup is idempotent; failures of
    one feature do not prevent releasing the next. Caller owns non-feature
    resources such as sandboxes and the trace logger.
    """

    def __init__(self, agent: Any, features: Iterable[AgentFeature]):
        self._agent = agent
        self._features = tuple(features)
        self._entered: list[AgentFeature] = []
        self._closed = False
        self._initialized = False
        self._posted = False
        # Plugin features may use distinct names; prevent opaque type errors
        # mid-init and avoid mutable feature sequences during execution.
        for feature in self._features:
            if not isinstance(feature, AgentFeature):
                raise TypeError("Agent runtime feature must implement AgentFeature")

    def initialize(self) -> None:
        if self._initialized or self._entered or self._closed:
            raise RuntimeError("Feature lifecycle cannot initialize twice")
        for feature in self._features:
            self._entered.append(feature)
            feature.init(self._agent)
        self._initialized = True

    def post_initialize(self) -> None:
        if not self._initialized or self._posted or self._closed:
            raise RuntimeError("Feature post-init requires a live initialized runtime")
        for feature in self._features:
            feature.post_init(self._agent)
        self._posted = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for feature in reversed(self._entered):
            try:
                feature.cleanup(self._agent)
            except Exception:
                log.exception(
                    "Feature %s cleanup failed",
                    getattr(feature, "name", type(feature).__name__),
                )

    @property
    def active(self) -> tuple[AgentFeature, ...]:
        return tuple(self._entered)
