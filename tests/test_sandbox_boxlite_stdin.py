"""BoxLite native stdin bridge is opt-in, bounded, loop-affine and fail-closed."""
from __future__ import annotations

import asyncio
import base64
import json
import threading
from types import SimpleNamespace

import pytest

from core.sandbox.boxlite_provider import BoxliteCommandProvider
from core.sandbox.contracts import SandboxUnavailable
from core.sandbox.guest_files import GuestFilesystem


class NativeStdin:
    written = []
    closed = 0

    async def send_input(self, data):
        assert isinstance(data, bytes)
        self.written.append(data)

    async def close(self):
        self.closed += 1


class NativeExecution:
    started = []
    def __init__(self):
        self.writer = NativeStdin()

    def stdin(self):
        return self.writer

    def stdout(self):
        async def chunks():
            yield json.dumps({"ok": True, "written": 4})
        return chunks()

    def stderr(self):
        async def chunks():
            if False:
                yield "never"
        return chunks()

    async def wait(self):
        return SimpleNamespace(exit_code=0)


class NativeBox:
    args = []
    threads = []

    async def exec(self, *args, **kwargs):
        self.args.append((args, kwargs))
        self.threads.append(threading.get_ident())
        return NativeExecution()


class Box:
    created = 0
    stopped = 0
    def __init__(self, **options):
        type(self).created += 1
        self._box = NativeBox()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        type(self).stopped += 1


@pytest.fixture(autouse=True)
def clear():
    Box.created = Box.stopped = 0
    NativeStdin.written.clear()
    NativeStdin.closed = 0
    NativeBox.args.clear()
    NativeBox.threads.clear()


def provider(tmp_path, enabled=True):
    return BoxliteCommandProvider(
        tmp_path, box_factory=Box, network_factory=dict,
        guest_stdin_enabled=enabled,
    )


def test_vm_native_stream_transfers_bytes_not_command_or_env(tmp_path):
    p = provider(tmp_path)
    try:
        contents = b"a\x00\xff\n"
        assert GuestFilesystem(p).write_file("a.bin", contents) == len(contents)
    finally:
        p.close()
    assert NativeStdin.written == [contents]
    assert NativeStdin.closed == 1
    assert Box.created == Box.stopped == 1
    args, kwargs = NativeBox.args[0]
    joined = repr((args, kwargs))
    assert base64.b64encode(contents).decode() not in joined
    assert repr(contents) not in joined
    assert args[0] == "sh"
    assert args[1][0] == "-lc"
    assert kwargs["cwd"] == "/workspace"


def test_vm_write_disabled_by_default_never_starts_native_box(tmp_path):
    p = provider(tmp_path, enabled=False)
    try:
        with pytest.raises(SandboxUnavailable, match="enabled"):
            GuestFilesystem(p).write_file("sensitive.txt", b"data")
    finally:
        p.close()
    assert Box.created == 0


def test_vm_stream_works_with_warm_pool_same_sdk_loop(tmp_path):
    p = provider(tmp_path)
    p.enable_warm_pool(session_scope="isolated-session", idle_timeout=60)
    try:
        f = GuestFilesystem(p)
        assert f.write_file("one.bin", b"ABCD") == 4
        assert f.write_file("two.bin", b"WXYZ") == 4
        assert Box.created == 1
        assert len(set(NativeBox.threads)) == 1
        assert NativeStdin.written == [b"ABCD", b"WXYZ"]
    finally:
        p.close()
    assert Box.stopped == 1


def test_vm_missing_low_level_execution_fails_closed(tmp_path):
    class NoNative:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    p = BoxliteCommandProvider(
        tmp_path, box_factory=lambda **kw: NoNative(),
        network_factory=dict, guest_stdin_enabled=True,
    )
    with pytest.raises(SandboxUnavailable, match="host fallback forbidden"):
        p.execute_stdin("cat", b"private")


def test_vm_stream_timeout_cleans_up_ephemeral_guest(tmp_path):
    class SlowNative(NativeBox):
        async def exec(self, *args, **kwargs):
            await asyncio.sleep(60)
    class SlowBox(Box):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._box = SlowNative()
    p = BoxliteCommandProvider(
        tmp_path, box_factory=SlowBox, network_factory=dict,
        guest_stdin_enabled=True,
    )
    result = p.execute_stdin("cat", b"private", timeout_seconds=0.2)
    assert result.timed_out
    assert SlowBox.stopped == 1


def test_vm_stream_cancellation_ends_promptly(tmp_path):
    class SlowNative(NativeBox):
        started = threading.Event()
        async def exec(self, *args, **kwargs):
            self.started.set()
            await asyncio.sleep(60)
    class SlowBox(Box):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._box = SlowNative()
    p = BoxliteCommandProvider(
        tmp_path, box_factory=SlowBox, network_factory=dict,
        guest_stdin_enabled=True,
    )
    cancel = threading.Event()
    result = []
    worker = threading.Thread(
        target=lambda: result.append(p.execute_stdin(
            "cat", b"private", timeout_seconds=10, cancel_event=cancel,
        )),
    )
    worker.start()
    assert SlowNative.started.wait(2)
    cancel.set()
    worker.join(4)
    assert not worker.is_alive()
    assert result[0].interrupted
    assert SlowBox.stopped == 1


def test_vm_stream_rejects_large_payload_and_bad_type(tmp_path):
    p = provider(tmp_path)
    with pytest.raises(ValueError):
        p.execute_stdin("cat", b"x" * 65537)
    with pytest.raises(ValueError):
        p.execute_stdin("cat", "str-not-bytes")
    assert Box.created == 0
