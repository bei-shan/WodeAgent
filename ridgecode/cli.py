"""Canonical, local-first Ridge Code command-line entrypoint.

All subprocesses use argument arrays and fixed repository-relative working
directories. The local Web API never binds a public interface by accident.
Hosted HTTP metadata and hosted worker infrastructure remain separate and
are not exposed through this local CLI.
"""
from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]


def _local_server(args: argparse.Namespace) -> int:
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        print(
            "Local trusted Agent API must bind to loopback. "
            "Public/multi-tenant Agent serving is not supported.",
            file=sys.stderr,
        )
        return 2
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ImportError:
        print("Install requirements.txt before starting the API.", file=sys.stderr)
        return 2
    uvicorn.run(
        "desktop.service.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


def _ui(binary: str, directory: str, arguments: list[str]) -> int:
    # Launch the path ``which`` validated: Windows resolves npm to npm.CMD,
    # which CreateProcess cannot find from the bare name.
    resolved = shutil.which(binary)
    if resolved is None:
        print(f"Missing {binary}. Install dependencies before starting this UI.", file=sys.stderr)
        return 2
    try:
        return subprocess.run([resolved, *arguments], cwd=ROOT / directory, check=False).returncode
    except OSError:
        print(f"Could not launch {binary}.", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ridgecode",
        description="Ridge Code local development commands (not a public hosted Agent API)",
    )
    cmds = parser.add_subparsers(dest="command", required=True)

    setup = cmds.add_parser("setup", help="Interactive model/provider configuration")
    setup.add_argument("--force", action="store_true", help="Explicitly replace an existing .env")

    doctor = cmds.add_parser("doctor", help="Check local dependencies without network calls")
    doctor.add_argument("--target", choices=("core", "web", "tui", "all"), default="core")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable report")

    serve = cmds.add_parser("serve", help="Start the trusted local-only API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    cmds.add_parser("web", help="Start the local Vite Web client (requires npm ci)")
    cmds.add_parser("tui", help="Start the OpenTUI client (requires bun install)")
    acp = cmds.add_parser("acp", help="Run the Agent Client Protocol server over stdio")
    acp.add_argument("--cwd", default=".", help="Default workspace root for new sessions")

    maintain = cmds.add_parser("maintain", help="Audit and repair the local session store")
    maintain.add_argument("--sessions-dir", default="memory/sessions")
    maintain.add_argument("--dry-run", action="store_true", help="Report only; change nothing")
    maintain.add_argument("--prune-empty", action="store_true", help="Delete sessions with no messages")
    maintain.add_argument("--json", action="store_true")

    init = cmds.add_parser("init", help="Generate AGENTS.md from the repository layout")
    init.add_argument("--dir", default=".")
    init.add_argument("--force", action="store_true", help="Replace an existing AGENTS.md")
    init.add_argument("--stdout", action="store_true", help="Print the file instead of writing it")

    evaluate = cmds.add_parser("eval", help="Run offline scripted Agent evaluation")
    evaluate.add_argument("--json", action="store_true")
    evaluate.add_argument("--case", action="append")

    soak = cmds.add_parser("soak", help="Run offline resource soak (no paid provider)")
    soak.add_argument("--duration-seconds", type=float, default=1800)
    soak.add_argument("--iterations", type=int, default=0)
    soak.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            parser.error("Port must be between 1 and 65535")
        return _local_server(args)
    if args.command == "setup":
        # Never overwrite an existing local secret store without explicit opt-in.
        if Path(".env").exists() and not args.force:
            print("An .env file already exists; use setup --force to replace it.",
                  file=sys.stderr)
            return 2
        from scripts.first_run_wizard import run_wizard
        return 0 if run_wizard() else 1
    if args.command == "doctor":
        from scripts.v01_preflight import main as preflight
        options = ["--target", args.target]
        if args.json:
            options.append("--json")
        return preflight(options)
    if args.command == "web":
        return _ui("npm", "desktop/web", ["run", "dev"])
    if args.command == "tui":
        return _ui("bun", "tui", ["run", "dev"])
    if args.command == "acp":
        from ridgecode.acp import main as acp_main
        return acp_main(["--cwd", args.cwd])
    if args.command == "maintain":
        from ridgecode.maintenance import main as maintain_main
        options = ["--sessions-dir", args.sessions_dir]
        if args.dry_run:
            options.append("--dry-run")
        if args.prune_empty:
            options.append("--prune-empty")
        if args.json:
            options.append("--json")
        return maintain_main(options)
    if args.command == "init":
        from core.instructions_generator import render_agents_md, scan_project, write_agents_md

        root = Path(args.dir).resolve()
        if not root.is_dir():
            print(f"Not a directory: {root}", file=sys.stderr)
            return 2
        content = render_agents_md(scan_project(root))
        if args.stdout:
            print(content, end="")
            return 0
        try:
            path = write_agents_md(root, content, force=args.force)
        except FileExistsError:
            print(
                "AGENTS.md already exists; pass --force to replace it (or --stdout to preview).",
                file=sys.stderr,
            )
            return 1
        print(f"Wrote {path} ({len(content)} chars)")
        return 0
    if args.command == "eval":
        from scripts.agent_eval import main as evaluate
        options = ["--json"] if args.json else []
        for case in args.case or []:
            options.extend(("--case", case))
        return evaluate(options)
    if args.command == "soak":
        from scripts.agent_soak import main as soak_main
        options = ["--duration-seconds", str(args.duration_seconds),
                   "--iterations", str(args.iterations)]
        if args.json:
            options.append("--json")
        return soak_main(options)
    parser.error("Unsupported command")
    return 2
