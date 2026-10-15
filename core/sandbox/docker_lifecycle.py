"""Bounded, owner-authenticated Docker orphan cleanup for the single-user sandbox.

Docker labels are visible to daemon operators, so ownership is backed by a
private, persistent local HMAC key kept outside the bind-mounted workspace.
This is cleanup authorization, not isolation from a privileged Docker user.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LOG = logging.getLogger(__name__)
LABEL_PREFIX = "io.ridgecode.sandbox"
OWNER_LABEL = f"{LABEL_PREFIX}.owner"
VERSION_LABEL = f"{LABEL_PREFIX}.version"
EXPIRES_LABEL = f"{LABEL_PREFIX}.expires"
SIGNATURE_LABEL = f"{LABEL_PREFIX}.signature"
VERSION = "1"
NAME_PATTERN = re.compile(r"wodeagent-[a-f0-9]{20}\Z")
CONTAINER_ID_PATTERN = re.compile(r"[a-f0-9]{12,64}\Z")
MAX_SCAN = 24
SCAN_BUDGET_SECONDS = 8.0
SCAN_INTERVAL_SECONDS = 60.0
LEASE_GRACE_SECONDS = 180


class SandboxLeaseError(RuntimeError):
    """Cannot securely initialize the local owner identity."""


@dataclass(frozen=True)
class ReapReport:
    inspected: int = 0
    removed: int = 0
    skipped: int = 0
    failures: int = 0


def installation_key_path() -> Path:
    """Stable per source installation; deliberately not inside the workspace."""
    install = str(Path(__file__).resolve().parents[2])
    suffix = hashlib.sha256(install.encode("utf-8")).hexdigest()[:24]
    return Path.home() / ".mycodeagent" / "sandbox-owners" / f"{suffix}.key"


def load_installation_key(path: Path) -> bytes:
    """Create a 0600 private key once, without following symlinks or overwriting."""
    if os.name != "posix":
        raise SandboxLeaseError("Owner keys require a POSIX host")
    path = Path(path).absolute()
    directory = path.parent
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        parent = directory.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise SandboxLeaseError("Sandbox owner directory is not private")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(secrets.token_bytes(32))
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise SandboxLeaseError("Sandbox owner key permissions are unsafe")
            key = handle.read(33)
        if len(key) != 32:
            raise SandboxLeaseError("Sandbox owner key is invalid")
        return key
    except SandboxLeaseError:
        raise
    except OSError as exc:
        raise SandboxLeaseError("Unable to access private sandbox owner key") from exc


class DockerLeaseManager:
    """Per-installation daemon reaper with bounded and authenticated decisions."""

    def __init__(
        self, docker_binary: str, key: bytes, *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        clock: Callable[[], float] = time.time,
    ):
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("Owner key must be 32 bytes")
        self.docker_binary = docker_binary
        self._key = key
        self.owner = hashlib.sha256(key).hexdigest()[:32]
        self._run = runner
        self._clock = clock
        self._active: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _signature(self, name: str, expires: int) -> str:
        return hmac.new(
            self._key, f"{VERSION}:{self.owner}:{name}:{expires}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def labels(self, name: str, timeout_seconds: float) -> dict[str, str]:
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("Invalid sandbox container name")
        # Docker CLI has a <=600 second timeout; the extra margin protects
        # delayed cleanup, busy runners and healthy concurrent sessions.
        expires = int(self._clock()) + int(timeout_seconds) + LEASE_GRACE_SECONDS + 1
        return {
            VERSION_LABEL: VERSION,
            OWNER_LABEL: self.owner,
            EXPIRES_LABEL: str(expires),
            SIGNATURE_LABEL: self._signature(name, expires),
        }

    def activate(self, name: str) -> None:
        with self._lock:
            self._active.add(name)

    def deactivate(self, name: str) -> None:
        with self._lock:
            self._active.discard(name)

    def _eligible(self, name: str, labels: object) -> bool:
        if not NAME_PATTERN.fullmatch(name) or not isinstance(labels, dict):
            return False
        if labels.get(VERSION_LABEL) != VERSION or labels.get(OWNER_LABEL) != self.owner:
            return False
        value = labels.get(EXPIRES_LABEL)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10,11}", value):
            return False
        expires = int(value)
        if expires > int(self._clock()):
            return False
        signature = labels.get(SIGNATURE_LABEL)
        if not isinstance(signature, str) or not hmac.compare_digest(
            signature, self._signature(name, expires)
        ):
            return False
        with self._lock:
            return name not in self._active

    def _call(self, argv: list[str], deadline: float) -> subprocess.CompletedProcess:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Sandbox reap budget exhausted")
        # Each daemon operation has its own short deadline and is never a shell.
        return self._run(
            argv, capture_output=True, text=True,
            timeout=min(2.0, remaining), check=False,
        )

    def reap(self) -> ReapReport:
        """Inspect only bounded filtered IDs; remove only signed expired leases.

        A missing daemon, unreadable container or malformed label is skipped.
        Never inspect arbitrary unlabeled containers or trust names alone.
        """
        inspected = removed = skipped = failures = 0
        deadline = time.monotonic() + SCAN_BUDGET_SECONDS
        try:
            result = self._call([
                self.docker_binary, "ps", "-a",
                "--filter", f"label={VERSION_LABEL}={VERSION}",
                "--filter", f"label={OWNER_LABEL}={self.owner}",
                "--last", str(MAX_SCAN),
                "--format", "{{.ID}}",
            ], deadline)
            if result.returncode != 0:
                failures += 1
                return ReapReport(failures=failures)
            ids = result.stdout.splitlines()[:MAX_SCAN]
            for cid in ids:
                if not CONTAINER_ID_PATTERN.fullmatch(cid):
                    skipped += 1
                    continue
                if time.monotonic() >= deadline:
                    break
                inspected += 1
                try:
                    current = self._call([
                        self.docker_binary, "inspect", "--format",
                        "{{json .Config.Labels}}|{{.Name}}", cid,
                    ], deadline)
                    if current.returncode:
                        failures += 1
                        continue
                    labels_json, sep, raw_name = current.stdout.strip().partition("|")
                    if not sep:
                        skipped += 1
                        continue
                    name = raw_name.removeprefix("/")
                    labels = json.loads(labels_json)
                    if not self._eligible(name, labels):
                        skipped += 1
                        continue
                    # Use immutable container ID, not name, to avoid removing
                    # a newly launched process after a name-race.
                    outcome = self._call(
                        [self.docker_binary, "rm", "--force", cid], deadline,
                    )
                    if outcome.returncode == 0:
                        removed += 1
                    else:
                        failures += 1
                except (OSError, subprocess.TimeoutExpired, TimeoutError, ValueError, TypeError):
                    failures += 1
        except (OSError, subprocess.TimeoutExpired, TimeoutError):
            failures += 1
        report = ReapReport(inspected, removed, skipped, failures)
        LOG.info(
            "Docker sandbox cleanup inspected=%d removed=%d skipped=%d failed=%d",
            report.inspected, report.removed, report.skipped, report.failures,
        )
        return report

    def start(self) -> None:
        """One background reconciliation worker per persistent installation."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._background, name="ridge-docker-reaper", daemon=True,
            )
            self._thread.start()

    def _background(self) -> None:
        # Startup reconciliation, then bounded periodic checks during uptime.
        while not self._stop.is_set():
            self.reap()
            if self._stop.wait(SCAN_INTERVAL_SECONDS):
                break

    def stop(self) -> None:
        self._stop.set()


_MANAGERS: dict[tuple[str, str], DockerLeaseManager] = {}
_MANAGERS_LOCK = threading.Lock()


def shared_lease_manager(docker_binary: str) -> DockerLeaseManager:
    path = installation_key_path()
    identity = (str(path), docker_binary)
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(identity)
        if manager is None:
            manager = DockerLeaseManager(docker_binary, load_installation_key(path))
            _MANAGERS[identity] = manager
            manager.start()
        return manager
