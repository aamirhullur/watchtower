"""Go-live detectors for YouTube and Twitch."""

from __future__ import annotations

from dataclasses import dataclass


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
    platform: str
    channel: str


__all__ = ["LiveEvent", "OfflineEvent"]
