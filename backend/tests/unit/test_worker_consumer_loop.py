"""Unit tests for the worker consume loop: polling, commits, heartbeat (issue #156).

用假 consumer 注入轮询脚本与异常，不依赖真实 Kafka，也不使用真实 sleep；
用手动 touch 记录器验证"每次轮询刷新心跳"，而不是在测试里等时间流逝。
"""

from types import SimpleNamespace

import pytest

from app.worker.consumer import run_consume_loop


class _Stop(Exception):
    """哨兵异常：假 consumer 脚本耗尽后抛出，用于结束死循环。"""


class FakeConsumer:
    """最小 Kafka consumer 替身：按脚本返回 getmany 批次，记录 commit 次数。"""

    def __init__(self, batches, events=None):
        self._batches = list(batches)
        self.commits = 0
        self._events = events if events is not None else []

    async def getmany(self, timeout_ms=0):
        if not self._batches:
            raise _Stop
        item = self._batches.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def commit(self):
        self.commits += 1
        self._events.append("commit")


def _msg(value):
    return SimpleNamespace(value=value)


@pytest.mark.asyncio
async def test_heartbeat_refreshed_on_every_poll_including_idle():
    # 三次空轮询（模拟空闲等待消息）→ 心跳应被刷新三次
    consumer = FakeConsumer([{}, {}, {}])
    beats = []

    with pytest.raises(_Stop):
        await run_consume_loop(consumer, touch=lambda: beats.append("beat"))

    assert beats == ["beat", "beat", "beat"]


@pytest.mark.asyncio
async def test_message_processed_then_committed_with_job_id():
    events = []

    async def fake_process(document_id, job_id):
        events.append(("process", document_id, job_id))

    consumer = FakeConsumer(
        [{"topic-0": [_msg({"document_id": "doc-1", "job_id": 7})]}],
        events=events,
    )

    with pytest.raises(_Stop):
        await run_consume_loop(consumer, process=fake_process, touch=lambda: None)

    # 先处理、后提交（失败消息不提交的语义依赖此顺序）
    assert events == [("process", "doc-1", 7), "commit"]


@pytest.mark.asyncio
async def test_non_dict_message_skipped_and_committed():
    processed = []

    async def fake_process(document_id, job_id):
        processed.append(document_id)

    consumer = FakeConsumer([{"topic-0": [_msg(["not", "a", "dict"])]}])

    with pytest.raises(_Stop):
        await run_consume_loop(consumer, process=fake_process, touch=lambda: None)

    assert processed == []
    assert consumer.commits == 1


@pytest.mark.asyncio
async def test_missing_document_id_committed_without_processing():
    processed = []

    async def fake_process(document_id, job_id):
        processed.append(document_id)

    consumer = FakeConsumer([{"topic-0": [_msg({"job_id": 1})]}])

    with pytest.raises(_Stop):
        await run_consume_loop(consumer, process=fake_process, touch=lambda: None)

    assert processed == []
    assert consumer.commits == 1


@pytest.mark.asyncio
async def test_processing_error_does_not_commit_and_loop_continues():
    events = []

    async def fake_process(document_id, job_id):
        if document_id == "doc-1":
            raise RuntimeError("boom")
        events.append(("processed", document_id))

    consumer = FakeConsumer(
        [{"topic-0": [_msg({"document_id": "doc-1"}), _msg({"document_id": "doc-2"})]}],
        events=events,
    )

    with pytest.raises(_Stop):
        await run_consume_loop(consumer, process=fake_process, touch=lambda: events.append("beat"))

    # 轮询后有一次心跳；doc-1 失败不提交、doc-2 成功提交；循环未被异常杀死
    assert events == ["beat", ("processed", "doc-2"), "commit"]


@pytest.mark.asyncio
async def test_poll_failure_propagates_to_caller():
    # 轮询异常（如 Kafka 断连）沿旧语义向上抛给 consume()，由容器重启兜底
    consumer = FakeConsumer([RuntimeError("kafka down")])

    with pytest.raises(RuntimeError, match="kafka down"):
        await run_consume_loop(consumer, touch=lambda: None)

    assert consumer.commits == 0
