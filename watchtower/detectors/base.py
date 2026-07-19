"""Detector interface.

A detector watches one or more channels on a platform and fires callbacks on
go-live / go-offline transitions. The shared contract (previously honoured only
by convention) is: ``run(on_live, on_offline, stop)`` where ``on_live`` is called
with a :class:`~watchtower.detectors.LiveEvent` and ``on_offline`` with an
:class:`~watchtower.detectors.OfflineEvent`, looping until ``stop`` is set.

YouTube runs one detector per channel (HTML polling); Twitch runs a single
detector covering every Twitch target over one EventSub websocket. See
``build_detectors`` in this package's ``__init__`` for that grouping.
"""

from __future__ import annotations

import abc
import asyncio


class Detector(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def run(self, on_live, on_offline, stop: asyncio.Event) -> None:
        """Poll/listen for transitions, calling ``on_live(LiveEvent)`` and
        ``on_offline(OfflineEvent)`` until ``stop`` is set."""
        raise NotImplementedError
