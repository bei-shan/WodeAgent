"""Safe, reusable file commit primitive for Write/Edit/MultiEdit.

This is a collision-free atomic *file publication* helper, not an OS sandbox:
the caller must have already resolved and authorized the workspace path.
"""
from __future__ import annotations

import os
import stat
import tempfile
import secrets
from contextlib import contextmanager
from pathlib import Path


class ConcurrentFileChange(OSError):
    """The workspace target changed while a tool prepared its update."""


def _identity(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev, item.st_ino, item.st_size,
        item.st_mtime_ns, item.st_ctime_ns,
    )


_ANCHOR_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.replace in os.supports_dir_fd
    and os.link in os.supports_dir_fd
)


@contextmanager
def _anchored_parent(workspace_root: Path, file_path: Path):
    """Walk each directory component relative to a pinned workspace fd."""
    relative = file_path.relative_to(workspace_root)
    if not relative.parts or any(p in {"", ".", ".."} for p in relative.parts):
        raise ConcurrentFileChange("Invalid workspace file path")
    if not _ANCHOR_SUPPORTED:
        raise ConcurrentFileChange("Descriptor-anchored writes unsupported")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(workspace_root, flags)
    try:
        for name in relative.parts[:-1]:
            next_fd = os.open(name, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        yield current, relative.parts[-1]
    finally:
        os.close(current)


def _commit_anchored(
    root: Path, path: Path, content: str, expected_stat: os.stat_result | None,
) -> int:
    """POSIX root-fd-bound commit with cooperative parent-directory locking."""
    import fcntl

    with _anchored_parent(root, path) as (parent_fd, filename):
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        temporary = f".{filename}.{secrets.token_hex(12)}.ridge-write"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        published = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                latest = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                latest = None
            if expected_stat is None:
                if latest is not None:
                    raise ConcurrentFileChange("Target created concurrently")
                os.link(temporary, filename, src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd, follow_symlinks=False)
            else:
                if latest is None or not stat.S_ISREG(latest.st_mode):
                    raise ConcurrentFileChange("Target was deleted or changed")
                if _identity(latest) != _identity(expected_stat):
                    raise ConcurrentFileChange("Target changed since it was read")
                temp_fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW,
                                  dir_fd=parent_fd)
                try:
                    os.fchmod(temp_fd, stat.S_IMODE(latest.st_mode))
                finally:
                    os.close(temp_fd)
                os.replace(temporary, filename, src_dir_fd=parent_fd,
                           dst_dir_fd=parent_fd)
            published = True
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            return len(content.encode("utf-8"))
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            fcntl.flock(parent_fd, fcntl.LOCK_UN)


@contextmanager
def open_regular_workspace_file(
    path: Path, workspace_root: Path, *, text: bool = False,
    errors: str = "strict",
):
    """Open regular workspace files anchored to root, ignoring symlink swaps.

    Explicitly approved external paths continue to use regular file I/O.
    Avoid using Path.resolve() between authorization and os.open.
    """
    path, root = Path(path), Path(workspace_root)
    try:
        path.relative_to(root)
    except ValueError:
        # External paths are separately guarded by PermissionGate. This is
        # legacy trusted-local compatibility, not a sandbox security fence.
        with open(path, "r" if text else "rb",
                  **({"encoding": "utf-8", "errors": errors} if text else {})) as f:
            yield f
        return
    if not _ANCHOR_SUPPORTED:
        raise ConcurrentFileChange("Secure workspace reads unavailable on this host")
    with _anchored_parent(root, path) as (parent_fd, filename):
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ConcurrentFileChange("Target is not a regular workspace file")
            with os.fdopen(fd, "r" if text else "rb", closefd=False,
                           **({"encoding": "utf-8", "errors": errors} if text else {})) as f:
                yield f
        finally:
            os.close(fd)


def read_workspace_bytes(
    path: Path, workspace_root: Path, *, limit_bytes: int = 16 * 1024 * 1024,
) -> bytes:
    """Read at most one bounded regular file through the anchored filesystem."""
    with open_regular_workspace_file(path, workspace_root) as source:
        file_stat = os.fstat(source.fileno())
        if file_stat.st_size > limit_bytes:
            raise ValueError("File exceeds 16 MiB edit limit")
        payload = source.read(limit_bytes + 1)
        if len(payload) > limit_bytes:
            raise ValueError("File grew past edit limit while reading")
        return payload


def commit_utf8(
    path: Path, content: str, *, expected_stat: os.stat_result | None,
    workspace_root: Path | None = None,
) -> int:
    """Publish UTF-8 text without partial contents or race-clobber on create.

    Existing: preserve POSIX mode and compare high-resolution file identity
    immediately before replace. New: use an exclusive hard link, so a target
    created concurrently is never overwritten. Temporary file lives in the
    destination directory so the rename is on the same filesystem.

    Does not make out-of-root symlink policies race-free: these require
    dirfd-relative filesystem APIs / an isolated mounted workspace.
    """
    path = Path(path)
    # Workspaces on POSIX get an anchored fd-based path walk. No symlink
    # replacement of parent directories can send the commit out of root.
    # Explicitly approved external paths retain their separate legacy policy.
    if workspace_root is not None and _ANCHOR_SUPPORTED:
        try:
            path.relative_to(workspace_root)
        except ValueError:
            pass
        else:
            return _commit_anchored(Path(workspace_root), path, content, expected_stat)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".ridge-write", dir=str(path.parent),
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

        if expected_stat is None:
            # os.replace() on POSIX would silently overwrite a concurrently
            # created file. A same-directory hard link is create-only.
            try:
                os.link(temp, path)
            except FileExistsError as exc:
                raise ConcurrentFileChange("File appeared during creation; Read again") from exc
            temp.unlink()
        else:
            try:
                latest = path.stat()
            except FileNotFoundError as exc:
                raise ConcurrentFileChange("File disappeared before commit; Read again") from exc
            if _identity(latest) != _identity(expected_stat):
                raise ConcurrentFileChange("File changed before commit; Read again")
            if not stat.S_ISREG(latest.st_mode):
                raise ConcurrentFileChange("Target is no longer a regular file")
            # mkstemp is mode 0600; preserve the original file's permission
            # bits, including executable mode. Do not copy ownership/ACLs
            # or broad permissions from the user's umask.
            os.chmod(temp, stat.S_IMODE(latest.st_mode))
            os.replace(temp, path)
        return len(content.encode("utf-8"))
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
