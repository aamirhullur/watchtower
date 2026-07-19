from __future__ import annotations

from streamwatch.db import Cursor
from streamwatch.summarize import (
    Window,
    assemble_window,
    build_digest_prompt,
    build_stats_summary,
    build_update_prompt,
    window_is_empty,
)


def chunk(seq, text):
    # assemble_window uses r["text"] and r["seq"]; a dict satisfies that.
    return {"seq": seq, "text": text}


def chat(cid, author, text):
    return {"id": cid, "author": author, "text": text}


def test_assemble_window_basic():
    chunks = [chunk(1, "hello world"), chunk(2, "see https://x.com/tool")]
    chats = [chat(10, "alice", "nice"), chat(11, "bob", "check https://y.com")]
    w = assemble_window(chunks, chats, Cursor())
    assert w.chunk_count == 2
    assert w.chat_count == 2
    assert "hello world" in w.transcript
    assert w.cursor.last_chunk_seq == 2
    assert w.cursor.last_chat_id == 11
    assert set(w.links) == {"https://x.com/tool", "https://y.com"}
    assert w.chat_lines == ["alice: nice", "bob: check https://y.com"]


def test_assemble_window_advances_from_prev_cursor():
    prev = Cursor(last_chunk_seq=5, last_chat_id=100)
    w = assemble_window([chunk(6, "next")], [], prev)
    assert w.cursor.last_chunk_seq == 6
    assert w.cursor.last_chat_id == 100  # unchanged (no new chat)


def test_assemble_window_empty_keeps_prev_cursor():
    prev = Cursor(last_chunk_seq=7, last_chat_id=3)
    w = assemble_window([], [], prev)
    assert w.cursor.last_chunk_seq == 7
    assert w.cursor.last_chat_id == 3


def test_window_is_empty_true_when_quiet():
    assert window_is_empty(Window(transcript="hi", chat_count=1)) is True
    assert window_is_empty(Window()) is True


def test_window_not_empty_with_transcript():
    w = Window(transcript="x" * 100, chat_count=0)
    assert window_is_empty(w) is False


def test_window_not_empty_with_links():
    assert window_is_empty(Window(transcript="", links=["https://x.com"])) is False


def test_window_not_empty_with_active_chat():
    assert window_is_empty(Window(transcript="", chat_count=6)) is False


def test_build_stats_summary_contains_counts_and_links():
    w = Window(transcript="some talk", chunk_count=3, chat_count=4, links=["https://a.com"])
    out = build_stats_summary(w)
    assert "3" in out and "4" in out
    assert "https://a.com" in out


def test_build_update_prompt_includes_sections():
    w = assemble_window([chunk(1, "talk about tools")], [chat(1, "a", "hi")], Cursor())
    p = build_update_prompt(channel="Chan", title="T", window=w, style="Be brief.")
    assert "NEW TRANSCRIPT" in p and "NEW CHAT" in p and "LINKS MENTIONED" in p
    assert "Chan" in p and "Be brief." in p


def test_build_digest_prompt_refined_label():
    p = build_digest_prompt(
        channel="C", title="T", transcript="hello", chat_lines=["a: hi"],
        links=["https://z.com"], style="s", refined=True,
    )
    assert "refined" in p.lower()
    assert "FULL TRANSCRIPT" in p
