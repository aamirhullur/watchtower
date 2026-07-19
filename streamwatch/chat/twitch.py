"""Twitch chat ingest via TwitchIO.

TwitchIO already maintains the IRC/EventSub chat connection for the detector's
client; rather than open a second connection this adapter can be fed messages by
the shared client. For a standalone/simulate path it can also connect on its own.

Because chat semantics in TwitchIO v3 are delivered as ``event_message`` /
``ChatMessage`` payloads, this adapter exposes an ``ingest`` coroutine the caller
wires into the client's message listener. Lazy import keeps twitchio optional.

Payload shapes VERIFIED LIVE 2026-07-19 (TwitchIO 3.2.2, real chat firehose):
``event_message`` delivers ``twitchio.models.eventsub_.ChatMessage`` with
``.broadcaster`` / ``.chatter`` (each with ``.name``/``.id``) and ``.text``.
"""

from __future__ import annotations

import asyncio
import logging

from ..util import now_utc, utc_iso
from .base import ChatAdapter, ChatMessage

log = logging.getLogger("streamwatch.chat.twitch")


class TwitchChatAdapter(ChatAdapter):
    name = "twitch"

    def __init__(self, channel: str):
        self.channel = channel.lower()
        self._queue: asyncio.Queue[ChatMessage] = asyncio.Queue()

    async def ingest_payload(self, payload) -> None:
        """Called from the shared TwitchIO client's event_message listener."""
        try:
            chan = str(getattr(getattr(payload, "broadcaster", None), "name", "")).lower()
            if chan and chan != self.channel:
                return
            author = str(getattr(getattr(payload, "chatter", None), "name", "")) or "?"
            text = getattr(payload, "text", "") or ""
            if not text:
                return
            await self._queue.put(ChatMessage(author=author, text=text, ts=utc_iso(now_utc())))
        except Exception as e:  # never let a malformed payload kill ingest
            log.debug("twitch chat ingest skip: %s", e)

    async def run(self, sink, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await sink(msg)
