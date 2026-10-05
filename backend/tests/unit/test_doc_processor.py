"""Unit tests for the unified document processor."""

from types import SimpleNamespace

import pytest

from app.worker.doc_processor import DocumentProcessor


@pytest.mark.asyncio
async def test_get_embeddings_preserves_length(monkeypatch):
    async def fake_get_embeddings(texts):
        return [[1.0]] * len(texts)

    monkeypatch.setattr(
        "app.worker.doc_processor.embedding_service.get_embeddings",
        fake_get_embeddings,
    )

    processor = DocumentProcessor()
    embeddings = await processor._get_embeddings(["hello", "", "world"])

    assert len(embeddings) == 3


@pytest.mark.parametrize("with_progress", [True, False])
@pytest.mark.asyncio
async def test_process_reports_progress_at_stage_boundaries(monkeypatch, with_progress):
    """心跳回调（issue #156）应在每个阶段边界各触发一次；缺省时为 no-op。"""
    processor = DocumentProcessor()
    events = []

    async def fake_lock(document_id):
        events.append("lock")
        return True

    async def fake_load(document_id, job_id):
        events.append("load")
        return SimpleNamespace(file_path="f.pdf", organization_id="org", id="doc-1")

    async def fake_parse(file_path, organization_id):
        events.append("parse")
        return {"chunks": [{"chunk_text": "hi", "chunk_index": 0, "chunk_length": 2}]}

    async def fake_embeddings(texts):
        events.append("embed")
        return [[1.0]]

    async def fake_replace(doc, chunks_data, embeddings):
        events.append("replace")

    async def fake_success(document_id, chunks_data, job_id):
        events.append("success")

    async def fake_release(document_id):
        events.append("release")

    monkeypatch.setattr(processor, "_acquire_processing_lock", fake_lock)
    monkeypatch.setattr(processor, "_load_and_mark_parsing", fake_load)
    monkeypatch.setattr(processor.parser, "parse_document", fake_parse)
    monkeypatch.setattr(processor, "_get_embeddings", fake_embeddings)
    monkeypatch.setattr(processor, "_replace_document_state", fake_replace)
    monkeypatch.setattr(processor, "_mark_success", fake_success)
    monkeypatch.setattr(processor, "_release_processing_lock", fake_release)

    on_progress = (lambda: events.append("beat")) if with_progress else None
    ok = await processor.process("doc-1", 7, on_progress=on_progress)

    assert ok is True
    expected = ["lock", "load", "parse", "embed", "replace", "success"]
    if with_progress:
        # 阶段边界：加锁后 / 解析后 / 向量化后 / 替换后 / 标记成功后
        expected = [
            "lock",
            "beat",
            "load",
            "parse",
            "beat",
            "embed",
            "beat",
            "replace",
            "beat",
            "success",
            "beat",
        ]
    expected.append("release")
    assert events == expected
