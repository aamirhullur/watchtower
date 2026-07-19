"""Tests for the "Finds" feature: JSON parsing, deep links, offset math,
dedup, db round-trip, and embed field construction. No network / no LLM."""

from __future__ import annotations

import pytest

from watchtower.config import Config
from watchtower.db import Cursor, Database
from watchtower.discord import (
    _finds_field,
    render_digest,
    render_finds_recap,
    render_rolling_update,
)
from watchtower.llm.base import LLMBackend, LLMResult
from watchtower.notify import Digest, Find, FindsRecap, RollingUpdate
from watchtower.summarize import (
    Summarizer,
    assemble_window,
    build_finds_prompt,
    dedupe_finds,
    parse_finds,
)
from watchtower.util import deep_link


class FakeLLM(LLMBackend):
    name = "claude_cli"

    def __init__(self, text="", ok=True):
        self._text = text
        self._ok = ok
        self.calls = 0

    async def summarize(self, prompt: str) -> LLMResult:
        self.calls += 1
        return LLMResult(ok=self._ok, text=self._text, error="" if self._ok else "boom")


def chunk(seq, text):
    return {"seq": seq, "text": text}


# --------------------------------------------------------------------------- #
# parse_finds
# --------------------------------------------------------------------------- #
def test_parse_finds_valid_array():
    text = (
        '[{"type": "product", "name": "GMKtec K8 Plus", '
        '"detail": "A mini PC he recommends.", "sentiment": "positive"}]'
    )
    finds = parse_finds(text)
    assert len(finds) == 1
    f = finds[0]
    assert f["type"] == "product"
    assert f["name"] == "GMKtec K8 Plus"
    assert f["detail"] == "A mini PC he recommends."
    assert f["sentiment"] == "positive"


def test_parse_finds_prose_wrapped_json():
    text = (
        "Sure! Here are the discoveries I found:\n"
        '```json\n[{"type": "game", "name": "Balatro", "detail": "A roguelike deck builder."}]\n```\n'
        "Let me know if you want more."
    )
    finds = parse_finds(text)
    assert len(finds) == 1
    assert finds[0]["name"] == "Balatro"
    # Missing sentiment defaults to neutral.
    assert finds[0]["sentiment"] == "neutral"


def test_parse_finds_skips_malformed_entries():
    text = (
        "["
        '{"type": "tool", "name": "ripgrep", "detail": "fast grep"},'  # good
        '{"type": "product"},'                                          # no name
        '{"name": "no type here"},'                                     # no type
        '"just a string",'                                              # not an object
        '{"type": "", "name": "empty type"},'                          # empty type
        '{"type": "book", "name": "  ", "detail": "blank name"},'       # blank name
        '{"type": "media", "name": "Dune", "detail": "sci-fi film"}'    # good
        "]"
    )
    finds = parse_finds(text)
    assert [f["name"] for f in finds] == ["ripgrep", "Dune"]


def test_parse_finds_non_json_returns_empty():
    assert parse_finds("No discoveries in this window.") == []
    assert parse_finds("") == []
    assert parse_finds("here is a [broken array with no close") == []


def test_parse_finds_empty_array():
    assert parse_finds("[]") == []


def test_parse_finds_caps_entries():
    entries = ",".join(
        f'{{"type": "other", "name": "thing{i}"}}' for i in range(20)
    )
    finds = parse_finds(f"[{entries}]", cap=10)
    assert len(finds) == 10


def test_parse_finds_string_with_brackets_does_not_truncate():
    # A ']' inside a string value must not close the array early.
    text = '[{"type": "tip", "name": "arr[0] indexing", "detail": "use arr[0]"}]'
    finds = parse_finds(text)
    assert len(finds) == 1
    assert finds[0]["name"] == "arr[0] indexing"


# --------------------------------------------------------------------------- #
# deep_link
# --------------------------------------------------------------------------- #
def test_deep_link_youtube_with_params():
    # watch URL already has ?v=... -> append &t=; offset rounds down to minute -30s.
    link = deep_link("https://www.youtube.com/watch?v=abc123", "youtube", 130)
    assert link == "https://www.youtube.com/watch?v=abc123&t=90s"


def test_deep_link_youtube_offset_floor_at_zero():
    # 0s -> (0*60)-30 = -30 -> clamped to 0.
    assert deep_link("https://youtu.be/x?v=y", "youtube", 0).endswith("&t=0s")
    assert deep_link("https://youtu.be/x?v=y", "youtube", 20).endswith("&t=0s")


def test_deep_link_youtube_no_query_uses_question_mark():
    assert deep_link("https://youtu.be/abc", "youtube", 125) == "https://youtu.be/abc?t=90s"


def test_deep_link_empty_url():
    assert deep_link("", "youtube", 100) == ""


def test_deep_link_non_youtube():
    assert deep_link("https://twitch.tv/x", "twitch", 100) == ""
    assert deep_link("https://twitch.tv/videos/123", "simulate", 100) == ""


# --------------------------------------------------------------------------- #
# offset math from Window.first_seq
# --------------------------------------------------------------------------- #
def test_window_carries_first_seq():
    w = assemble_window([chunk(4, "a"), chunk(5, "b")], [], Cursor(last_chunk_seq=3))
    assert w.first_seq == 4
    assert w.cursor.last_chunk_seq == 5


def test_window_first_seq_default_when_empty():
    w = assemble_window([], [], Cursor(last_chunk_seq=7))
    assert w.first_seq == -1


def test_offset_math_from_first_seq():
    # seq N covers offset (N-1)*segment_seconds. First chunk seq 4, seg 60 -> 180s.
    seg = 60
    w = assemble_window([chunk(4, "talk")], [], Cursor())
    start_offset = max(0, (w.first_seq - 1) * seg) if w.first_seq >= 1 else 0
    assert start_offset == 180
    # seq 1 -> offset 0
    w1 = assemble_window([chunk(1, "start")], [], Cursor())
    assert (w1.first_seq - 1) * seg == 0


# --------------------------------------------------------------------------- #
# dedupe_finds
# --------------------------------------------------------------------------- #
def _row(name, detail, offset):
    return {"name": name, "detail": detail, "offset_seconds": offset}


def test_dedupe_finds_case_insensitive_keep_first():
    rows = [
        _row("Balatro", "first mention", 60),
        _row("balatro", "second mention", 300),
        _row("ripgrep", "a grep tool", 120),
    ]
    out = dedupe_finds(rows, "https://youtube.com/watch?v=x", "youtube")
    assert [f["name"] for f in out] == ["Balatro", "ripgrep"]
    assert out[0]["detail"] == "first mention"
    # Deep link built from the first row's offset (60 -> 30s).
    assert out[0]["deeplink"] == "https://youtube.com/watch?v=x&t=30s"


def test_dedupe_finds_no_link_for_non_youtube():
    rows = [_row("Some Game", "a game", 120)]
    out = dedupe_finds(rows, "https://twitch.tv/x", "twitch")
    assert out[0]["deeplink"] == ""


def test_dedupe_finds_caps():
    rows = [_row(f"thing{i}", "d", i * 60) for i in range(40)]
    out = dedupe_finds(rows, "", "youtube", cap=25)
    assert len(out) == 25


# --------------------------------------------------------------------------- #
# db add_find / finds_for round-trip
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_db_finds_roundtrip_ordered(tmp_path):
    db = Database(str(tmp_path / "f.db"))
    await db.connect()
    try:
        sid = await db.open_stream("youtube", "chan", "T", "https://u", "vid")
        # Insert out of offset order; finds_for returns ordered by offset.
        await db.add_find(sid, 300, "game", "Balatro", "roguelike", "positive")
        await db.add_find(sid, 60, "product", "K8 Plus", "mini pc", "positive")
        rows = await db.finds_for(sid)
        assert [r["name"] for r in rows] == ["K8 Plus", "Balatro"]
        assert rows[0]["offset_seconds"] == 60
        assert rows[0]["type"] == "product"
        assert rows[0]["sentiment"] == "positive"
        assert rows[0]["created_at"]
        # Isolated per stream.
        other = await db.open_stream("youtube", "c2", "T", "u", "v2")
        assert await db.finds_for(other) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_db_finds_pruned_with_stream(tmp_path):
    db = Database(str(tmp_path / "p.db"))
    await db.connect()
    try:
        sid = await db.open_stream("youtube", "c", "t", "u", "v1")
        await db.add_find(sid, 0, "tool", "ripgrep", "grep", "neutral")
        await db.end_stream(sid)
        db.conn.execute(
            "UPDATE streams SET ended_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", sid)
        )
        db.conn.commit()
        assert await db.prune_old_streams(30) == 1
        assert await db.finds_for(sid) == []
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# embed field construction + 1024 cap
# --------------------------------------------------------------------------- #
def test_finds_field_renders_name_detail_link():
    finds = (
        Find(name="K8 Plus", detail="a mini pc", deeplink="https://y?t=30s"),
        Find(name="NoLink", detail="no deeplink here", deeplink=""),
    )
    field = _finds_field(finds)
    assert field["name"] == "🔎 Finds"
    assert "**K8 Plus** — a mini pc [↗](https://y?t=30s)" in field["value"]
    # No link markup when deeplink is empty.
    assert "**NoLink** — no deeplink here" in field["value"]
    assert "[↗]()" not in field["value"]


def test_finds_field_limit_five():
    finds = tuple(Find(name=f"n{i}", detail="d") for i in range(8))
    field = _finds_field(finds, limit=5)
    assert field["value"].count("\n") == 4  # 5 lines


def test_finds_field_respects_1024_cap():
    finds = tuple(
        Find(name="x" * 400, detail="y" * 400, deeplink="https://z?t=0s") for _ in range(25)
    )
    field = _finds_field(finds, limit=25)
    assert len(field["value"]) <= 1024


def test_finds_field_none_when_empty():
    assert _finds_field(()) is None
    assert _finds_field(None) is None
    # Entries with blank names collapse to no field.
    assert _finds_field((Find(name="  ", detail="d"),)) is None


def test_update_embed_includes_finds_field():
    e = render_rolling_update(
        RollingUpdate(
            channel="C", title="T", url="https://u", summary="s", links=("https://a.com",),
            finds=(Find(name="Balatro", detail="a game", deeplink="https://u&t=0s"),),
        ),
        max_desc=3800,
    )
    field_names = [f["name"] for f in e["fields"]]
    assert "🔎 Finds" in field_names
    assert "Links" in field_names


def test_update_embed_no_finds_no_field():
    e = render_rolling_update(
        RollingUpdate(channel="C", title="T", url="", summary="s", finds=()), max_desc=3800
    )
    assert "fields" not in e


def test_digest_embed_has_no_finds_field():
    # Finds moved to a standalone follow-up message (render_finds_recap): a full
    # stream's list can't fit a 1024-char embed field.
    e = render_digest(
        Digest(channel="C", title="T", url="https://u", summary="s"), max_desc=3800
    )
    assert "fields" not in e


# --------------------------------------------------------------------------- #
# render_finds_recap (standalone end-of-stream recap message)
# --------------------------------------------------------------------------- #
def test_finds_embed_renders_all_25_in_description():
    finds = tuple(
        Find(name=f"tool{i}", detail=f"detail {i}", deeplink=f"https://y?t={i}s")
        for i in range(25)
    )
    e = render_finds_recap(FindsRecap(channel="C", title="Stream T", url="https://u", finds=finds))
    assert e["title"].startswith("🔎 Finds — Stream T")
    for i in range(25):
        assert f"**tool{i}** — detail {i} [↗](https://y?t={i}s)" in e["description"]
    assert len(e["description"]) <= 4096


def test_finds_embed_none_when_empty():
    assert render_finds_recap(FindsRecap(channel="C", title="T", url="", finds=())) is None
    assert render_finds_recap(FindsRecap(channel="C", title="T", url="", finds=(Find(name=" "),))) is None


def test_finds_embed_overflow_drops_whole_lines_with_marker():
    finds = tuple(Find(name=f"n{i}" + "x" * 300, detail="y" * 200) for i in range(25))
    e = render_finds_recap(FindsRecap(channel="C", title="T", url="", finds=finds))
    assert len(e["description"]) <= 4096
    # Truncation is visible and lands on a line boundary, never mid-line.
    last = e["description"].splitlines()[-1]
    assert last.startswith("… (+") and last.endswith("more)")
    for line in e["description"].splitlines()[:-1]:
        assert line.startswith("**n")


def test_finds_embed_no_link_markup_when_deeplink_empty():
    e = render_finds_recap(
        FindsRecap(channel="C", title="T", url="", finds=(Find(name="K8 Plus", detail="mini pc"),))
    )
    assert "**K8 Plus** — mini pc" in e["description"]
    assert "[↗]" not in e["description"]


# --------------------------------------------------------------------------- #
# Summarizer._extract_finds integration (real db, fake LLM)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_extract_finds_stores_and_builds_deeplinks(tmp_path):
    db = Database(str(tmp_path / "e.db"))
    await db.connect()
    try:
        sid = await db.open_stream(
            "youtube", "chan", "T", "https://www.youtube.com/watch?v=abc", "abc"
        )
        stream = await db.get_stream(sid)
        llm = FakeLLM(
            text='[{"type": "product", "name": "GMKtec K8 Plus", "detail": "mini pc", "sentiment": "positive"}]'
        )
        cfg = Config()
        cfg.capture.segment_seconds = 60
        s = Summarizer(cfg, db=db, llm=llm, poster=None)  # type: ignore[arg-type]
        # Window starts at chunk seq 4 -> offset (4-1)*60 = 180s.
        window = assemble_window([chunk(4, "talking K8 Plus")], [], Cursor(last_chunk_seq=3))
        out = await s._extract_finds(sid, stream, window)

        assert len(out) == 1
        assert out[0]["name"] == "GMKtec K8 Plus"
        # 180s -> round to 180 -30 = 150s.
        assert out[0]["deeplink"] == "https://www.youtube.com/watch?v=abc&t=150s"
        # Stored with the window's starting offset.
        rows = await db.finds_for(sid)
        assert len(rows) == 1
        assert rows[0]["offset_seconds"] == 180
        assert rows[0]["name"] == "GMKtec K8 Plus"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_extract_finds_llm_failure_returns_empty(tmp_path):
    db = Database(str(tmp_path / "ef.db"))
    await db.connect()
    try:
        sid = await db.open_stream("youtube", "c", "T", "https://u", "v")
        stream = await db.get_stream(sid)
        s = Summarizer(Config(), db=db, llm=FakeLLM(ok=False), poster=None)  # type: ignore[arg-type]
        window = assemble_window([chunk(1, "talk")], [], Cursor())
        assert await s._extract_finds(sid, stream, window) == []
        assert await db.finds_for(sid) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_extract_finds_none_backend_skips(tmp_path):
    db = Database(str(tmp_path / "en.db"))
    await db.connect()
    try:
        sid = await db.open_stream("youtube", "c", "T", "https://u", "v")
        stream = await db.get_stream(sid)

        class NoneLLM(FakeLLM):
            name = "none"

        llm = NoneLLM(text='[{"type": "tool", "name": "x"}]')
        s = Summarizer(Config(), db=db, llm=llm, poster=None)  # type: ignore[arg-type]
        window = assemble_window([chunk(1, "talk")], [], Cursor())
        assert await s._extract_finds(sid, stream, window) == []
        assert llm.calls == 0  # short-circuited, no wasted call
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #
def test_build_finds_prompt_has_json_instructions():
    w = assemble_window([chunk(1, "talking about the GMKtec K8 Plus")], [], Cursor())
    p = build_finds_prompt(window=w, style="Be concise.")
    assert "JSON array" in p
    assert "TRANSCRIPT" in p
    assert "Be concise." in p
    assert "GMKtec K8 Plus" in p


# --------------------------------------------------------------------------- #
# post_digest posts the standalone finds message (final only, not refined)
# --------------------------------------------------------------------------- #
class StubPoster:
    def __init__(self):
        self.posts = []  # neutral notification payloads

    async def post(self, note, **kw):
        self.posts.append(note)
        return True


async def _digest_fixture(tmp_path, name):
    from watchtower.config import WatchTarget

    db = Database(str(tmp_path / name))
    await db.connect()
    sid = await db.open_stream("youtube", "chan", "T", "https://www.youtube.com/watch?v=abc", "abc")
    await db.add_chunk(sid, 1, "2026-01-01T00:00:00+00:00", "talking about tools")
    await db.add_find(sid, 60, "product", "K8 Plus", "mini pc", "positive")
    target = WatchTarget(platform="youtube", handle="chan")
    return db, sid, target


@pytest.mark.asyncio
async def test_post_digest_posts_followup_finds_message(tmp_path):
    db, sid, target = await _digest_fixture(tmp_path, "pd.db")
    try:
        poster = StubPoster()
        s = Summarizer(Config(), db=db, llm=FakeLLM(text="digest text"), poster=poster)  # type: ignore[arg-type]
        assert await s.post_digest(sid, target)
        # A Digest followed by a standalone FindsRecap (final digest only).
        assert [type(n).__name__ for n in poster.posts] == ["Digest", "FindsRecap"]
        recap = poster.posts[1]
        assert isinstance(recap, FindsRecap)
        assert recap.finds[0].name == "K8 Plus"
        # Render at the delivery boundary to confirm the embed content is intact.
        finds_embed = render_finds_recap(recap)
        assert finds_embed["title"].startswith("🔎 Finds")
        assert "**K8 Plus** — mini pc" in finds_embed["description"]
        # Deep link from offset 60 -> t=30s.
        assert "t=30s" in finds_embed["description"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_post_digest_refined_skips_finds_message(tmp_path):
    db, sid, target = await _digest_fixture(tmp_path, "pr.db")
    try:
        poster = StubPoster()
        s = Summarizer(Config(), db=db, llm=FakeLLM(text="refined text"), poster=poster)  # type: ignore[arg-type]
        assert await s.post_digest(sid, target, refined=True)
        # Only the refined digest — no standalone finds recap follows it.
        assert [type(n).__name__ for n in poster.posts] == ["Digest"]
        assert poster.posts[0].refined is True
    finally:
        await db.close()
