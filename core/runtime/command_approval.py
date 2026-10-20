"""Conservative Bash effect classifier. Approval is NOT sandbox confinement.

Only an extremely small set of argv-only queries can avoid explicit approval
when a broker exists. Complex shell grammar, wrappers, interpreters, writes
and unknown commands are deliberately approval-required. Never authorize
based on substrings or model-supplied effect labels.
"""
from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandDecision:
    effect: str
    approval_required: bool
    digest: str


def classify_command(command: str) -> CommandDecision:
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return CommandDecision("shell_exec", True, digest)
    if not tokens:
        return CommandDecision("shell_exec", True, digest)
    # Only minimal commands with plain argv arguments avoid an interactive
    # approval. Operators/pipelines/redirections and control tokens always ask.
    if any(token in {";", "&&", "||", "&", "|", ">", ">>", "<", "<<",
                     "(", ")", ";;", "|&", "<<<"} or any(ch in token for ch in ("\n", "\r"))
           for token in tokens):
        return CommandDecision("shell_exec", True, digest)
    first = tokens[0].lower()
    if first == "pwd" and len(tokens) == 1:
        return CommandDecision("read", False, digest)
    if first == "git" and len(tokens) >= 2:
        sub = tokens[1].lower()
        if sub in {"status", "diff", "log", "show", "rev-parse"} and all(
            not arg.startswith(("--output", "--exec", "--ext-diff", "--config-env"))
            and not arg.startswith("-c")
            and "=" not in arg
            for arg in tokens[2:]
        ):
            return CommandDecision("read", False, digest)
    if first in {"curl", "wget", "ssh", "scp", "nc", "npm", "pip"}:
        return CommandDecision("external_network", True, digest)
    if first in {"rm", "mv", "cp", "mkdir", "touch", "tee", "sed"}:
        return CommandDecision("workspace_write", True, digest)
    return CommandDecision("shell_exec", True, digest)


def authorize_bash_command(command: str, broker, *, strict: bool = False) -> bool:
    """Make one exact-command request; do not cache path grants for shell.

    A command digest is included in UI prompt to bind consent to identical
    bytes and prevent a subsequent command borrowing a previous approval.
    """
    decision = classify_command(command)
    if not decision.approval_required:
        return True
    if broker is None:
        return not strict
    try:
        summary = (
            f"[{decision.effect}] SHA256:{decision.digest[:16]} "
            f"command: {command[:1200]}"
        )
        return broker(summary, "Bash", decision.effect) == "granted"
    except Exception:
        return False
