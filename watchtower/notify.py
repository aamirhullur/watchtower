"""Neutral notification model.

The domain emits these adapter-agnostic payloads; a delivery adapter (currently
``discord.py``) renders each for its channel. Nothing here knows how they are
rendered or delivered: no embeds, wire formats, or size caps, only source-domain
terms. Swap in a different adapter and this model stays untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Find:
    """A concrete discovery surfaced from a source: a specifically named product,
    tool, or idea, with an optional deep link back to where it was mentioned."""

    name: str
    detail: str = ""
    deeplink: str = ""


@dataclass(frozen=True)
class GoLive:
    """A watched source just went live."""

    channel: str
    platform: str
    title: str
    url: str


@dataclass(frozen=True)
class RollingUpdate:
    """A mid-stream summary of the latest window: what happened since the last one,
    plus the links and finds pulled from it."""

    channel: str
    title: str
    url: str
    summary: str
    links: tuple[str, ...] = ()
    finds: tuple[Find, ...] = ()


@dataclass(frozen=True)
class Digest:
    """An end-of-stream digest. ``refined`` marks the higher-quality pass written
    from VOD captions that lands ~30 min after the final digest.

    ``finds`` carries the full deduped discovery list for the stream. It is part
    of the neutral payload (a future web/Slack sink would attach it to the digest
    itself); only the Discord adapter splits it into a standalone follow-up
    message because of its embed-field size cap.
    """

    channel: str
    title: str
    url: str
    summary: str
    links: tuple[str, ...] = ()
    refined: bool = False
    finds: tuple[Find, ...] = ()


@dataclass(frozen=True)
class WebhookTest:
    """A connectivity check ("does the webhook work?")."""


Notification = GoLive | RollingUpdate | Digest | WebhookTest
"""Any neutral notification the domain can emit to a delivery adapter."""


class NotificationSink(Protocol):
    """A delivery sink for neutral notifications.

    ``DiscordPoster`` (real webhooks) and ``DryRunPoster`` (prints embeds) both
    implement this structurally; the domain (summarizer, pipeline) depends on the
    Protocol, not a concrete poster. There is deliberately no fan-out/multiplexer
    sink: there is only ever one live sink today, and unused machinery is exactly
    what the audit flags elsewhere."""

    async def post(self, note: Notification) -> bool: ...
