"""Interactive permission dialogs via prompt_toolkit.

Replaces the blocking input() calls in PermissionGate.ask() with a
prompt_toolkit dialog that shows the request details clearly.
"""

from __future__ import annotations

from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style as PromptStyle


class PermissionDialog:
    """Interactive permission prompt using prompt_toolkit.

    Usage::

        dialog = PermissionDialog()
        result = dialog.ask(
            tool="Read",
            path="/etc/hosts",
            action="reading",
        )
        # Returns "granted" or "denied"
    """

    _STYLE = PromptStyle.from_dict({
        "dialog.frame": "bg:#333333 #ffffff",
        "dialog.title": "bold #ff5555",
        "dialog.path": "#ffff00",
        "dialog.info": "#aaaaaa",
        "question": "bold",
        "yes": "bg:#005500 #ffffff bold",
        "no": "bg:#550000 #ffffff bold",
    })

    def __init__(self):
        self._session: Optional[PromptSession] = None

    def _get_session(self) -> PromptSession:
        if self._session is None:
            self._session = PromptSession(style=self._STYLE)
        return self._session

    def ask(self, tool: str, path: str, action: str) -> str:
        """Show a permission prompt and return 'granted' or 'denied'.

        Parameters
        ----------
        tool:
            Tool name (e.g. 'Read', 'Write', 'Bash').
        path:
            The path being accessed.
        action:
            Human-readable action (e.g. 'reading', 'writing to', 'cd to').
        """
        session = self._get_session()
        prompt_html = (
            f"\n<dialog.frame>"
            f"  <dialog.title>🔒 权限请求</dialog.title>\n"
            f"  <dialog.info>{tool} 工具尝试{action}项目外的路径:</dialog.info>\n"
            f"  <dialog.path>{path}</dialog.path>\n"
            f"</dialog.frame>\n"
            f"<question>允许访问? [<yes>y</yes>/<no>N</no>]</question> "
        )

        try:
            answer = session.prompt(HTML(prompt_html))
        except (EOFError, KeyboardInterrupt):
            return "denied"

        if not answer or not answer.strip():
            return "denied"
        return "granted" if answer.strip().lower() in ("y", "yes") else "denied"
