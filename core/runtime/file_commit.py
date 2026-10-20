"""Safe, reusable file commit primitive for Write/Edit/MultiEdit.

This is a collision-free atomic *file publication* helper, not an OS sandbox:
the caller must have already resolved and authorized the workspace path.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


class ConcurrentFileChange(OSError):
    """The workspace target changed while a tool prepared its update."""


def _identity(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev, item.st_ino, item.st_size,
        item.st_mtime_ns, item.st_ctime_ns,
    )


def commit_utf8(
    path: Path, content: str, *, expected_stat: os.stat_result | None,
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
