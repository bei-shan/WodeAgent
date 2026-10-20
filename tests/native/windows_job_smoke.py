"""Live Windows Job Object acceptance, executed on windows-latest CI.

A child creates a grandchild that would otherwise outlive its parent. A
timeout must kill the entire process tree, not only cmd.exe/python parent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from core.runtime.process_execution import run_process


def main():
    if os.name != "nt":
        raise SystemExit("This test needs an interactive Windows runner")
    with tempfile.TemporaryDirectory(prefix="ridge-win-job-") as folder:
        marker = Path(folder) / "grandchild_started.txt"
        # The child creates a detached-looking grandchild, but inherits the
        # Job from its parent. The grandchild writes a marker only if it
        # survives beyond the parent timeout.
        child = (
            "import subprocess,sys;"
            "code='import time,pathlib; time.sleep(2); "
            f"pathlib.Path({str(marker)!r}).write_text(\\\"survived\\\")';"
            "subprocess.Popen([sys.executable,'-c',code]);"
            "import time;time.sleep(20)"
        )
        result = run_process(
            [sys.executable, "-c", child], shell=False,
            cwd=folder, env=os.environ.copy(), timeout=0.7,
            poll_interval=0.05, max_output_bytes=1024,
        )
        assert result.timed_out, result
        time.sleep(2.5)
        assert not marker.exists(), "Windows Job leaked a grandchild process"
    print("PASS Windows Job Object tree teardown")


if __name__ == "__main__":
    main()
