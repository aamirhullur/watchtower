"""Chat adapter interface.

An adapter streams live chat for one stream. It calls an async ``sink`` callback
with each ChatMessage; the pipeline persists it and extracts links. Adapters run
until their stop event is set (stream ended).
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass


@dataclass
class ChatMessage:
    author: str
    text: str
    ts: str  # ISO8601


class ChatAdapter(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def run(self, sink, stop: asyncio.Event) -> None:
        """Stream chat, calling ``await sink(ChatMessage)`` per message, until stop."""
        raise NotImplementedError
