"""TUI module — Claude Code-style terminal user interface.

Components:
- StreamingResponse — Rich Live-based real-time LLM output
- MentionCompleter — @-mention autocompletion for agents/models/files
- PermissionDialog — interactive permission prompts via prompt_toolkit
- StatusLine — model indicator in prompt line
"""

from .streaming import StreamingResponse
from .mention_completer import MentionCompleter
from .permission_dialog import PermissionDialog
from .status_line import StatusLine

__all__ = [
    "StreamingResponse",
    "MentionCompleter",
    "PermissionDialog",
    "StatusLine",
]
