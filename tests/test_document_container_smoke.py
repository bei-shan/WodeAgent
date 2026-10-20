"""Real Docker document-conversion acceptance (run only on dedicated CI)."""
from __future__ import annotations

import io
import os
import subprocess
import zipfile

import pytest

from core.runtime.attachment_context import make_attachment_part

pytestmark = pytest.mark.skipif(
    os.getenv("RIDGE_DOCUMENT_DOCKER_SMOKE") != "1",
    reason="Requires prebuilt ridge-document-converter Docker image",
)


def _docx(text: str) -> bytes:
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>" + text + "</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_real_docker_docx_and_pdf_roundtrip():
    parsed = make_attachment_part("summary.docx", _docx("real isolated text"))
    assert parsed["kind"] == "document"
    assert "real isolated text" in parsed["content"]["text"]

    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    parsed_pdf = make_attachment_part("blank.pdf", buffer.getvalue())
    assert parsed_pdf["kind"] == "document"
    assert parsed_pdf["content"] is None


def test_real_docker_rejects_hostile_zip_and_invalid_pdf():
    with pytest.raises(ValueError, match="rejected or failed"):
        make_attachment_part("bomb.docx", _docx("x" * 2_100_000))
    with pytest.raises(ValueError, match="rejected or failed"):
        make_attachment_part("broken.pdf", b"not a PDF")
    containers = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=ridge-doc-",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert containers.stdout.strip() == "", containers.stdout
