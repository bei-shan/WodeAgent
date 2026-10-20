"""Ridge Code v0.1 local release preflight (offline and side-effect free).

This does NOT contact providers, Docker daemons, Langfuse, or remote servers.
It checks local runtime prerequisites; release acceptance remains separate.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
_PYTHON_PACKAGES = ("openai", "pydantic", "mcp", "fastapi", "uvicorn", "jsonschema")
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class Check:
    id: str
    ok: bool
    detail: str


def _version(command: str) -> tuple[int, ...] | None:
    binary = shutil.which(command)
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _VERSION_RE.search(result.stdout or result.stderr)
    return tuple(int(part or 0) for part in match.groups()) if match else None


def collect(
    target: str = "core", *,
    python_version: tuple[int, ...] | None = None,
    has_package: Callable[[str], bool] | None = None,
    get_version: Callable[[str], tuple[int, ...] | None] | None = None,
    root: Path = ROOT,
) -> list[Check]:
    """Check only local requirements. Dependency injection makes tests offline."""
    if target not in {"core", "web", "tui", "all"}:
        raise ValueError("Unknown preflight target")
    has_package = has_package or (lambda name: importlib.util.find_spec(name) is not None)
    get_version = get_version or _version
    py = python_version if python_version is not None else sys.version_info[:3]

    checks: list[Check] = [
        Check("python", py >= (3, 12), "Python 3.12+ required"),
        Check("repository", (root / "requirements.txt").is_file(), "Root requirements.txt is present"),
    ]
    for name in _PYTHON_PACKAGES:
        checks.append(Check(f"python:{name}", bool(has_package(name)),
                            f"Required Python package: {name}"))

    if target in {"web", "all"}:
        node = get_version("node")
        checks.extend((
            Check("node", node is not None and node >= (20, 0), "Node.js 20+ required for Web"),
            Check("npm", get_version("npm") is not None, "npm required for Web"),
            Check("web-lockfile", (root / "desktop/web/package-lock.json").is_file(),
                  "Web package-lock.json is present"),
        ))

    if target in {"tui", "all"}:
        bun = get_version("bun")
        checks.extend((
            Check("bun", bun is not None and bun >= (1, 4), "Bun 1.4+ required for OpenTUI"),
            Check("tui-lockfile", any((root / "tui" / name).is_file()
                                     for name in ("bun.lock", "bun.lockb")),
                  "OpenTUI Bun dependency lockfile is present"),
        ))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("core", "web", "tui", "all"), default="core")
    parser.add_argument("--json", action="store_true", help="Emit CI-friendly JSON")
    args = parser.parse_args(argv)
    checks = collect(args.target)
    ok = all(item.ok for item in checks)
    if args.json:
        print(json.dumps({"target": args.target, "ok": ok,
                          "checks": [asdict(item) for item in checks]}, ensure_ascii=False))
    else:
        print(f"Ridge Code v0.1 preflight: {args.target}")
        for item in checks:
            print(f"  {'PASS' if item.ok else 'FAIL'} {item.id}: {item.detail}")
        print("Local prerequisites passed." if ok else "Missing required local prerequisites.")
        print("This preflight does not validate live provider, MCP or Langfuse connectivity.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
