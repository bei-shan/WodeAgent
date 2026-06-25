"""SlashCommandRegistry — replaces 530-line if-elif chain in chat_test_agent.py.

Each command is a (match_fn, handler_fn) pair.  Handlers receive
(agent, user_input, console) and return True if the input was consumed.
"""

from __future__ import annotations

from typing import Any, Callable

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel


# Backward-compatible: accept either CodeAgent or RichConsoleCodeAgent
def _get_agent_module():
    from agents.codeAgent import CodeAgent
    return CodeAgent


MatchFn = Callable[[str], bool]
HandlerFn = Callable[[Any, str, Console], bool]  # (agent, user_input, console) → handled


class SlashCommandRegistry:
    """Register and dispatch slash commands by pattern matching."""

    def __init__(self):
        self._commands: list[tuple[MatchFn, HandlerFn]] = []

    def register(self, pattern: str, handler: HandlerFn) -> None:
        """Register a handler for a pattern.

        Patterns:
          ``"/model"``        — exact match
          ``"/model *"``      — prefix match (any args)
          ``"/thinking"``     — exact, also matches /thinking on|off via ``startswith``
        """

        def _match(user_input: str) -> bool:
            inp = user_input.strip()
            if " " in pattern or pattern.endswith(" *"):
                prefix = pattern.rstrip(" *")
                return inp == prefix or inp.startswith(prefix + " ")
            # For /thinking: also match /thinking with args
            if pattern == "/thinking":
                return inp == "/thinking" or inp.startswith("/thinking ")
            if pattern == "/tree":
                return inp == "/tree" or inp.startswith("/tree ")
            return inp.lower() == pattern.lower()

        self._commands.append((_match, handler))

    def dispatch(self, agent, user_input: str, console: Console) -> bool:
        """Try each registered command. Return True if handled."""
        for match, handler in self._commands:
            if match(user_input):
                handler(agent, user_input, console)
                return True
        return False


def register_all_commands(registry: SlashCommandRegistry, agent) -> None:
    """Register all built-in slash commands."""
    from core.model_profiles import load_model_profiles
    import json

    # ── /model ──
    def _model(agent, inp, console):
        enhanced_ui = getattr(agent, "_ui", None)
        if enhanced_ui:
            enhanced_ui.show_banner()
            enhanced_ui.show_detailed_token_summary()

    registry.register("/model", _model)

    # ── /model <id> ──
    def _model_switch(agent, inp, console):
        new_model = inp.split(maxsplit=1)[1].strip()
        if not new_model:
            console.print("[bold red]✗ Usage: /model <model-id>[/bold red]")
            return
        try:
            previous = agent.llm.model
            agent.switch_model(model=new_model)
            console.print(f"[bold green]✓ Switched:[/bold green] {previous} → {agent.llm.model}")
        except Exception as exc:
            console.print(f"[bold red]✗ Switch failed:[/bold red] {exc}")

    registry.register("/model *", _model_switch)

    # ── /info ──
    def _info(agent, inp, console):
        enhanced_ui = getattr(agent, "_ui", None)
        if enhanced_ui:
            enhanced_ui.show_banner()
            enhanced_ui.show_detailed_token_summary()

    registry.register("/info", _info)

    # ── /plan ──
    def _plan(agent, inp, console):
        if getattr(agent, "_in_plan_mode", False):
            console.print("[bold yellow]Already in plan mode. Use ExitPlanMode tool to exit.[/bold yellow]")
            return
        agent.enter_plan_mode()
        console.print("[bold green]✓ Entered plan mode (read-only tools only).[/bold green]")

    registry.register("/plan", _plan)

    # ── /sessions ──
    def _sessions(agent, inp, console):
        sessions = agent.list_sessions()
        if not sessions:
            console.print("[dim]No saved sessions[/dim]")
            return
        for i, s in enumerate(sessions):
            marker = " ←" if s["id"] == agent._session_id else ""
            preview = (s.get("preview") or "")[:60]
            console.print(f"  {i}: [bold]{s['title']}[/bold]{marker} ({s['id'][:8]}...): {preview}")

    registry.register("/sessions", _sessions)

    # ── /resume <id> ──
    def _resume(agent, inp, console):
        identifier = inp.split(maxsplit=1)[1].strip()
        sid = agent.resolve_session_id(identifier)
        if not sid:
            console.print(f"[bold red]✗ Session not found:[/bold red] {identifier}")
            return
        if agent.resume_session(sid):
            console.print(f"[bold green]✓ Resumed session:[/bold green] {sid[:12]}...")
        else:
            console.print(f"[bold red]✗ Failed to resume:[/bold red] {identifier}")

    registry.register("/resume *", _resume)

    # ── /rename <title> ──
    def _rename(agent, inp, console):
        title = inp.split(maxsplit=1)[1].strip()
        if not title:
            console.print("[bold red]✗ Usage: /rename <title>[/bold red]")
            return
        if agent.rename_session(title):
            console.print(f"[bold green]✓ Renamed to:[/bold green] {title}")
        else:
            console.print("[bold red]✗ Rename failed[/bold red]")

    registry.register("/rename *", _rename)

    # ── /budget ──
    def _budget(agent, inp, console):
        parts = inp.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            budget = getattr(agent, "_token_budget", None)
            if budget:
                console.print(f"[bold]Token budget:[/bold] {budget.remaining()}/{budget.total}")
            else:
                console.print("[dim]No budget set. Use /budget 500k to set one.[/dim]")
            return
        try:
            from core.budget_tracker import TokenBudget
            budget = TokenBudget.parse(arg)
            agent._token_budget = budget
            console.print(f"[bold green]✓ Budget set:[/bold green] {budget.total}")
        except ValueError:
            console.print("[bold red]✗ Invalid budget format. Use /budget 500k or /budget 10万[/bold red]")

    registry.register("/budget", _budget)
    registry.register("/budget *", _budget)

    # ── /style ──
    def _style(agent, inp, console):
        parts = inp.split(maxsplit=1)
        name = parts[1].strip() if len(parts) > 1 else ""
        if not name:
            console.print(f"[bold]Current style:[/bold] {agent.output_style}")
            available = agent.list_output_styles()
            if available:
                console.print(f"Available: {', '.join(available.keys())}")
            return
        if not hasattr(agent, "set_output_style"):
            console.print("[bold red]✗ Output styles not available[/bold red]")
            return
        if agent.set_output_style(name):
            console.print(f"[bold green]✓ Output style:[/bold green] {agent.output_style}")
        else:
            available = ", ".join(agent.list_output_styles().keys())
            console.print(f"[bold red]✗ Unknown style:[/bold red] {name}\nAvailable: {available}")

    registry.register("/style", _style)
    registry.register("/style *", _style)

    # ── /save [path] ──
    def _save(agent, inp, console):
        path = inp.split(maxsplit=1)[1].strip() if len(inp.split(maxsplit=1)) > 1 else None
        path = path or "memory/sessions/session-manual.json"
        try:
            agent.save_session(path)
            console.print(f"[bold green]✓ Session saved to:[/bold green] {path}")
        except Exception as exc:
            console.print(f"[bold red]✗ Save failed:[/bold red] {exc}")

    registry.register("/save *", _save)

    # ── /load [path] ──
    def _load(agent, inp, console):
        path = inp.split(maxsplit=1)[1].strip() if len(inp.split(maxsplit=1)) > 1 else None
        if not path:
            console.print("[bold red]✗ Usage: /load <path>[/bold red]")
            return
        try:
            agent.resume_session(path) if not path.endswith(".json") else None
            from core.session_store import load_session_snapshot
            snapshot = load_session_snapshot(path)
            agent._system_messages_override = snapshot.get("system_messages") or []
            history_items = snapshot.get("history_entries") or snapshot.get("history_messages") or []
            if snapshot.get("history_entries"):
                agent.history_manager.load_entries(history_items)
            else:
                agent.history_manager.load_messages(history_items)
            console.print(f"[bold green]✓ Session loaded from:[/bold green] {path}")
        except Exception as exc:
            console.print(f"[bold red]✗ Load failed:[/bold red] {exc}")

    registry.register("/load *", _load)

    # ── /tree ──
    def _tree(agent, inp, console):
        parts = inp.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else None
        tree = agent.history_manager.get_tree()
        console.print(tree)

    registry.register("/tree", _tree)
    registry.register("/tree *", _tree)

    # ── /fork <id> ──
    def _fork(agent, inp, console):
        target_id = inp.split(maxsplit=1)[1].strip()
        try:
            agent.history_manager.fork(target_id)
            console.print(f"[bold green]✓ Forked to {target_id}[/bold green]")
        except ValueError as e:
            console.print(f"[bold red]✗ {e}[/bold red]")

    registry.register("/fork *", _fork)

    # ── /thinking ──
    def _thinking(agent, inp, console):
        parts = inp.split(maxsplit=1)
        if len(parts) == 1 or not parts[1].strip():
            level = agent.history_manager.get_thinking_level()
            console.print(f"[bold]Thinking level:[/bold] {level}")
            return
        level = parts[1].strip().lower()
        if level in ("on", "off"):
            agent.history_manager.append_thinking_change(level)
            console.print(f"[bold green]✓ Thinking:[/bold green] {level}")
        else:
            console.print("[bold red]✗ Use /thinking on or /thinking off[/bold red]")

    registry.register("/thinking", _thinking)
    registry.register("/thinking *", _thinking)

    # ── /team msg ──
    def _team_msg(agent, inp, console):
        args = inp[len("/team msg "):].strip()
        if not args:
            console.print("[bold red]✗ Usage: /team msg <team_name> <recipient> <message>[/bold red]")
            return
        parts = args.split(maxsplit=2)
        if len(parts) < 3:
            console.print("[bold red]✗ Usage: /team msg <team_name> <recipient> <message>[/bold red]")
            return
        team_name, recipient, message = parts
        try:
            agent.team_manager.send_message(team_name, recipient, message, message_type="message")
            console.print(f"[bold green]✓ Message sent to {recipient} in {team_name}[/bold green]")
        except Exception as exc:
            console.print(f"[bold red]✗ {exc}[/bold red]")

    registry.register("/team msg *", _team_msg)

    # ── /team watch ──
    def _team_watch(agent, inp, console):
        team_name = inp[len("/team watch "):].strip() if inp.startswith("/team watch ") else ""
        if not team_name:
            console.print("[bold red]✗ Usage: /team watch <team_name>[/bold red]")
            return
        try:
            status = agent.team_manager.get_team_status(team_name)
            console.print(f"[bold]Team {team_name}:[/bold]")
            for k, v in status.items():
                console.print(f"  {k}: {v}")
        except Exception as exc:
            console.print(f"[bold red]✗ {exc}[/bold red]")

    registry.register("/team watch *", _team_watch)

    # ── /delegate ──
    def _delegate(agent, inp, console):
        arg = inp.split(maxsplit=1)[1].strip() if len(inp.split(maxsplit=1)) > 1 else ""
        if arg.lower() in ("on", "true", "yes", "1"):
            agent.set_delegate_mode(True)
            console.print("[bold green]✓ Delegate mode ON (team tools only)[/bold green]")
        elif arg.lower() in ("off", "false", "no", "0"):
            agent.set_delegate_mode(False)
            console.print("[bold green]✓ Delegate mode OFF[/bold green]")
        else:
            console.print(f"[bold]Delegate mode:[/bold] {'ON' if agent.delegate_mode else 'OFF'}")

    registry.register("/delegate", _delegate)
    registry.register("/delegate *", _delegate)

    # ── /help ──
    def _help(agent, inp, console):
        console.print(Panel(
            "[bold]Available Commands:[/bold]\n"
            "/model - Show current model\n"
            "/model <id> - Switch model (e.g. /model gpt-4o)\n"
            "/info - Show detailed token usage\n"
            "/plan - Toggle plan mode (read-only analysis)\n"
            "/sessions - List all saved sessions\n"
            "/resume <id|index> - Switch to another session\n"
            "/rename <title> - Rename current session\n"
            "/budget [amount|none] - Show/set/clear token budget\n"
            "/style [name] - Show or set output style\n"
            "/tree - Show session tree\n"
            "/fork <msg-id> - Fork to a message (new branch)\n"
            "/thinking [on|off] - Show or toggle thinking level\n"
            "/save [path] - Save session snapshot\n"
            "/load [path] - Load session snapshot\n"
            "/team msg <team> <recipient> <message>\n"
            "/team watch <team>\n"
            "/delegate [on|off] - Toggle delegate mode\n"
            "/help - Show this help\n"
            "exit, quit, q - Exit the chat\n"
            "init - Generate code_law.md",
            title="Help",
            border_style="cyan"
        ))

    registry.register("/help", _help)
