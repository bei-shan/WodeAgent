"""Guard the v2 runtime contract: no deleted Feature SDK in live code."""
from __future__ import annotations

import ast
from pathlib import Path

from core.runtime.service_mcp import MCPService
from core.runtime.service_teams import TeamService
from core.runtime.service_sandbox import SandboxService
from core.runtime.middleware import PolicyMiddlewarePipeline


ROOT = Path(__file__).resolve().parents[1]


def test_no_old_feature_module_or_runtime_imports():
    assert not (ROOT / "core" / "features").exists()
    # Runtime code must not access AgentFeature, PluginLoader Python class
    # import or the old implicit global feature registry.
    for name in ("agents", "core", "desktop", "tools"):
        for path in (ROOT / name).rglob("*.py"):
            syntax = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(syntax):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith("core.features"), path
                if isinstance(node, ast.Import):
                    assert not any(alias.name.startswith("core.features")
                                   for alias in node.names), path


def test_hosted_policy_and_owned_services_are_distinct_kinds():
    assert not issubclass(MCPService, PolicyMiddlewarePipeline)
    assert not issubclass(TeamService, PolicyMiddlewarePipeline)
    assert not issubclass(SandboxService, PolicyMiddlewarePipeline)


def test_plugin_loader_cannot_import_project_python_entrypoints():
    import inspect
    from core.plugin_loader import PluginLoader

    source = inspect.getsource(PluginLoader)
    assert "exec_module" not in source
    assert "spec_from_file_location" not in source
    # The loader never recognizes an executable Python plugin contribution.
    assert "custom_features" not in source
    from core.plugin_loader import PluginManifest
    validator = inspect.getsource(PluginManifest)
    assert '"custom_features" in parts' in validator
    assert "not supported" in validator
