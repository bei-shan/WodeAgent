"""First-run interactive setup wizard for Ridge Code.

Detects whether a first-run setup is needed (no .env or missing API key)
and guides the user through configuring their LLM provider.

Usage::

    from scripts.first_run_wizard import run_wizard, should_run_wizard
    if should_run_wizard():
        run_wizard()
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("Please install rich: pip install rich")
    raise SystemExit(1)

console = Console()

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "1": {
        "key": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-v4-pro",
        "env_prefix": "DEEPSEEK",
    },
    "2": {
        "key": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "env_prefix": "OPENAI",
    },
    "3": {
        "key": "zhipu",
        "name": "Zhipu (智谱)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4",
        "env_prefix": "ZHIPU",
    },
    "4": {
        "key": "kimi",
        "name": "Kimi (月之暗面)",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-auto",
        "env_prefix": "KIMI",
    },
    "5": {
        "key": "qwen",
        "name": "Qwen (通义千问)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-max",
        "env_prefix": "DASHSCOPE",
    },
    "6": {
        "key": "ollama",
        "name": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "default_model": "llama3",
        "env_prefix": "OLLAMA",
    },
    "7": {
        "key": "custom",
        "name": "Custom (manual URL)",
        "base_url": "",
        "default_model": "",
        "env_prefix": "LLM",
    },
}


def should_run_wizard() -> bool:
    """Check whether the first-run wizard should be triggered."""
    env_path = Path(".env")
    if not env_path.exists():
        return True
    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return True
    # Check if any API key is configured.
    has_key = any(
        keyword in content
        for keyword in (
            "LLM_API_KEY=", "DEEPSEEK_API_KEY=", "OPENAI_API_KEY=",
            "ZHIPU_API_KEY=", "GLM_API_KEY=", "KIMI_API_KEY=",
            "MOONSHOT_API_KEY=", "DASHSCOPE_API_KEY=", "OLLAMA_API_KEY=",
        )
    )
    return not has_key


def run_wizard() -> bool:
    """Run the interactive setup wizard. Returns True if .env was created."""
    _show_welcome()
    provider = _choose_provider()
    api_key = _prompt_api_key(provider)
    if not api_key:
        console.print("[yellow]No API key provided. Setup cancelled.[/yellow]")
        return False
    enable_teams = _ask_features()
    if not _confirm(provider, api_key, enable_teams):
        console.print("[yellow]Setup cancelled. Run with --wizard to try again.[/yellow]")
        return False
    _write_env(provider, api_key, enable_teams)
    _show_complete()
    return True


def _show_welcome() -> None:
    console.print()
    console.print(
        Panel(
            "[bold cyan]🏔️ Welcome to Ridge Code![/bold cyan]\n\n"
            "Let's set up your environment.\n"
            "You can skip this with [bold]--skip-wizard[/bold].\n\n"
            "Press Enter to continue...",
            title="First Run Setup",
            border_style="cyan",
        )
    )
    input()


def _choose_provider() -> dict[str, str]:
    table = Table(title="Choose your LLM provider")
    table.add_column("#", style="cyan")
    table.add_column("Provider")
    table.add_column("Default Model", style="dim")
    for num, preset in PROVIDER_PRESETS.items():
        table.add_row(num, preset["name"], preset["default_model"])
    console.print(table)
    console.print()
    while True:
        choice = Prompt.ask("Choice", default="1", choices=list(PROVIDER_PRESETS.keys()))
        preset = PROVIDER_PRESETS.get(choice)
        if preset:
            return preset


def _prompt_api_key(provider: dict[str, str]) -> str:
    console.print()
    console.print(f"[bold]Provider:[/bold] {provider['name']}")
    if provider["base_url"]:
        console.print(f"[bold]Base URL:[/bold] {provider['base_url']}")
    console.print()
    if provider["key"] == "ollama":
        console.print("[dim]Ollama runs locally — no API key needed.[/dim]")
        return "ollama"
    key = Prompt.ask(
        f"Enter your {provider['name']} API key",
        password=True,
    )
    return key.strip()


def _ask_features() -> bool:
    console.print()
    console.print("[bold]Optional features:[/bold]")
    enable_teams = Confirm.ask(
        "Enable AgentTeams (multi-agent collaboration)?",
        default=False,
    )
    return enable_teams


def _confirm(provider: dict[str, str], api_key: str, enable_teams: bool) -> bool:
    console.print()
    console.print("[bold]Configuration summary:[/bold]")
    console.print(f"  LLM: {provider['name']} ({provider['default_model']})")
    masked = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
    console.print(f"  API Key: {masked}")
    console.print(f"  AgentTeams: {'enabled' if enable_teams else 'disabled'}")
    console.print()
    return Confirm.ask("Write to .env?", default=True)


def _write_env(provider: dict[str, str], api_key: str, enable_teams: bool) -> None:
    model = provider["default_model"]
    base_url = provider["base_url"]
    prefix = provider["env_prefix"]
    prov_key = provider["key"]

    lines = [
        "# ===== Ridge Code Configuration =====",
        f"# Generated by first-run wizard",
        "",
        f"# LLM Provider: {provider['name']}",
        f"LLM_PROVIDER={prov_key}",
        f"LLM_MODEL_ID={model}",
    ]
    if prov_key != "ollama":
        lines.append(f"LLM_API_KEY={api_key}")
    if base_url:
        lines.append(f"LLM_BASE_URL={base_url}")
    lines.extend([
        "",
        "# ===== Model Profiles =====",
        f"MODEL_PROFILES=main",
        f"MODEL_MAIN_ID={model}",
        f"MODEL_MAIN_PROVIDER={prov_key}",
    ])
    if prov_key != "ollama":
        lines.append(f"MODEL_MAIN_API_KEY={api_key}")
    if base_url:
        lines.append(f"MODEL_MAIN_BASE_URL={base_url}")
    lines.append("MODEL_POINTER_MAIN=main")
    lines.extend([
        "",
        "# ===== Features =====",
        f"ENABLE_AGENT_TEAMS={'true' if enable_teams else 'false'}",
        "# MCP_CONNECT_MODE=manual",
        "# AGENT_OUTPUT_STYLE=default",
        "",
        "# ===== Advanced =====",
        "# CONTEXT_WINDOW=128000",
        "# COMPRESSION_THRESHOLD=0.8",
        "# TRACE_ENABLED=true",
        "",
    ])
    # Secret-bearing config is atomically published with 0600 permissions.
    # The public CLI refuses to overwrite an existing file without --force.
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".ridge-env-", dir=".")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, ".env")
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def _show_complete() -> None:
    console.print()
    console.print(
        Panel(
            "[bold green]Setup complete![/bold green]\\n\\n"
            "Start the local server:\\n"
            "  [cyan]python -m ridgecode serve[/cyan]\\n\\n"
            "Start the Web client in another terminal:\\n"
            "  [cyan]python -m ridgecode web[/cyan]\\n\\n"
            "Or launch OpenTUI after installing Bun dependencies:\\n"
            "  [cyan]python -m ridgecode tui[/cyan]\\n\\n"
            "Your [bold].env[/bold] was created with restricted permissions.",
            border_style="green",
        )
    )
    console.print()


if __name__ == "__main__":
    # Never allow the old direct script invocation to silently succeed
    # without running setup; use the canonical command and overwrite guard.
    from ridgecode.cli import main as ridge_main
    raise SystemExit(ridge_main(["setup"]))
