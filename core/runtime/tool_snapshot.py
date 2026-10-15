"""Per-model-step tool capability seal.

The model receives copied JSON schemas, while execution later checks the exact
adapter identity *and* matching schema before dispatch. A changed registration
must be presented to the model on another step; it cannot be silently swapped
into an older LLM response. No adapter copies are retained beyond the step.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


def encode_tool_schema(schema: dict[str, Any]) -> str:
    """Canonical, strict JSON; reject unserializable or non-JSON schemas."""
    return json.dumps(
        schema, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    )


def schema_fingerprint(schema: dict[str, Any]) -> str:
    return hashlib.sha256(encode_tool_schema(schema).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolStepBinding:
    adapter: object
    schema_hash: str


@dataclass(frozen=True, slots=True)
class ToolStepSnapshot:
    """Registry-scoped capability seal for exactly one model step.

    The private JSON representation makes model_schemas return fresh objects;
    mutations to the LLM request cannot weaken execution-time enforcement.
    """
    registry: object
    _schemas_json: tuple[str, ...]
    bindings: Mapping[str, ToolStepBinding]

    @classmethod
    def create(
        cls, registry: object, schemas_json: list[str],
        bindings: dict[str, ToolStepBinding],
    ) -> "ToolStepSnapshot":
        return cls(registry, tuple(schemas_json), MappingProxyType(dict(bindings)))

    def model_schemas(self) -> list[dict[str, Any]]:
        return [json.loads(item) for item in self._schemas_json]
