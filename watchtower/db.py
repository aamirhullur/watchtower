"""SQLite state store.

Uses the stdlib ``sqlite3`` module behind an ``asyncio.to_thread`` wrapper so the
single-threaded event loop is never blocked on disk I/O. A per-instance lock
serialises writes (SQLite handles one writer at a time anyway).

Tables: streams, transcript_chunks, chat_messages, links, updates_posted, finds.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .util import extract_urls, now_utc, utc_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,
    channel       TEXT NOT NULL,
    title         TEXT,
    url           TEXT,
    video_id      TEXT,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    status        TEXT NOT NULL DEFAULT 'live'   -- live | ended
);
CREATE INDEX IF NOT EXISTS idx_streams_status ON streams(status);

CREATE TABLE IF NOT EXISTS transcript_chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id     INTEGER NOT NULL REFERENCES streams(id),
    seq           INTEGER NOT NULL,          -- capture sequence number
    started_at    TEXT NOT NULL,             -- wall clock when chunk began
    text          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE(stream_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chunks_stream ON transcript_chunks(stream_id, seq);

CREATE TABLE IF NOT EXISTS chat_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id     INTEGER NOT NULL REFERENCES streams(id),
    author        TEXT,
    text          TEXT NOT NULL,
    ts            TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_stream ON chat_messages(stream_id, id);

CREATE TABLE IF NOT EXISTS links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id     INTEGER NOT NULL REFERENCES streams(id),
    url           TEXT NOT NULL,
    source        TEXT NOT NULL,             -- transcript | chat
    ts            TEXT NOT NULL,
    UNIQUE(stream_id, url)
);
CREATE INDEX IF NOT EXISTS idx_links_stream ON links(stream_id);

CREATE TABLE IF NOT EXISTS updates_posted (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id             INTEGER NOT NULL REFERENCES streams(id),
    kind                  TEXT NOT NULL,     -- announce | update | digest | refined
    posted_at             TEXT NOT NULL,
    last_chunk_seq        INTEGER,           -- high-water mark of transcript consumed
    last_chat_id          INTEGER            -- high-water mark of chat consumed
);
CREATE INDEX IF NOT EXISTS idx_updates_stream ON updates_posted(stream_id, id);

CREATE TABLE IF NOT EXISTS finds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id       INTEGER NOT NULL REFERENCES streams(id),
    offset_seconds  INTEGER NOT NULL,          -- stream offset the mention starts at
    type            TEXT NOT NULL,             -- product|tool|benchmark|game|media|tip|other
    name            TEXT NOT NULL,
    detail          TEXT,
    sentiment       TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_finds_stream ON finds(stream_id, offset_seconds);
"""


@dataclass
class Cursor:
    """High-water marks for what a stream's summarizer has already consumed."""

    last_chunk_seq: int = -1
    last_chat_id: int = 0


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    # ---- lifecycle ----------------------------------------------------- #
    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._open)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # ---- streams ------------------------------------------------------- #
    async def open_stream(
        self, platform: str, channel: str, title: str, url: str, video_id: str
    ) -> int:
        def _do() -> int:
            cur = self.conn.execute(
                "INSERT INTO streams(platform, channel, title, url, video_id, started_at, status) "
                "VALUES(?,?,?,?,?,?, 'live')",
                (platform, channel, title, url, video_id, utc_iso()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        return await self._run(_do)

    async def end_stream(self, stream_id: int) -> None:
        def _do() -> None:
            self.conn.execute(
                "UPDATE streams SET status='ended', ended_at=? WHERE id=?",
                (utc_iso(), stream_id),
            )
            self.conn.commit()

        await self._run(_do)

    async def get_stream(self, stream_id: int) -> sqlite3.Row | None:
        def _do():
            return self.conn.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()

        return await self._run(_do)

    async def find_active_stream(self, platform: str, video_id: str) -> sqlite3.Row | None:
        """Find a still-live stream row for a (platform, video_id) pair.

        Used to reconcile after a restart so we don't double-announce.
        """
        def _do():
            return self.conn.execute(
                "SELECT * FROM streams WHERE platform=? AND video_id=? AND status='live' "
                "ORDER BY id DESC LIMIT 1",
                (platform, video_id),
            ).fetchone()

        return await self._run(_do)

    # ---- transcript ---------------------------------------------------- #
    async def add_chunk(self, stream_id: int, seq: int, started_at: str, text: str) -> None:
        def _do() -> None:
            self.conn.execute(
                "INSERT OR IGNORE INTO transcript_chunks(stream_id, seq, started_at, text, created_at) "
                "VALUES(?,?,?,?,?)",
                (stream_id, seq, started_at, text, utc_iso()),
            )
            self.conn.commit()

        await self._run(_do)

    async def chunks_since(self, stream_id: int, after_seq: int) -> list[sqlite3.Row]:
        def _do():
            return self.conn.execute(
                "SELECT * FROM transcript_chunks WHERE stream_id=? AND seq>? ORDER BY seq",
                (stream_id, after_seq),
            ).fetchall()

        return await self._run(_do)

    async def all_transcript(self, stream_id: int) -> str:
        def _do():
            rows = self.conn.execute(
                "SELECT text FROM transcript_chunks WHERE stream_id=? ORDER BY seq",
                (stream_id,),
            ).fetchall()
            return " ".join(r["text"] for r in rows if r["text"])

        return await self._run(_do)

    async def replace_transcript(self, stream_id: int, text: str) -> None:
        """Replace a stream's transcript with a single refined chunk (VOD captions)."""
        def _do() -> None:
            self.conn.execute("DELETE FROM transcript_chunks WHERE stream_id=?", (stream_id,))
            self.conn.execute(
                "INSERT INTO transcript_chunks(stream_id, seq, started_at, text, created_at) "
                "VALUES(?, 0, ?, ?, ?)",
                (stream_id, utc_iso(), text, utc_iso()),
            )
            self.conn.commit()

        await self._run(_do)

    # ---- chat ---------------------------------------------------------- #
    async def add_chat(self, stream_id: int, author: str, text: str, ts: str) -> int:
        def _do() -> int:
            cur = self.conn.execute(
                "INSERT INTO chat_messages(stream_id, author, text, ts, created_at) VALUES(?,?,?,?,?)",
                (stream_id, author, text, ts, utc_iso()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        return await self._run(_do)

    async def chat_since(
        self, stream_id: int, after_id: int, limit: int | None = None
    ) -> list[sqlite3.Row]:
        def _do():
            sql = "SELECT * FROM chat_messages WHERE stream_id=? AND id>? ORDER BY id"
            params: tuple = (stream_id, after_id)
            if limit is not None:
                sql += " LIMIT ?"
                params = (stream_id, after_id, limit)
            return self.conn.execute(sql, params).fetchall()

        return await self._run(_do)

    # ---- links --------------------------------------------------------- #
    async def add_link(self, stream_id: int, url: str, source: str, ts: str) -> None:
        def _do() -> None:
            self.conn.execute(
                "INSERT OR IGNORE INTO links(stream_id, url, source, ts) VALUES(?,?,?,?)",
                (stream_id, url, source, ts),
            )
            self.conn.commit()

        await self._run(_do)

    async def add_links_from(self, stream_id: int, text: str, source: str, ts: str) -> None:
        """Extract every URL from ``text`` and insert each as a link row.

        Convenience over ``add_link`` for the common transcript/chat ingest idiom;
        ``source`` (transcript|chat) and ``ts`` are recorded verbatim per call.
        """
        for url in extract_urls(text):
            await self.add_link(stream_id, url, source, ts)

    # ---- ingest steps (shared by the live pipeline and `simulate`) ------ #
    async def persist_transcript_chunk(
        self, stream_id: int, seq: int, started_at: str, text: str
    ) -> None:
        """Persist one transcript chunk and index the links it contains.

        The single "add a transcript chunk" ingest step, shared by the live
        pipeline and the `simulate` acceptance test so both drive the same code
        rather than parallel copies. Callers gate on non-empty ``text``.
        """
        await self.add_chunk(stream_id, seq, started_at, text)
        await self.add_links_from(stream_id, text, "transcript", started_at)

    async def persist_chat_message(
        self, stream_id: int, author: str, text: str, ts: str
    ) -> None:
        """Persist one chat message and index the links it contains.

        The single "add a chat message" ingest step, shared by the live pipeline
        and `simulate`.
        """
        await self.add_chat(stream_id, author, text, ts)
        await self.add_links_from(stream_id, text, "chat", ts)

    async def links_for(self, stream_id: int) -> list[sqlite3.Row]:
        def _do():
            return self.conn.execute(
                "SELECT * FROM links WHERE stream_id=? ORDER BY id", (stream_id,)
            ).fetchall()

        return await self._run(_do)

    # ---- finds --------------------------------------------------------- #
    async def add_find(
        self,
        stream_id: int,
        offset_seconds: int,
        type: str,
        name: str,
        detail: str,
        sentiment: str,
    ) -> int:
        def _do() -> int:
            cur = self.conn.execute(
                "INSERT INTO finds(stream_id, offset_seconds, type, name, detail, sentiment, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (stream_id, offset_seconds, type, name, detail, sentiment, utc_iso()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        return await self._run(_do)

    async def finds_for(self, stream_id: int) -> list[sqlite3.Row]:
        def _do():
            return self.conn.execute(
                "SELECT * FROM finds WHERE stream_id=? ORDER BY offset_seconds, id",
                (stream_id,),
            ).fetchall()

        return await self._run(_do)

    # ---- cursors / updates -------------------------------------------- #
    async def last_cursor(self, stream_id: int) -> Cursor:
        def _do() -> Cursor:
            row = self.conn.execute(
                "SELECT last_chunk_seq, last_chat_id FROM updates_posted "
                "WHERE stream_id=? AND last_chunk_seq IS NOT NULL ORDER BY id DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
            if row is None:
                return Cursor()
            return Cursor(
                last_chunk_seq=row["last_chunk_seq"] if row["last_chunk_seq"] is not None else -1,
                last_chat_id=row["last_chat_id"] or 0,
            )

        return await self._run(_do)

    async def record_update(
        self, stream_id: int, kind: str, cursor: Cursor | None = None
    ) -> None:
        def _do() -> None:
            self.conn.execute(
                "INSERT INTO updates_posted(stream_id, kind, posted_at, last_chunk_seq, last_chat_id) "
                "VALUES(?,?,?,?,?)",
                (
                    stream_id,
                    kind,
                    utc_iso(now_utc()),
                    cursor.last_chunk_seq if cursor else None,
                    cursor.last_chat_id if cursor else None,
                ),
            )
            self.conn.commit()

        await self._run(_do)

    # ---- retention ----------------------------------------------------- #
    async def prune_old_streams(self, retention_days: int) -> int:
        """Delete chat/transcript/link rows for streams ended more than
        ``retention_days`` ago. Returns the number of streams pruned. The
        lightweight stream rows themselves are kept. 0/negative disables pruning.
        """
        if retention_days <= 0:
            return 0
        cutoff = utc_iso(now_utc() - timedelta(days=retention_days))

        def _do() -> int:
            rows = self.conn.execute(
                "SELECT id FROM streams WHERE status='ended' AND ended_at IS NOT NULL "
                "AND ended_at < ?",
                (cutoff,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if not ids:
                return 0
            qmarks = ",".join("?" * len(ids))
            for table in ("transcript_chunks", "chat_messages", "links", "finds"):
                self.conn.execute(f"DELETE FROM {table} WHERE stream_id IN ({qmarks})", ids)
            self.conn.commit()
            return len(ids)

        return await self._run(_do)
