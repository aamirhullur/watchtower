"""Tests for Discord "threads mode": one forum thread per stream.

The go-live creates the thread (payload thread_name + ?wait=true; the response's
channel_id is the thread id, persisted); every later post for the same
thread_key lands in it (?thread_id=...). A fake aiohttp session records each
POST's url/params/json so we can assert the wire shape without real network.
"""

from __future__ import annotations

import pytest

from watchtower.config import Config
from watchtower.db import Database
from watchtower.discord import DiscordPoster
from watchtower.notify import Digest, Find, GoLive, RollingUpdate


# --------------------------------------------------------------------------- #
# Fakes: aiohttp session/response + health sink
# --------------------------------------------------------------------------- #
class FakeResponse:
    """Doubles as the awaitable-less async context manager `session.post` returns."""

    def __init__(self, status: int, json_data: dict | None = None, *, text: str = "", headers: dict | None = None):
        self.status = status
        self._json = json_data
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def json(self) -> dict:
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    async def text(self) -> str:
        return self._text


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, *, params=None, json=None, timeout=None):
        self.calls.append({"url": url, "params": params, "json": json})
        assert self._responses, "unexpected extra POST"
        return self._responses.pop(0)


class FakeHealth:
    def __init__(self):
        self.failures: list[tuple[str, str]] = []

    async def record_failure(self, component: str, msg: str) -> None:
        self.failures.append((component, msg))


def _threads_cfg() -> Config:
    cfg = Config()
    cfg.discord.threads = True
    cfg.discord.forum_webhook = "https://discord.test/forum"
    return cfg


async def _open_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "threads.db"))
    await db.connect()
    return db


# --------------------------------------------------------------------------- #
# Thread creation + reuse
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_first_post_creates_thread_and_stores_channel_id(tmp_path):
    db = await _open_db(tmp_path)
    try:
        session = FakeSession([FakeResponse(200, {"channel_id": "999"})])
        poster = DiscordPoster(_threads_cfg(), session=session, db=db)
        ok = await poster.post(GoLive(channel="C", platform="youtube", title="Big stream", url="u"), thread_key="5")
        assert ok is True
        assert len(session.calls) == 1
        call = session.calls[0]
        # First post creates the thread: thread_name in payload, wait=true in params.
        assert call["url"] == "https://discord.test/forum"
        assert call["params"] == {"wait": "true"}
        assert "thread_name" in call["json"]
        assert "Big stream" in call["json"]["thread_name"]
        # The returned channel_id is the new thread's id, persisted for reuse.
        assert await db.get_thread_id("5") == "999"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_second_post_reuses_existing_thread(tmp_path):
    db = await _open_db(tmp_path)
    try:
        await db.set_thread_id("5", "999")  # thread already created earlier (or pre-restart)
        session = FakeSession([FakeResponse(204)])
        poster = DiscordPoster(_threads_cfg(), session=session, db=db)
        ok = await poster.post(RollingUpdate(channel="C", title="T", url="u", summary="s"), thread_key="5")
        assert ok is True
        call = session.calls[0]
        # Later posts land in the existing thread: thread_id param, no thread_name.
        assert call["params"] == {"thread_id": "999"}
        assert "thread_name" not in call["json"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_digest_with_finds_recap_lands_in_created_thread(tmp_path):
    db = await _open_db(tmp_path)
    try:
        session = FakeSession([FakeResponse(200, {"channel_id": "42"}), FakeResponse(204)])
        poster = DiscordPoster(_threads_cfg(), session=session, db=db)
        note = Digest(
            channel="C", title="T", url="u", summary="s",
            finds=(Find(name="Gadget", detail="a thing"),),
        )
        ok = await poster.post(note, thread_key="7")
        assert ok is True
        assert len(session.calls) == 2
        # Digest embed creates the thread; the finds recap follows into that thread.
        assert session.calls[0]["params"] == {"wait": "true"}
        assert session.calls[1]["params"] == {"thread_id": "42"}
        assert await db.get_thread_id("7") == "42"
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# Flat fallback: thread_key is a no-op unless threads mode is fully wired
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_flat_mode_ignores_thread_key(tmp_path):
    db = await _open_db(tmp_path)
    try:
        cfg = Config()  # threads defaults to False
        cfg.discord.announce_webhook = "https://discord.test/announce"
        session = FakeSession([FakeResponse(204)])
        poster = DiscordPoster(cfg, session=session, db=db)
        ok = await poster.post(GoLive(channel="C", platform="youtube", title="T", url="u"), thread_key="5")
        assert ok is True
        call = session.calls[0]
        assert call["url"] == "https://discord.test/announce"
        assert call["params"] is None
        assert "thread_name" not in call["json"]
        # Nothing threaded happened, so no thread id was recorded.
        assert await db.get_thread_id("5") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_threads_enabled_without_db_falls_back_to_flat():
    # The webhook-test path constructs a db-less poster; threads mode must degrade.
    cfg = _threads_cfg()
    cfg.discord.announce_webhook = "https://discord.test/announce"
    session = FakeSession([FakeResponse(204)])
    poster = DiscordPoster(cfg, session=session, db=None)
    ok = await poster.post(GoLive(channel="C", platform="youtube", title="T", url="u"), thread_key="5")
    assert ok is True
    call = session.calls[0]
    assert call["params"] is None
    assert "thread_name" not in call["json"]


# --------------------------------------------------------------------------- #
# Thread creation failures: no id stored, health surfaced, post fails
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_thread_creation_non_2xx_fails_and_records_health(tmp_path):
    db = await _open_db(tmp_path)
    try:
        health = FakeHealth()
        session = FakeSession([FakeResponse(400, text="bad request")])
        poster = DiscordPoster(_threads_cfg(), session=session, health=health, db=db)
        ok = await poster.post(GoLive(channel="C", platform="youtube", title="T", url="u"), thread_key="5")
        assert ok is False
        assert health.failures  # broken forum webhook is visible, not silent
        assert await db.get_thread_id("5") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_thread_creation_missing_channel_id_fails(tmp_path):
    db = await _open_db(tmp_path)
    try:
        health = FakeHealth()
        # 2xx but the response lacks channel_id: we can't know the thread, so fail.
        session = FakeSession([FakeResponse(200, {"id": "msg1"})])
        poster = DiscordPoster(_threads_cfg(), session=session, health=health, db=db)
        ok = await poster.post(GoLive(channel="C", platform="youtube", title="T", url="u"), thread_key="5")
        assert ok is False
        assert health.failures
        assert await db.get_thread_id("5") is None
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# Posting into an existing thread: failures surface to health; a definitive 404
# (thread deleted server-side) clears the mapping so the next post recreates it,
# while a non-404 (e.g. 400) leaves the mapping intact.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_post_into_existing_thread_400_records_health_and_keeps_mapping(tmp_path):
    db = await _open_db(tmp_path)
    try:
        await db.set_thread_id("5", "999")
        health = FakeHealth()
        session = FakeSession([FakeResponse(400, text="bad request")])
        poster = DiscordPoster(_threads_cfg(), session=session, health=health, db=db)
        ok = await poster.post(RollingUpdate(channel="C", title="T", url="u", summary="s"), thread_key="5")
        assert ok is False
        # A non-404 post failure is surfaced but not treated as "thread gone": the
        # mapping is kept so we keep targeting the same (still-valid) thread.
        assert health.failures
        assert await db.get_thread_id("5") == "999"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_post_into_existing_thread_404_clears_mapping(tmp_path):
    db = await _open_db(tmp_path)
    try:
        await db.set_thread_id("5", "999")
        health = FakeHealth()
        # Thread deleted server-side: Discord returns a definitive 404.
        session = FakeSession([FakeResponse(404, text="Unknown Channel")])
        poster = DiscordPoster(_threads_cfg(), session=session, health=health, db=db)
        ok = await poster.post(RollingUpdate(channel="C", title="T", url="u", summary="s"), thread_key="5")
        assert ok is False
        assert health.failures
        # Stale mapping is cleared so the NEXT post for this key recreates a thread.
        assert await db.get_thread_id("5") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_next_post_after_404_recreates_thread(tmp_path):
    db = await _open_db(tmp_path)
    try:
        await db.set_thread_id("5", "999")
        # First post 404s (mapping cleared); the follow-up post recreates a thread.
        session = FakeSession(
            [FakeResponse(404, text="Unknown Channel"), FakeResponse(200, {"channel_id": "1234"})]
        )
        poster = DiscordPoster(_threads_cfg(), session=session, db=db)
        await poster.post(RollingUpdate(channel="C", title="T", url="u", summary="s"), thread_key="5")
        assert await db.get_thread_id("5") is None
        ok = await poster.post(GoLive(channel="C", platform="youtube", title="T", url="u"), thread_key="5")
        assert ok is True
        # Second call created a fresh thread (thread_name + wait=true).
        assert session.calls[1]["params"] == {"wait": "true"}
        assert "thread_name" in session.calls[1]["json"]
        assert await db.get_thread_id("5") == "1234"
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# Threaded finds recap failure is recorded to health (best-effort: the digest's
# returned result is unaffected).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_threaded_finds_recap_failure_records_health(tmp_path, monkeypatch):
    async def _no_sleep(*_a, **_k):  # skip the 5xx backoff so the test is instant
        return None

    monkeypatch.setattr("watchtower.discord.asyncio.sleep", _no_sleep)
    db = await _open_db(tmp_path)
    try:
        await db.set_thread_id("7", "42")  # thread already exists
        health = FakeHealth()
        # Digest post into the thread succeeds (204); the recap post fails (500 all
        # retries). max_retries=1 keeps the 5xx retry loop from consuming extras.
        session = FakeSession([FakeResponse(204), FakeResponse(500, text="boom")])
        poster = DiscordPoster(_threads_cfg(), session=session, health=health, db=db)
        note = Digest(
            channel="C", title="T", url="u", summary="s",
            finds=(Find(name="Gadget", detail="a thing"),),
        )
        ok = await poster.post(note, thread_key="7", max_retries=1)
        # Digest itself succeeded; the recap failure never flips the result.
        assert ok is True
        assert any("finds recap" in msg for _, msg in health.failures)
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# Retry preserves thread params (thread_id / wait) across a 429 or 5xx.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_threaded_post_retries_preserve_thread_id_param(tmp_path):
    db = await _open_db(tmp_path)
    try:
        await db.set_thread_id("5", "999")
        # 429 then success: the retry must keep hitting the same thread_id.
        session = FakeSession(
            [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(204)]
        )
        poster = DiscordPoster(_threads_cfg(), session=session, db=db)
        ok = await poster.post(RollingUpdate(channel="C", title="T", url="u", summary="s"), thread_key="5")
        assert ok is True
        assert len(session.calls) == 2
        assert session.calls[0]["params"] == {"thread_id": "999"}
        assert session.calls[1]["params"] == {"thread_id": "999"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_threaded_create_retries_preserve_wait_param(tmp_path, monkeypatch):
    async def _no_sleep(*_a, **_k):  # skip the 5xx backoff so the test is instant
        return None

    monkeypatch.setattr("watchtower.discord.asyncio.sleep", _no_sleep)
    db = await _open_db(tmp_path)
    try:
        # 5xx then success on thread creation: the retry must keep wait=true so the
        # channel_id comes back and the new thread is stored.
        session = FakeSession(
            [FakeResponse(500, text="boom"), FakeResponse(200, {"channel_id": "1234"})]
        )
        poster = DiscordPoster(_threads_cfg(), session=session, db=db)
        ok = await poster.post(
            GoLive(channel="C", platform="youtube", title="T", url="u"), thread_key="5", max_retries=3
        )
        assert ok is True
        assert len(session.calls) == 2
        assert session.calls[0]["params"] == {"wait": "true"}
        assert session.calls[1]["params"] == {"wait": "true"}
        assert await db.get_thread_id("5") == "1234"
    finally:
        await db.close()
