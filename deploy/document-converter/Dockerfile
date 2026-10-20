# Build with:
#   docker build --pull -f deploy/document-converter/Dockerfile -t ridge-document-converter:local .
# Production: pin both image digest and dependency hashes in your image registry.
FROM python:3.12-slim
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN python -m pip install --no-cache-dir 'pypdf>=6,<7' \
    && mkdir -p /app/core/runtime \
    && touch /app/core/__init__.py /app/core/runtime/__init__.py
COPY core/runtime/attachment_context.py /app/core/runtime/attachment_context.py
COPY core/runtime/document_worker.py /app/core/runtime/document_worker.py
USER 65534:65534
ENTRYPOINT ["python", "-m", "core.runtime.document_worker"]
