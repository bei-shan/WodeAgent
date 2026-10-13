# Descriptor-anchored workspace traversal

Every service operation now opens the trusted session base, then the session
and each relative subdirectory using POSIX dir_fd + O_NOFOLLOW + O_DIRECTORY.
Files use O_NOFOLLOW with fstat regular-file validation. Uploads and partial
cleanup are relative to a held session directory descriptor. This prevents
a writable intermediate directory being swapped for a symlink between
validation and opening the final file.

Security characteristics:
- Reject absolute paths, parent components, backslashes, NUL and symlink
  components (including symlinks that point *within* the workspace).
- Never use Path.resolve()/relative_to() as the security boundary.
- Fail closed when secure POSIX dir_fd and O_NOFOLLOW are unavailable.
- Directory listing excludes symlinks and non-file devices.
- Uploads use O_EXCL; downloaded file descriptors remain tied to the opened
  inode even if paths are renamed.

Known boundary: this protects against path replacement within the session
workspace, not a hostile actor allowed to rewrite trusted service configuration
or ancestor directories of data_dir itself. Windows support requires a
separate secure OS-specific backend before exposing the file endpoints.
