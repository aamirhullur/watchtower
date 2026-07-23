"""build_detectors factory: per-platform grouping + the Detector seam.

A (structural): detectors now share a base ABC + a build_detectors() factory that
owns the wiring app.run() used to hardcode (YouTube one-per-channel; Twitch one
shared with the chat router). The twitch module import stays lazy (its twitchio
extra is optional) and must not happen when there are no Twitch targets.
"""

from __future__ import annotations

import sys

from watchtower.config import Config, WatchTarget
from watchtower.detectors import build_detectors
from watchtower.detectors.base import Detector
from watchtower.detectors.twitch import TwitchDetector
from watchtower.detectors.youtube import YouTubeDetector


def _cfg(targets):
    cfg = Config()
    cfg.watch = targets
    return cfg


def test_youtube_targets_get_one_detector_each():
    cfg = _cfg(
        [
            WatchTarget(platform="youtube", handle="@a"),
            WatchTarget(platform="youtube", handle="@b"),
        ]
    )
    dets = build_detectors(cfg, session=object())
    assert [type(d) for d in dets] == [YouTubeDetector, YouTubeDetector]
    assert all(isinstance(d, Detector) for d in dets)
    assert [d.name for d in dets] == ["yt:a", "yt:b"]


def test_disabled_targets_are_skipped():
    cfg = _cfg(
        [
            WatchTarget(platform="youtube", handle="@a"),
            WatchTarget(platform="youtube", handle="@b", enabled=False),
        ]
    )
    dets = build_detectors(cfg, session=object())
    assert [d.name for d in dets] == ["yt:a"]


def test_twitch_targets_collapse_to_one_detector_with_chat_router():
    async def router(*a):
        return None

    cfg = _cfg(
        [
            WatchTarget(platform="youtube", handle="@a"),
            WatchTarget(platform="twitch", handle="x"),
            WatchTarget(platform="twitch", handle="y"),
        ]
    )
    dets = build_detectors(cfg, session=object(), chat_router=router)
    assert [type(d).__name__ for d in dets] == ["YouTubeDetector", "TwitchDetector"]
    twitch = dets[-1]
    assert isinstance(twitch, TwitchDetector)
    assert twitch.chat_router is router
    assert {t.handle for t in twitch.targets} == {"x", "y"}
    assert twitch.name == "twitch"


def test_no_twitch_targets_does_not_import_twitch_module(monkeypatch):
    # Lazy import: a YouTube-only deployment must never pull in the twitch module
    # (and its optional twitchio extra).
    monkeypatch.delitem(sys.modules, "watchtower.detectors.twitch", raising=False)
    cfg = _cfg([WatchTarget(platform="youtube", handle="@a")])
    build_detectors(cfg, session=object())
    assert "watchtower.detectors.twitch" not in sys.modules


# --- watchdog / reconcile poll (twitch.py hardening) -------------------------


class _FakeStreamUser:
    def __init__(self, uid, name):
        self.id = uid
        self.name = name


class _FakeStream:
    def __init__(self, uid, name, title):
        self.user = _FakeStreamUser(uid, name)
        self.title = title


class _FakeStreamsClient:
    def __init__(self, streams):
        self._streams = streams
        self.calls = []

    def fetch_streams(self, **kw):
        self.calls.append(kw)

        async def _gen():
            for s in self._streams:
                yield s

        return _gen()


def _twitch(targets, chat_router=None):
    cfg = _cfg(targets)
    return TwitchDetector(cfg, [t for t in targets if t.platform == "twitch"], chat_router)


def test_poll_live_now_fires_on_live_with_uid_video_id():
    import asyncio

    det = _twitch([WatchTarget(platform="twitch", handle="theo")])
    det._uid_to_login = {"146593057": "theo"}
    events = []

    async def on_live(ev):
        events.append(ev)

    det._on_live = on_live
    client = _FakeStreamsClient([_FakeStream("146593057", "theo", "shipping code")])
    asyncio.run(det._poll_live_now(client, bot_uid="481015872"))

    assert len(events) == 1
    ev = events[0]
    # video_id must mirror the EventSub handler (broadcaster uid) so the app's
    # session dedupe treats poll-detected and event-detected go-lives as one.
    assert ev.video_id == "146593057"
    assert ev.channel == "theo"
    assert ev.title == "shipping code"
    assert client.calls[0]["type"] == "live"


def test_poll_live_now_no_targets_is_noop():
    import asyncio

    det = _twitch([WatchTarget(platform="twitch", handle="theo")])
    det._uid_to_login = {}
    client = _FakeStreamsClient([])
    asyncio.run(det._poll_live_now(client, bot_uid="1"))
    assert client.calls == []


def test_expected_sub_count_tracks_chat_router():
    det = _twitch([WatchTarget(platform="twitch", handle="a")])
    det._uid_to_login = {"1": "a", "2": "b"}
    assert det._expected_sub_count() == 4

    async def router(*a):
        return None

    det_chat = _twitch([WatchTarget(platform="twitch", handle="a")], chat_router=router)
    det_chat._uid_to_login = {"1": "a", "2": "b"}
    assert det_chat._expected_sub_count() == 6


def test_subscribe_all_ignores_duplicate_409():
    import asyncio
    from types import SimpleNamespace

    # Stub payload factories: twitchio is an optional extra, not a test dep.
    eventsub = SimpleNamespace(
        StreamOnlineSubscription=lambda **kw: ("online", kw),
        StreamOfflineSubscription=lambda **kw: ("offline", kw),
        ChatMessageSubscription=lambda **kw: ("chat", kw),
    )

    det = _twitch([WatchTarget(platform="twitch", handle="a")])
    det._uid_to_login = {"1": "a"}

    class _Client:
        def __init__(self):
            self.n = 0

        async def subscribe_websocket(self, **kw):
            self.n += 1
            raise RuntimeError("Request failed with status 409: subscription already exists")

    client = _Client()
    asyncio.run(det._subscribe_all(client, eventsub, bot_uid="9"))
    assert client.n == 2
