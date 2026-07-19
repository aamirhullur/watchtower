from __future__ import annotations

import pytest

from watchtower.db import Cursor, Database
from watchtower.summarize import assemble_window


@pytest.mark.asyncio
async def test_db_roundtrip_and_cursor_dedup(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    try:
        sid = await db.open_stream("youtube", "chan", "Title", "https://u", "vid123")
        assert isinstance(sid, int)

        await db.add_chunk(sid, 1, "2026-01-01T00:00:00+00:00", "hello world")
        await db.add_chunk(sid, 2, "2026-01-01T00:01:00+00:00", "more talk https://x.com")
        # duplicate seq ignored
        await db.add_chunk(sid, 2, "x", "dup")

        cid = await db.add_chat(sid, "alice", "hi there", "2026-01-01T00:00:30+00:00")
        assert cid > 0
        await db.add_link(sid, "https://x.com", "transcript", "2026-01-01T00:01:00+00:00")
        await db.add_link(sid, "https://x.com", "transcript", "dup")  # unique ignored

        # First window: everything since the start.
        prev = await db.last_cursor(sid)
        assert prev == Cursor()  # nothing posted yet
        chunks = await db.chunks_since(sid, prev.last_chunk_seq)
        chats = await db.chat_since(sid, prev.last_chat_id)
        assert len(chunks) == 2
        assert len(chats) == 1
        w = assemble_window(chunks, chats, prev)
        await db.record_update(sid, "update", w.cursor)

        # Second window after recording the cursor: no new content.
        prev2 = await db.last_cursor(sid)
        assert prev2.last_chunk_seq == 2
        assert prev2.last_chat_id == cid
        chunks2 = await db.chunks_since(sid, prev2.last_chunk_seq)
        assert chunks2 == []

        # Links are de-duplicated.
        links = await db.links_for(sid)
        assert len(links) == 1

        # Full transcript + replace (refined path).
        assert "hello world" in await db.all_transcript(sid)
        await db.replace_transcript(sid, "clean refined transcript")
        assert await db.all_transcript(sid) == "clean refined transcript"

        await db.end_stream(sid)
        row = await db.get_stream(sid)
        assert row["status"] == "ended"
        assert row["ended_at"] is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prune_old_streams(tmp_path):
    db = Database(str(tmp_path / "r.db"))
    await db.connect()
    try:
        old = await db.open_stream("youtube", "c", "t", "u", "v1")
        new = await db.open_stream("youtube", "c", "t", "u", "v2")
        for sid, txt in ((old, "old text"), (new, "new text")):
            await db.add_chunk(sid, 1, "ts", txt)
            await db.add_chat(sid, "a", "hi", "ts")
            await db.add_link(sid, f"https://x/{sid}", "chat", "ts")
        await db.end_stream(old)
        await db.end_stream(new)
        # Backdate the old stream's ended_at well past the retention window.
        db.conn.execute(
            "UPDATE streams SET ended_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", old)
        )
        db.conn.commit()

        pruned = await db.prune_old_streams(30)
        assert pruned == 1

        # Old stream's chat/transcript/link rows are gone; the stream row remains.
        assert await db.all_transcript(old) == ""
        assert await db.chat_since(old, 0) == []
        assert await db.links_for(old) == []
        assert await db.get_stream(old) is not None
        # Recent stream is untouched.
        assert "new text" in await db.all_transcript(new)
        assert len(await db.links_for(new)) == 1

        # 0/negative disables pruning.
        assert await db.prune_old_streams(0) == 0
    finally:
        await db.close()
