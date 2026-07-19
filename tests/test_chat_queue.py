"""M4: the per-session chat queue is bounded and drops the oldest on overflow."""

from __future__ import annotations

import pytest

from watchtower.chat.base import ChatMessage
from watchtower.config import Config
from watchtower.pipeline import StreamSession


def _make_session(maxq: int) -> StreamSession:
    cfg = Config()
    cfg.chat_queue_max = maxq
    # Only cfg (for chat_queue_max) is touched by __init__ / enqueue_chat.
    return StreamSession(cfg, None, None, None, None, None, None, None)


@pytest.mark.asyncio
async def test_chat_queue_drops_oldest_on_overflow():
    sess = _make_session(2)
    for i in range(5):
        await sess.enqueue_chat(ChatMessage(author="a", text=f"m{i}", ts="t"))

    assert sess.chat_queue.qsize() == 2
    drained = [sess.chat_queue.get_nowait().text for _ in range(2)]
    assert drained == ["m3", "m4"]  # oldest three (m0..m2) dropped
    assert sess._chat_dropped == 3


@pytest.mark.asyncio
async def test_chat_queue_no_drop_under_capacity():
    sess = _make_session(10)
    for i in range(3):
        await sess.enqueue_chat(ChatMessage(author="a", text=f"m{i}", ts="t"))
    assert sess.chat_queue.qsize() == 3
    assert sess._chat_dropped == 0
