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
