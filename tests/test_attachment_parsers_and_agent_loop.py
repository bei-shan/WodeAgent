"""Media adapters and multi-step Agent vision context without external LLMs."""
from __future__ import annotations

import io
import zipfile
from unittest.mock import Mock

import pytest

from core.runtime.attachment_context import make_attachment_part
from core.runtime.agent_loop import AgentLoop
from tests.test_agent_loop_ports import Host, Trace, result


def _docx(xml_text: str, *, declared_size: int | None = None) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml_text)
    return data.getvalue()


def test_docx_bounded_text_extraction_and_untrusted_boundaries():
    document = _docx(
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>Summary of income</w:t></w:r></w:p>'
        '</w:body></w:document>'
    )
    part = make_attachment_part("summary.docx", document)
    assert part["kind"] == "document"
    text = part["content"]["text"]
    assert "Summary of income" in text
    assert "BEGIN UNTRUSTED ATTACHMENT" in text


def test_docx_zip_bomb_declaration_rejected():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            '<root>' + ("x" * (2 * 1024 * 1024 + 20)) + "</root>",
        )
    with pytest.raises(ValueError, match="rejected or failed"):
        make_attachment_part("unsafe.docx", data.getvalue())


def test_pdf_encrypted_and_invalid_inputs_rejected():
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    w.encrypt("secret")
    data = io.BytesIO()
    w.write(data)
    with pytest.raises(ValueError, match="rejected or failed"):
        make_attachment_part("locked.pdf", data.getvalue())
    with pytest.raises(ValueError, match="rejected or failed"):
        make_attachment_part("broken.pdf", b"fake not a PDF")


def test_tampered_image_header_is_not_forwarded():
    with pytest.raises(ValueError, match="Invalid|corrupted"):
        make_attachment_part("image.png", b"\x89PNG\r\n\x1a\nforged")


def test_attached_images_survive_intermediate_tool_steps_without_history_mutation():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (2, 2)).save(out, format="PNG")
    media = make_attachment_part("image.png", out.getvalue())
    host = Host([result("thinking", [{"name": "Read"}]), result("done")])
    host._turn_attachment_parts = [media]
    seen = []

    def invoke(messages, base, *args):
        assert messages[-1]["role"] == "user"
        parts = messages[-1]["content"]
        assert parts[-1]["type"] == "image_url"
        assert base[-1]["content"][-1]["type"] == "image_url"
        seen.append(len(parts))
        return next(host.results)

    host._invoke_llm_with_retry = invoke
    assert AgentLoop(host).run("describe", False, Trace()) == "done"
    assert seen == [2, 2]


def test_document_worker_timeout_is_rejected(monkeypatch):
    import subprocess
    from core.runtime.attachment_context import _isolated_document_text

    def stall(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 8)

    monkeypatch.setattr(subprocess, "run", stall)
    with pytest.raises(ValueError, match="timed out"):
        _isolated_document_text(".pdf", b"%PDF-1.4")


def test_document_worker_failure_does_not_expose_stderr(monkeypatch):
    import subprocess
    from core.runtime.attachment_context import _isolated_document_text

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 4, b"", b"private"))
    with pytest.raises(ValueError, match="rejected or failed"):
        _isolated_document_text(".docx", b"PK")


def test_document_worker_rejects_oversize_before_launch(monkeypatch):
    import subprocess
    from core.runtime.attachment_context import _isolated_document_text
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(True))
    with pytest.raises(ValueError, match="3 MiB"):
        _isolated_document_text(".pdf", b"x" * (3 * 1024 * 1024 + 1))
    assert not called
