"""No-network, no-host-mount container conversion of user PDF and DOCX bytes.

The input is streamed over stdin, so no workspace path or API credential is
available to the guest. Docker image must be prebuilt and preloaded. A command
failure, missing daemon or unavailable image NEVER triggers a host fallback.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess

MAX_DOCUMENT_BYTES = 3 * 1024 * 1024
MAX_DOCUMENT_OUTPUT_BYTES = 160_000
DEFAULT_IMAGE = "ridge-document-converter:local"


def convert_document_container(
    suffix: str, payload: bytes, *, timeout_seconds: float = 8,
    image: str | None = None,
) -> str:
    if suffix not in {".pdf", ".docx"}:
        raise ValueError("Unsupported document conversion type")
    if not isinstance(payload, bytes) or len(payload) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds 3 MiB extraction cap")
    configured_image = image or os.getenv("RIDGE_DOCUMENT_IMAGE", DEFAULT_IMAGE)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,199}", configured_image)
            or ".." in configured_image):
        raise ValueError("Invalid document container image")
    docker = shutil.which("docker")
    if not docker:
        raise ValueError("Isolated document converter unavailable")
    name = "ridge-doc-" + secrets.token_hex(12)
    command = [
        docker, "run", "--rm", "-i", "--pull=never", "--name", name,
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=24",
        "--memory=384m", "--memory-swap=384m", "--cpus=0.75",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=16m",
        "--user=65534:65534", "--workdir=/app",
        "--entrypoint=python", configured_image,
        "-m", "core.runtime.document_worker", suffix,
    ]
    cleanup_required = False
    try:
        cleanup_required = True
        result = subprocess.run(
            command, input=payload, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout_seconds, check=False,
        )
        if result.returncode != 0:
            raise ValueError("Isolated document conversion rejected or failed")
        if len(result.stdout) > MAX_DOCUMENT_OUTPUT_BYTES:
            raise ValueError("Isolated document converter output exceeded limit")
        return result.stdout.decode("utf-8", errors="replace")[:40_000]
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Isolated document conversion timed out") from exc
    except OSError as exc:
        raise ValueError("Isolated document converter unavailable") from exc
    finally:
        if cleanup_required:
            # Docker --rm handles the common successful case, but if the
            # CLI is killed on timeout the daemon's container must be reaped.
            try:
                subprocess.run(
                    [docker, "rm", "--force", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                # Cleanup is best effort; no host parsing follows failure.
                pass
