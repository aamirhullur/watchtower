"""Tests for the extended `simulate` subcommand: transcripts JSONL round-trip,
live-chat timeline ingest, and refined-VTT digest reuse. No network / no ffmpeg:
audio chunking and STT are monkeypatched, LLM backend is 'none'.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from conftest import FIXTURES

import streamwatch.main as main
from streamwatch.capture import Chunk
from streamwatch.config import Config
from streamwatch.db import Database
from streamwatch.main import (
    StoredTranscript,
    _simulate,
    load_transcripts_jsonl,
    write_transcript_line,
)
from streamwatch.summarize import Summarizer
from streamwatch.util import parse_vtt


# --------------------------------------------------------------------------- #
# Pure JSONL round-trip
# --------------------------------------------------------------------------- #
def test_transcript_jsonl_write_load_roundtrip(tmp_path):
    p = tmp_path / "t.jsonl"
    rows = [(1, "2026-01-01T00:00:00+00:00", "alpha talk"), (3, "2026-01-01T00:02:00+00:00", "gamma https://x.com")]
    with open(p, "a", encoding="utf-8") as fh:
        for seq, ts, text in rows:
            write_transcript_line(fh, seq, ts, text)
    loaded = load_transcripts_jsonl(p)
    assert loaded == [StoredTranscript(seq=s, started_at=ts, text=t) for s, ts, t in rows]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _args(input_path, tmp_path, **overrides):
    ns = argparse.Namespace(
        input=str(input_path),
        dry_run=True,
        name="sim",
        title="Sim title",
        window_chunks=1,
        config="unused",
        transcripts_out=None,
        transcripts_in=None,
        chat_file=None,
        refined_vtt=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _cfg(tmp_path, segment_seconds=60):
    cfg = Config()
    cfg.state_db = str(tmp_path / "state" / "sw.db")  # keep llm_workdir off /var/lib
    cfg.llm.backend = "none"
    cfg.capture.segment_seconds = segment_seconds
    return cfg


def _fake_chunks(monkeypatch, texts):
    """Patch chunk_local_file -> deterministic chunks and build_stt -> mapped STT."""
    started = {i + 1: f"2026-01-01T00:0{i}:00+00:00" for i in range(len(texts))}
    text_by_path = {}

    async def fake_chunk_local_file(cfg, input_path, workdir):
        chunks = []
        for i, _t in enumerate(texts):
            path = Path(workdir) / f"chunk_{i + 1}.wav"
            text_by_path[str(path)] = texts[i]
            chunks.append(Chunk(seq=i + 1, path=path, started_at=started[i + 1]))
        return chunks

    class FakeStt:
        name = "fake"

        async def transcribe(self, wav_path):
            return text_by_path.get(str(wav_path), "")

        async def close(self):
            return None

    monkeypatch.setattr(main, "chunk_local_file", fake_chunk_local_file)
    monkeypatch.setattr(main, "build_stt", lambda _stt_cfg: FakeStt())
    return started


# Capture the pristine method once so stacked spies never chain through each other.
_ORIG_ADD_CHUNK = Database.add_chunk


def _spy_add_chunk(monkeypatch):
    calls: list[tuple] = []

    async def spy(self, stream_id, seq, started_at, text):
        calls.append((seq, started_at, text))
        return await _ORIG_ADD_CHUNK(self, stream_id, seq, started_at, text)

    monkeypatch.setattr(Database, "add_chunk", spy)
    return calls


# --------------------------------------------------------------------------- #
# transcripts-out then transcripts-in produce identical add_chunk calls
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_transcripts_out_then_in_identical_add_chunk(tmp_path, monkeypatch):
    dummy_input = tmp_path / "audio.wav"
    dummy_input.write_bytes(b"not real audio")
    out_file = tmp_path / "transcripts.jsonl"
    # Middle chunk transcribes to empty -> must not be written nor add_chunk'd.
    texts = ["alpha talk", "", "gamma https://x.com"]

    # --- Run 1: transcribe (faked) and write JSONL ---
    _fake_chunks(monkeypatch, texts)
    calls_out = _spy_add_chunk(monkeypatch)
    rc = await _simulate(_cfg(tmp_path), _args(dummy_input, tmp_path, transcripts_out=str(out_file)))
    assert rc == 0
    assert len(calls_out) == 2  # empty chunk skipped

    # File only has the two non-empty lines.
    lines = [ln for ln in out_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2

    # --- Run 2: replay from JSONL, STT must never be constructed ---
    def boom(_cfg_stt):
        raise AssertionError("build_stt must not be called for --transcripts-in")

    monkeypatch.setattr(main, "build_stt", boom)
    calls_in = _spy_add_chunk(monkeypatch)
    rc2 = await _simulate(_cfg(tmp_path), _args(dummy_input, tmp_path, transcripts_in=str(out_file)))
    assert rc2 == 0

    assert calls_in == calls_out


# --------------------------------------------------------------------------- #
# live-chat timeline ingest during the loop
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_chat_file_ingest_follows_chunk_timeline(tmp_path, monkeypatch):
    # 2 transcript chunks, tiny segment so the chat offsets gate across windows:
    # seg=4 -> seq1 covers 4s, seq2 covers 8s. Chat offsets: 3s, 5s, 12s.
    jsonl = tmp_path / "t.jsonl"
    with open(jsonl, "a", encoding="utf-8") as fh:
        write_transcript_line(fh, 1, "2026-01-01T00:00:00+00:00", "first chunk")
        write_transcript_line(fh, 2, "2026-01-01T00:00:04+00:00", "second chunk")

    chat_authors: list[str] = []
    chat_links: list[tuple] = []
    orig_add_chat = Database.add_chat
    orig_add_link = Database.add_link

    async def spy_chat(self, stream_id, author, text, ts):
        chat_authors.append(author)
        return await orig_add_chat(self, stream_id, author, text, ts)

    async def spy_link(self, stream_id, url, source, ts):
        chat_links.append((url, source))
        return await orig_add_link(self, stream_id, url, source, ts)

    monkeypatch.setattr(Database, "add_chat", spy_chat)
    monkeypatch.setattr(Database, "add_link", spy_link)

    cfg = _cfg(tmp_path, segment_seconds=4)
    args = _args(jsonl, tmp_path, transcripts_in=str(jsonl), chat_file=str(FIXTURES / "sample_live_chat.json"))
    rc = await _simulate(cfg, args)
    assert rc == 0

    # All 3 valid chat messages ingested, in offset order (3s, 5s, 12s).
    assert chat_authors == ["UCzzz", "Alice", "Dave"]
    # Alice's message carried a link, ingested with source "chat".
    assert ("https://example.com/tool", "chat") in chat_links


# --------------------------------------------------------------------------- #
# refined-vtt reuses the existing VTT parser + refined digest path
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_refined_vtt_posts_refined_digest(tmp_path, monkeypatch):
    jsonl = tmp_path / "t.jsonl"
    with open(jsonl, "a", encoding="utf-8") as fh:
        write_transcript_line(fh, 1, "2026-01-01T00:00:00+00:00", "rough transcript")

    digest_refined_flags: list[bool] = []
    replaced_text: list[str] = []
    orig_pd = Summarizer.post_digest
    orig_rt = Database.replace_transcript

    async def spy_pd(self, stream_id, target, *, refined=False):
        digest_refined_flags.append(refined)
        return await orig_pd(self, stream_id, target, refined=refined)

    async def spy_rt(self, stream_id, text):
        replaced_text.append(text)
        return await orig_rt(self, stream_id, text)

    monkeypatch.setattr(Summarizer, "post_digest", spy_pd)
    monkeypatch.setattr(Database, "replace_transcript", spy_rt)

    vtt_path = FIXTURES / "sample.vtt"
    args = _args(jsonl, tmp_path, transcripts_in=str(jsonl), refined_vtt=str(vtt_path))
    rc = await _simulate(_cfg(tmp_path), args)
    assert rc == 0

    # Final digest (refined=False) then the refined digest (refined=True).
    assert digest_refined_flags == [False, True]
    # The refined transcript is exactly what the shared VTT parser produces.
    assert replaced_text == [parse_vtt(vtt_path.read_text())]
