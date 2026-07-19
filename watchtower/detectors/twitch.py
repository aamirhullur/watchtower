"""Twitch go-live detection via TwitchIO v3 EventSub over WebSocket.

The box is outbound-only behind Tailscale, so we use the **websocket** EventSub
transport (no public callback URL required).

Verified against TwitchIO 3.x docs (twitchio.dev, stable) and Twitch EventSub docs:

* ``twitchio.Client(client_id=..., client_secret=..., bot_id=...)``: app creds.
* Websocket EventSub **requires a user access token** (app tokens are rejected).
  We register one with ``client.add_token(token, refresh)`` from env-provided
  secrets, then subscribe with ``token_for=<user_id>``.
* ``client.subscribe_websocket(payload, as_bot=False, token_for=<uid>)`` where
  ``payload`` is ``eventsub.StreamOnlineSubscription(broadcaster_user_id=...)`` /
  ``eventsub.StreamOfflineSubscription(broadcaster_user_id=...)``.
* Event listeners registered via ``@client.listen()`` for
  ``event_stream_online`` / ``event_stream_offline``.

TwitchIO is an optional dependency (``pip install watchtower[twitch]``). Imports
are lazy so a YouTube-only deployment doesn't need it. See install.md for the
one-time user-token bootstrap.

VERIFIED LIVE 2026-07-19 against TwitchIO 3.2.2 + real Twitch EventSub (smoke
test on the VM): ``login()`` → ``add_token(token, refresh)`` (response carries
``user_id`` of the token owner) → ``subscribe_websocket(payload, token_for=<token
owner uid>)``. Subscriptions authorized by the *token owner's* uid, NOT the
broadcaster's. Chat arrives as ``event_message`` with a
``twitchio.models.eventsub_.ChatMessage`` payload (``.broadcaster``, ``.chatter``,
``.text``). No ``client.start()`` needed: the websocket lives once subscribed.
"""

from __future__ import annotations

import asyncio
import logging
import os

from ..config import Config, WatchTarget
from ..util import now_utc, utc_iso
from . import LiveEvent, OfflineEvent
from .base import Detector

log = logging.getLogger("watchtower.detectors.twitch")


class TwitchDetector(Detector):
    """Manages one TwitchIO client covering all configured Twitch targets."""

    name = "twitch"

    def __init__(self, cfg: Config, targets: list[WatchTarget], chat_router=None):
        self.cfg = cfg
        self.targets = targets
        # chat_router: async (login: str, author: str, text: str, ts: str) -> None
        self.chat_router = chat_router
        self._client = None  # twitchio.Client, lazily created
        self._on_live = None
        self._on_offline = None
        self._login_to_target: dict[str, WatchTarget] = {t.handle.lower(): t for t in targets}
        self._uid_to_login: dict[str, str] = {}

    def _env(self, name: str) -> str:
        val = os.environ.get(name, "")
        if not val:
            raise RuntimeError(f"${name} not set (required for Twitch detection)")
        return val

    async def run(self, on_live, on_offline, stop: asyncio.Event) -> None:
        self._on_live = on_live
        self._on_offline = on_offline
        try:
            import twitchio  # noqa: F401
            from twitchio import eventsub
        except ImportError:
            log.error(
                "twitchio not installed; Twitch targets disabled. "
                "Install with: pip install watchtower[twitch]"
            )
            await stop.wait()
            return

        client_id = self._env(self.cfg.twitch_client_id_env)
        client_secret = self._env(self.cfg.twitch_client_secret_env)
        user_token = self._env(self.cfg.twitch_bot_token_env)
        user_refresh = os.environ.get(self.cfg.twitch_bot_refresh_env, "")

        client = twitchio.Client(client_id=client_id, client_secret=client_secret)
        self._client = client

        # Register event listeners.
        async def _stream_online(payload) -> None:
            login = getattr(getattr(payload, "broadcaster", None), "name", None) or ""
            uid = str(getattr(getattr(payload, "broadcaster", None), "id", "") or "")
            login = login.lower() or self._uid_to_login.get(uid, "")
            target = self._login_to_target.get(login)
            title = ""
            # stream.online carries limited data; try to fetch title if available.
            title = getattr(payload, "title", "") or (target.display() if target else login)
            log.info("twitch %s LIVE", login)
            if self._on_live:
                await self._on_live(
                    LiveEvent(
                        platform="twitch",
                        channel=login,
                        title=title or f"{login} live",
                        url=f"https://www.twitch.tv/{login}",
                        video_id=uid or login,
                    )
                )

        async def _stream_offline(payload) -> None:
            uid = str(getattr(getattr(payload, "broadcaster", None), "id", "") or "")
            login = self._uid_to_login.get(uid) or (
                getattr(getattr(payload, "broadcaster", None), "name", "") or ""
            ).lower()
            log.info("twitch %s offline", login)
            if self._on_offline:
                await self._on_offline(OfflineEvent(platform="twitch", channel=login))

        async def _message(payload) -> None:
            if self.chat_router is None:
                return
            login = str(getattr(getattr(payload, "broadcaster", None), "name", "")).lower()
            author = str(getattr(getattr(payload, "chatter", None), "name", "")) or "?"
            text = getattr(payload, "text", "") or ""
            if not (login and text):
                return
            await self.chat_router(login, author, text, utc_iso(now_utc()))

        client.listen("event_stream_online")(_stream_online)
        client.listen("event_stream_offline")(_stream_offline)
        if self.chat_router is not None:
            client.listen("event_message")(_message)

        try:
            await client.login()
            # Register the user token used for websocket subscriptions. The
            # response identifies the token owner: the uid that authorizes
            # every subscription (verified live: token_for = token owner).
            resp = await client.add_token(user_token, user_refresh)
            bot_uid = str(getattr(resp, "user_id", "") or "")
            if not bot_uid:
                raise RuntimeError("could not resolve token owner user_id from add_token")
            log.info("twitch token bound to uid=%s login=%s", bot_uid, getattr(resp, "login", "?"))

            # Resolve broadcaster user ids from logins.
            logins = [t.handle for t in self.targets]
            users = await client.fetch_users(logins=logins)
            for u in users:
                login = str(getattr(u, "name", "")).lower()
                uid = str(getattr(u, "id", ""))
                if login and uid:
                    self._uid_to_login[uid] = login

            # Subscribe online + offline (+ chat, if routed) per broadcaster.
            for u in users:
                uid = str(getattr(u, "id", ""))
                if not uid:
                    continue
                await client.subscribe_websocket(
                    payload=eventsub.StreamOnlineSubscription(broadcaster_user_id=uid),
                    token_for=bot_uid,
                )
                await client.subscribe_websocket(
                    payload=eventsub.StreamOfflineSubscription(broadcaster_user_id=uid),
                    token_for=bot_uid,
                )
                if self.chat_router is not None:
                    await client.subscribe_websocket(
                        payload=eventsub.ChatMessageSubscription(
                            broadcaster_user_id=uid, user_id=bot_uid
                        ),
                        token_for=bot_uid,
                    )
                log.info(
                    "twitch subscribed EventSub for uid=%s (chat=%s)",
                    uid,
                    self.chat_router is not None,
                )

            # No client.start() needed: once subscribed, TwitchIO keeps the
            # EventSub websocket alive and dispatches events (verified live).
            await stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # keep the detector resilient; log & idle till stop
            log.exception("twitch detector error: %s", e)
            await stop.wait()
        finally:
            try:
                await client.close()
            except Exception:
                pass
