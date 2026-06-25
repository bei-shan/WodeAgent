"""Shared constants used across multiple modules."""

# Directories always skipped by Glob/Grep/LS (reduce noise)
ALWAYS_IGNORE_DIRS: frozenset[str] = frozenset({
    ".git",          # Git version control
    ".hg",           # Mercurial VCS
    ".svn",          # Subversion VCS
    "__pycache__",   # Python bytecode cache
    "node_modules",  # Node.js dependencies
    ".venv",         # Python virtual environment
    "venv",          # Python virtual environment
    ".idea",         # JetBrains IDE
    ".vscode",       # VS Code
    ".DS_Store",     # macOS
    ".mypy_cache",   # mypy type checker
    ".pytest_cache", # pytest
    ".ruff_cache",   # ruff linter
    ".tox",          # tox test env
    ".cache",        # generic cache
    "dist",          # build dist
    "build",         # build output
    "target",        # Rust/Cargo target dir
    "site-packages", # Python packages
})
