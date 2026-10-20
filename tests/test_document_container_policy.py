"""Command-argument and failure-path policy checks for isolated documents."""
from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from core.runtime.attachment_context import _isolated_document_text
from core.runtime.document_container import convert_document_container


def test_docker_parser_never_mounts_host_or_uses_network(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    commands = []

    def fake_run(argv, **kwargs):
        commands.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"hello document", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert convert_document_container(".pdf", b"%PDF-1.4") == "hello document"
    argv, kwargs = commands[0]
    assert argv[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--pull=never" in argv
    assert "--user=65534:65534" in argv
    assert "--memory=384m" in argv
    assert "--pids-limit=24" in argv
    assert "--memory-swap=384m" in argv
    assert "--tmpfs=/tmp:rw,nosuid,nodev,size=16m" in argv
    assert not any("type=bind" in value or "source=" in value for value in argv)
    assert kwargs["input"] == b"%PDF-1.4"
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert commands[-1][0][:3] == ["/usr/bin/docker", "rm", "--force"]


def test_isolation_required_fail_closed_when_no_docker(monkeypatch):
    monkeypatch.setenv("RIDGE_DOCUMENT_CONVERTER_MODE", "docker")
    monkeypatch.setattr("shutil.which", lambda _: None)
    spawn = Mock(side_effect=AssertionError("host parser must never run"))
    monkeypatch.setattr(subprocess, "run", spawn)
    with pytest.raises(ValueError, match="unavailable"):
        _isolated_document_text(".docx", b"PK")
    spawn.assert_not_called()


@pytest.mark.parametrize("strict_key", ["RIDGE_DOCUMENT_REQUIRE_SANDBOX", "RIDGE_DEPLOYMENT_MODE"])
def test_host_document_parser_forbidden_by_production_policy(monkeypatch, strict_key):
    monkeypatch.setenv("RIDGE_DOCUMENT_CONVERTER_MODE", "local")
    monkeypatch.setenv(strict_key, "hosted" if strict_key == "RIDGE_DEPLOYMENT_MODE" else "true")
    spawn = Mock(side_effect=AssertionError("host parser invoked"))
    monkeypatch.setattr(subprocess, "run", spawn)
    with pytest.raises(ValueError, match="required"):
        _isolated_document_text(".pdf", b"%PDF-1.4")
    spawn.assert_not_called()


def test_docker_parser_timeout_reaps_daemon_container(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/docker")
    commands = []

    def fake_run(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, 8)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="timed out"):
        convert_document_container(".pdf", b"%PDF-1.4")
    assert len(commands) == 2
    assert commands[1][1:3] == ["rm", "--force"]


def test_docker_image_argument_and_large_output_fail_closed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/docker")
    with pytest.raises(ValueError, match="Invalid document container image"):
        convert_document_container(".pdf", b"%PDF-1.4", image="--privileged")
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b"x" * 200000, b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="output exceeded"):
        convert_document_container(".pdf", b"%PDF-1.4")


def test_production_policy_dispatches_docker_without_host_fallback(monkeypatch):
    monkeypatch.setenv("RIDGE_DOCUMENT_CONVERTER_MODE", "docker")
    monkeypatch.setenv("RIDGE_DOCUMENT_REQUIRE_SANDBOX", "1")
    called = []
    def fake_docker(suffix, data):
        called.append((suffix, data))
        return "Extracted in sandbox"
    monkeypatch.setattr(
        "core.runtime.document_container.convert_document_container", fake_docker
    )
    assert _isolated_document_text(".docx", b"PK") == "Extracted in sandbox"
    assert called == [(".docx", b"PK")]
