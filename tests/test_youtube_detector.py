"""Detector run-loop logic: fetch-error vs not-live vs consecutive-miss offline.

These exercise H2 — a transient fetch failure (429/timeout/non-200) must never be
conflated with the stream going offline, and offline is only declared after N
*consecutive* fetched-and-not-live polls.
"""

from __future__ import annotations

import asyncio

import pytest

from streamwatch.detectors.youtube import FETCH_ERROR, LiveInfo, YouTubeDetector


def _live(vid: str) -> LiveInfo:
    return LiveInfo(video_id=vid, title="t", url=f"https://www.youtube.com/watch?v={vid}")


async def _drive(script, *, offline_confirmations=2, initial_live_id=None):
    det = YouTubeDetector(
        "handle", "Handle", 1, session=object(), offline_confirmations=offline_confirmations
    )
    det.poll_interval = 0.001  # keep the inter-poll wait tiny
    if initial_live_id is not None:
        det._current_video_id = initial_live_id
    events: list[tuple] = []
    stop = asyncio.Event()
    it = iter(script)

    async def fake_poll():
        try:
            return next(it)
        except StopIteration:
            stop.set()
            return FETCH_ERROR

    async def on_live(ev):
        events.append(("live", ev.video_id))

    async def on_offline(platform, channel):
        events.append(("offline", channel))

    det.poll_once = fake_poll  # type: ignore[assignment]
    await asyncio.wait_for(det.run(on_live, on_offline, stop), timeout=5)
    return events


@pytest.mark.asyncio
async def test_fetch_error_while_live_does_not_go_offline():
    events = await _drive([FETCH_ERROR, FETCH_ERROR], initial_live_id="vid00000000")
    assert events == []  # no premature offline from transient fetch failures


@pytest.mark.asyncio
async def test_single_not_live_does_not_go_offline():
    events = await _drive([None], initial_live_id="vid00000000", offline_confirmations=2)
    assert events == []


@pytest.mark.asyncio
async def test_consecutive_not_live_declares_offline():
    events = await _drive([None, None], initial_live_id="vid00000000", offline_confirmations=2)
    assert events == [("offline", "handle")]


@pytest.mark.asyncio
async def test_fetch_error_does_not_reset_or_advance_offline_streak():
    # not-live, then a fetch error (held), then not-live => two real misses => offline.
    events = await _drive(
        [None, FETCH_ERROR, None], initial_live_id="vid00000000", offline_confirmations=2
    )
    assert events == [("offline", "handle")]


@pytest.mark.asyncio
async def test_live_fires_on_live_once_per_new_id():
    events = await _drive([_live("aaaaaaaaaaa"), _live("aaaaaaaaaaa"), _live("bbbbbbbbbbb")])
    assert events == [("live", "aaaaaaaaaaa"), ("live", "bbbbbbbbbbb")]
