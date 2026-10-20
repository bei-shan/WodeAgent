"""Short-lived document conversion child. Never serve requests directly."""
from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {".pdf", ".docx"}:
        return 2
    try:
        # On Linux, cap CPU and address space in addition to parent deadline.
        if sys.platform.startswith("linux"):
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (6, 7))
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        from core.runtime.attachment_context import (
            MAX_DOCUMENT_BYTES, _extract_docx, _extract_pdf,
        )
        data = sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            return 3
        text = _extract_pdf(data) if sys.argv[1] == ".pdf" else _extract_docx(data)
        sys.stdout.buffer.write(text.encode("utf-8")[:160_000])
        return 0
    except Exception:
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
