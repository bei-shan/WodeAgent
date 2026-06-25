import argparse
import json
import os
import sys
import time
import re
from pathlib import Path
from typing import Optional, Any

# Ensure project root is on sys.path when running as a script
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.env import load_env

load_env()

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.live import Live
    from rich.style import Style
    from rich.text import Text
    from rich.theme import Theme
    from rich.rule import Rule
    from rich.syntax import Syntax
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style as PromptStyle
    from prompt_toolkit.formatted_text import HTML
except ImportError:
    print("Please install required packages: pip install rich prompt_toolkit")
    sys.exit(1)

from core.llm import HelloAgentsLLM
from agents.codeAgent import CodeAgent
from tools.registry import ToolRegistry
from prompts.agents_prompts.init_prompt import CODE_LAW_GENERATION_PROMPT
from core.config import Config
from utils.ui_components import EnhancedUI, ToolCallTree
from tui.status_line import StatusLine
from tui.mention_completer import MentionCompleter
from tui.permission_dialog import PermissionDialog
from tui.streaming import StreamingResponse
from core.model_profiles import load_model_profiles, list_model_profiles
from scripts.slash_commands import SlashCommandRegistry, register_all_commands

# Geeky Theme
custom_theme = Theme({
    "info": "bright_cyan",
    "warning": "bright_yellow",
    "error": "bold bright_red",
    "user": "bold bright_green",
    "agent": "bold bright_blue",
    "banner": "bold bright_blue",
    "thinking": "italic bright_magenta",
    "action": "bold bright_cyan",
    "observation": "dim",
})

console = Console(theme=custom_theme)

class RichConsoleCodeAgent(CodeAgent):
    """
    Extensions of CodeAgent with Rich UI features.
    Overrides _console and _execute_tool to provide better visual feedback.
    """
    
    def __init__(self, *args, ui: Optional['EnhancedUI'] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ui = ui
        self._step_count = 0
        self._current_step_input_tokens = 0
        self._thinking_active = False
        
    def run(self, user_input: str, show_raw: bool = False) -> str:
        """Override run to integrate with enhanced UI"""
        # Start thinking timer
        if self.ui and not self._thinking_active:
            self.ui.start_thinking()
            self._thinking_active = True
            
        try:
            result = super().run(user_input, show_raw=show_raw)
            return result
        finally:
            # Stop thinking timer
            if self.ui and self._thinking_active:
                self.ui.stop_thinking()
                self._thinking_active = False
                
                # Update token tracker from trace logger if available
                if hasattr(self, 'trace_logger') and self.trace_logger:
                    usage = self.trace_logger.total_usage
                    if usage.get("total_tokens", 0) > 0:
                        self.ui.add_token_usage(
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                            "Session Total"
                        )
        
    def _console(self, message: str) -> None:
        """Override to render messages with Rich"""
        msg = message.strip()
        
        if "Engine 启动" in msg:
             pass # Skip start message to reduce noise
        elif "--- Step" in msg:
             console.print(Rule(style="dim", title=msg))
        elif "🤔 Thought:" in message: # Match with keyword as message might have newlines
             # Extract thought content
             content = message.split("🤔 Thought:", 1)[-1].strip()
             if content:
                 md = Markdown(content)
                 console.print(Panel(md, title="[thinking]Thinking[/thinking]", border_style="yellow", title_align="left"))
        elif "🧠 Reasoning:" in message:
             content = message.split("🧠 Reasoning:", 1)[-1].strip()
             if content:
                 md = Markdown(content)
                 console.print(Panel(md, title="[thinking]Reasoning[/thinking]", border_style="magenta", title_align="left"))
        elif "🎬 Action:" in message:
             # Action is usually followed by content, let's parse it
             content = message.split("🎬 Action:", 1)[-1].strip()
             console.print(Panel(Text(content, style="bold cyan"), title="[action]Action[/action]", border_style="cyan", title_align="left"))
        elif "👀 Observation:" in message:
             content = message.split("👀 Observation:", 1)[-1].strip()
             # Truncate if too long for display, but keep enough context
             if len(content) > 1000:
                  content = content[:1000] + "\n... (remaining content truncated for display)"
             
             # Attempt to highlight code if it looks like code
             if content.strip().startswith("{") or content.strip().startswith("["):
                 try:
                     json.loads(content)
                     renderable = Syntax(content, "json", theme="monokai", word_wrap=True)
                 except:
                     renderable = Text(content, style="dim")
             else:
                 renderable = Text(content, style="dim")
                 
             console.print(Panel(renderable, title="[observation]Observation[/observation]", border_style="dim", title_align="left"))
        elif "✅ Finish" in msg:
            pass # Finish is usually followed by the final answer which is printed separately
        elif "⏳" in msg or "Process" in msg:
            # We handle status via console.status in main loop or _execute_tool, so we can ignore simple progress msgs
            # or print them dimly
            console.print(f"[dim]{msg}[/dim]")
        elif "📎" in msg:
             console.print(f"[info]{msg}[/info]")
        elif "📦" in msg:
             console.print(f"[warning]{msg}[/warning]")
        else:
             # Fallback
             if msg:
                console.print(f"[dim]{msg}[/dim]")

    def _execute_tool(self, tool_name: str, tool_input: Any) -> str:
        """Override to show tool call in UI tree and spinner during execution"""
        # Show tool call in enhanced UI
        if self.ui:
            self.ui.show_tool_call(tool_name, tool_input)
            if tool_name in {"Task", "TeamFanout", "TeamCollect"}:
                mode = ""
                if isinstance(tool_input, dict):
                    mode = str(tool_input.get("mode", "") or "").strip()
                mode_suffix = f" mode={mode}" if mode else ""
                console.print(
                    f"[bold magenta]⚡ Team Dispatch[/bold magenta] "
                    f"{tool_name}{mode_suffix}"
                )
        
        with console.status(f"[bold cyan]Executing {tool_name}...[/bold cyan]", spinner="dots"):
            # artificial small delay to make the spinner visible if tool is too fast
            # time.sleep(0.1) 
            result = super()._execute_tool(tool_name, tool_input)

        if self.ui and self.enable_agent_teams and self.team_manager and tool_name in {"Task", "TeamFanout", "TeamCollect"}:
            try:
                state = self.team_manager.export_state()
                self.ui.show_team_progress(state)
            except Exception:
                pass
        return result

def _env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}

def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default

def _print_banner(code_law_exists: bool, ui: Optional['EnhancedUI'] = None) -> None:
    """Print banner - use enhanced UI if available"""
    if ui:
        ui.show_banner()
    else:
        banner_text = r"""
      /\_/\
     ( o.o )  [MyCat]
      > ^ <
        """
        console.print(Text(banner_text, style="banner"))
        console.print("[dim]Developer-first Coding Agent[/dim]")
    
    console.print("[dim]Type 'exit' to quit, '/model' to see model info[/dim]")
    
    if not code_law_exists:
        console.print(Panel("⚠️  code_law.md missing. Type 'init' to generate it.", style="yellow", title="Setup Required"))
    console.print()

def _default_session_path() -> str:
    sessions_dir = os.path.join(PROJECT_ROOT, "memory", "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    return os.path.join(sessions_dir, "session-latest.json")

def _maybe_save_session(agent: CodeAgent, path: str, flag: dict, reason: str) -> None:
    if flag.get("saved"):
        return
    try:
        agent.save_session(path)
        console.print(f"[dim]Auto-saved session ({reason}): {path}[/dim]")
        flag["saved"] = True
    except Exception as exc:
        console.print(f"[bold red]✗ Auto-save failed:[/bold red] {exc}")

def _print_assistant_response(text: str) -> None:
    md = Markdown(text)
    console.print(Panel(md, title="[agent]Assistant[/agent]", border_style="blue", expand=False))


def _print_session_tree(tree: dict, console_obj, highlight_id: str = None) -> None:
    """Print session tree as ASCII art using Rich Tree."""
    from rich.tree import Tree as RichTree

    nodes = tree.get("nodes", {})
    children = tree.get("children", {})
    cursor_id = tree.get("cursor_id", "")
    labels = tree.get("labels", {})

    # Find roots (parentId == None or "__root__")
    root_ids = children.get("__root__", [])
    if not root_ids:
        # Try null parentId
        for nid, node in nodes.items():
            if node.get("parentId") is None:
                root_ids.append(nid)

    def _add_children(rt: RichTree, pid: str, depth: int = 0):
        if depth > 50:
            return
        for cid in children.get(pid, []):
            node = nodes.get(cid)
            if not node:
                continue
            ntype = node.get("type", "message")
            label = labels.get(cid, "")
            prefix = ""

            if ntype == "message":
                role = node.get("role", "?")[0].upper()
                content = (node.get("content", "") or "")[:40].replace("\n", " ")
                prefix = f"[{role}]"
            elif ntype == "model_change":
                prefix = "[M]"
                content = f"{node.get('provider','')}/{node.get('modelId','')}"
            elif ntype == "thinking_level_change":
                prefix = "[T]"
                content = f"thinking={node.get('thinkingLevel','')}"
            elif ntype == "branch_summary":
                prefix = "[BS]"
                content = (node.get("summary", "") or "")[:40]
            elif ntype == "compaction":
                prefix = "[C]"
                content = "compaction"
            elif ntype == "leaf":
                # skip leaf entries in display
                _add_children(rt, cid, depth + 1)
                continue
            elif ntype == "label":
                prefix = "[L]"
                content = f"label: {label}"
            else:
                prefix = f"[{ntype[:4]}]"
                content = ""

            style = "bold cyan" if cid == cursor_id else ""
            marker = " ←" if cid == cursor_id else ""
            display = f"{prefix} {cid}{marker}"
            if label:
                display += f" ({label})"
            if content:
                display += f": {content}"

            child_tree = rt.add(display, style=style)
            _add_children(child_tree, cid, depth + 1)

    rt_main = RichTree("🌳 Session Tree")
    for rid in root_ids:
        node = nodes.get(rid)
        if not node:
            continue
        ntype = node.get("type", "message")
        role = node.get("role", "?")[0].upper() if ntype == "message" else ""
        content = (node.get("content", "") or "")[:40].replace("\n", " ")
        label = labels.get(rid, "")
        style = "bold cyan" if rid == cursor_id else ""
        marker = " ←" if rid == cursor_id else ""
        display = f"[{role}] {rid}{marker}: {content}"
        if label:
            display += f" ({label})"
        child = rt_main.add(display, style=style)
        _add_children(child, rid)

    console_obj.print(rt_main)


def check_code_law_exists(project_root: str) -> bool:
    """Check if code_law.md exists"""
    code_law_path = Path(project_root) / "code_law.md"
    return code_law_path.exists()

def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with CodeAgent")
    parser.add_argument("--name", default="code", help="agent name")
    parser.add_argument("--system", default=None, help="system prompt")
    parser.add_argument("--provider", default=None, help="llm provider (override LLM_PROVIDER)")
    parser.add_argument("--model", default=None, help="model name (override LLM_MODEL_ID)")
    parser.add_argument("--api-key", default=None, help="api key (override LLM_API_KEY)")
    parser.add_argument("--base-url", default=None, help="base url (override LLM_BASE_URL)")
    parser.add_argument("--temperature", type=float, default=None, help="temperature (override TEMPERATURE)")
    parser.add_argument(
        "--teammate-mode",
        choices=["auto", "in-process", "tmux"],
        default=None,
        help="teammate display mode (override TEAMMATE_MODE)",
    )
    parser.add_argument("--show-raw", action="store_true", help="print raw response structure")
    parser.add_argument("--plan", action="store_true", dest="plan_mode", help="start in plan-only mode")
    parser.add_argument("-c", "--continue", action="store_true", dest="continue_last", help="resume the most recent session")
    parser.add_argument("-r", "--resume", default=None, dest="resume_id", help="resume a session by ID, index, or prefix")
    parser.add_argument("--wizard", action="store_true", help="force first-run setup wizard")
    parser.add_argument("--skip-wizard", action="store_true", help="skip first-run setup wizard")
    args = parser.parse_args()

    # First-run wizard
    if args.wizard:
        from scripts.first_run_wizard import run_wizard
        if not run_wizard():
            return
    elif not args.skip_wizard:
        from scripts.first_run_wizard import should_run_wizard, run_wizard
        if should_run_wizard():
            if not run_wizard():
                return

    # Initialize config first (used for temperature fallback)
    config = Config.from_env()
    if args.teammate_mode is not None:
        config.teammate_mode = args.teammate_mode

    # Initialize LLM
    try:
        llm = HelloAgentsLLM(
            model=args.model,
            api_key=args.api_key,
            base_url=args.base_url,
            provider=args.provider,
            temperature=args.temperature if args.temperature is not None else config.temperature,
        )
    except Exception as e:
        console.print(f"[error]Failed to initialize LLM: {e}[/error]")
        return

    tool_registry = ToolRegistry()
   
    # Ensure config has show_react_steps=True for our RichConsoleCodeAgent to receive events
    config.show_react_steps = True

    # Initialize Enhanced UI
    enhanced_ui = EnhancedUI(
        console=console,
        model=llm.model,
        provider=llm.provider,
        project_root=PROJECT_ROOT,
        version="v1.0"
    )

    agent = RichConsoleCodeAgent(
        name=args.name,
        llm=llm,
        tool_registry=tool_registry,
        project_root=PROJECT_ROOT,
        system_prompt=args.system,
        config=config,
        ui=enhanced_ui,
    )

    # Start in plan mode if --plan flag is set
    if args.plan_mode:
        agent.enter_plan_mode()
        console.print("[bold yellow]Starting in plan mode (--plan). Only read-only tools available.[/bold yellow]")

    # Resume session if -c or -r
    if args.resume_id:
        resolved = agent.resolve_session_id(args.resume_id)
        if resolved:
            agent.resume_session(resolved)
            console.print(f"[bold green]✓ Resumed session:[/bold green] {resolved}")
        else:
            console.print(f"[bold red]✗ Session not found:[/bold red] {args.resume_id}")
    elif args.continue_last:
        sessions = agent.list_sessions()
        if sessions:
            # sessions are sorted by modified_at desc
            agent.resume_session(sessions[0]["id"])
            console.print(f"[bold green]✓ Resumed most recent session:[/bold green] {sessions[0]['title']}")

    code_law_exists = check_code_law_exists(PROJECT_ROOT)
    _print_banner(code_law_exists, enhanced_ui)
    auto_save_path = _default_session_path()
    auto_save_flag = {"saved": False}

    # Setup TUI components
    history_file = os.path.join(PROJECT_ROOT, ".chat_history")
    status_line = StatusLine(agent)

    # @-mention completer
    def _get_agents():
        names = ["general", "explore", "plan", "summary"]
        if agent.enable_agent_teams:
            try:
                for t in agent.team_manager.store.list_teams():
                    names.append(f"team:{t}")
            except Exception:
                pass
        return names

    def _get_models():
        profiles = load_model_profiles()
        if profiles:
            return [p.name for p in profiles.values()]
        return [agent.llm.model]

    def _get_skills():
        try:
            skills = agent._skill_loader.list_skills()
            return [s.name for s in skills]
        except Exception:
            return []

    mention_completer = MentionCompleter(
        get_agents=_get_agents,
        get_models=_get_models,
        get_skills=_get_skills,
        project_root=Path(PROJECT_ROOT),
    )

    # Permission dialog (replaces input() in PermissionGate)
    permission_dialog = PermissionDialog()
    # Patch agent's permission gate to use the dialog
    if hasattr(agent, "_permission_gate") and agent._permission_gate:
        agent._permission_gate.ask = lambda path, tool, action: permission_dialog.ask(
            tool=tool, path=path, action=action
        )

    # Prompt with @-mention + status line
    prompt_style_dict = {
        "user": "#00ff00 bold",
        "arrow": "#0000ff",
        "host": "#00ffff",
        "model": "#888888 italic",
        "plan": "#ffff00 bold",
        "worktree": "#00ffff italic",
        "style": "#ff8800 italic",
    }
    session = PromptSession(
        history=FileHistory(history_file),
        completer=mention_completer,
        style=PromptStyle.from_dict(prompt_style_dict),
    )

    # Streaming response renderer
    stream = StreamingResponse(console)

    # Initialize slash command registry
    slash_registry = SlashCommandRegistry()
    register_all_commands(slash_registry, agent)

    try:
        while True:
            try:
                prompt_html = HTML(status_line.prompt_html())
                user_input = session.prompt(prompt_html).strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye![/dim]")
                _maybe_save_session(agent, auto_save_path, auto_save_flag, "keyboard interrupt")
                break

            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit", "q"}:
                console.print("\n[dim]Shutting down...[/dim]")
                _maybe_save_session(agent, auto_save_path, auto_save_flag, "exit")
                break
                
            # Handle slash commands (via SlashCommandRegistry)
            if user_input.startswith("/"):
                if slash_registry.dispatch(agent, user_input, console):
                    continue

            # Init command handling
            if "init" in user_input.lower() and len(user_input) < 10:
                if code_law_exists:
                    console.print("\n[warning]code_law.md already exists.[/warning]")
                    confirm = session.prompt("Regenerate? (yes/no): ").strip().lower()
                    if confirm != "yes":
                        console.print("Cancelled.")
                        continue
                
                console.print("[info]Initiailizing Agent Protocol...[/info]")
                enhanced_input = f"{CODE_LAW_GENERATION_PROMPT}\n\n请使用 LS、Glob、Grep、Read 等工具探索项目，然后使用 Write 工具生成 code_law.md 文件。"
                
                # Reset UI state for new request
                enhanced_ui.tool_tree = ToolCallTree()
                enhanced_ui.token_tracker.calls.clear()
                
                start_time = time.time()
                console.print()
                
                response = agent.run(enhanced_input, show_raw=args.show_raw)
                
                elapsed = time.time() - start_time
                
                # Show tool tree and token summary
                console.print()
                enhanced_ui.show_tool_tree()
                _print_assistant_response(response)
                
                # Show timing and summary
                timing_text = Text()
                timing_text.append(f"⏱️  Completed in {elapsed:.1f}s", style="dim cyan")
                console.print(timing_text)
                enhanced_ui.show_token_summary()
                console.print()
                
                if check_code_law_exists(PROJECT_ROOT):
                    console.print("[bold green]✓ code_law.md generated successfully.[/bold green]")
                    code_law_exists = True
                else:
                    console.print("[bold red]✗ Failed to generate code_law.md[/bold red]")
            else:
                # Normal chat
                # Reset UI state for new request
                enhanced_ui.tool_tree = ToolCallTree()
                enhanced_ui.token_tracker.calls.clear()

                # Show thinking with streaming response
                start_time = time.time()
                console.print()

                # Stream the response
                stream.start("Agent")
                response = agent.run(user_input, show_raw=args.show_raw)
                stream.finish()

                elapsed = time.time() - start_time

                # Show tool tree and response
                console.print()
                enhanced_ui.show_tool_tree()
                _print_assistant_response(response)

                # Show timing and token summary
                timing_text = Text()
                timing_text.append(f"⏱️  Completed in {elapsed:.1f}s", style="dim cyan")
                console.print(timing_text)
                enhanced_ui.show_token_summary()
                if agent.enable_agent_teams and agent.team_manager:
                    try:
                        enhanced_ui.show_team_progress(agent.team_manager.export_state())
                    except Exception:
                        pass
                console.print()

            if args.show_raw and hasattr(agent, "last_response_raw") and agent.last_response_raw is not None:
                console.print(Panel(json.dumps(agent.last_response_raw, ensure_ascii=False, indent=2), title="Raw Response", border_style="dim"))
                
    finally:
        _maybe_save_session(agent, auto_save_path, auto_save_flag, "finalize")
        agent.close()

if __name__ == "__main__":
    main()
