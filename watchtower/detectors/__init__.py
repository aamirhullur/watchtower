"""Go-live detectors for YouTube and Twitch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

    from ..config import Config
    from .base import Detector


@dataclass
class LiveEvent:
    """Emitted when a watched channel goes live."""

    platform: str  # youtube | twitch
    channel: str  # handle / login
    title: str
    url: str
    video_id: str


@dataclass
class OfflineEvent:
    """Emitted when a watched channel goes offline."""

    platform: str  # youtube | twitch
    channel: str  # handle / login


def build_detectors(
    cfg: "Config", session: "aiohttp.ClientSession", *, chat_router=None
) -> list["Detector"]:
    """Build the per-platform detectors for the enabled watch targets.

    Owns the platform grouping so ``app.run`` just iterates the result: YouTube
    gets one detector per channel (independent /live polls); Twitch gets a single
    detector covering every Twitch target over one EventSub websocket, wired to
    ``chat_router``. The Twitch module (and its optional ``twitchio`` extra) is
    imported lazily and only when Twitch targets exist, so a YouTube-only
    deployment never needs it.
    """
    from .youtube import YouTubeDetector

    detectors: list[Detector] = []
    for t in cfg.watch:
        if t.platform == "youtube" and t.enabled:
            detectors.append(
                YouTubeDetector(
                    t.handle,
                    t.display(),
                    cfg.poll_interval_for(t),
                    session,
                    offline_confirmations=cfg.youtube_offline_confirmations,
                )
            )

    twitch_targets = [t for t in cfg.watch if t.platform == "twitch" and t.enabled]
    if twitch_targets:
        from .twitch import TwitchDetector

        detectors.append(TwitchDetector(cfg, twitch_targets, chat_router=chat_router))

    return detectors


__all__ = ["LiveEvent", "OfflineEvent", "build_detectors"]
