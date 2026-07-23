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

# Watchdog cadence: verify EventSub subscriptions against Helix and reconcile
# live state by polling. Twitch can drop a websocket and TwitchIO's automatic
# resubscribe can fail permanently (observed 2026-07-22: 400 "websocket
# transport session does not exist"), leaving a healthy-looking but deaf
# client. Helix is the authoritative view, not the client's local state.
WATCHDOG_INTERVAL_SEC = int(os.environ.get("WATCHTOWER_TWITCH_WATCHDOG_SEC", "300"))
# Consecutive watchdog failures before the process exits and systemd restarts
# it with a fresh client (last line of defense).
WATCHDOG_MAX_FAILURES = 3


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
            await self._subscribe_all(client, eventsub, bot_uid)

            # Catch streams that were already live before we started (EventSub
            # only delivers the go-live *transition*).
            await self._poll_live_now(client, bot_uid)

            # No client.start() needed: once subscribed, TwitchIO keeps the
            # EventSub websocket alive and dispatches events (verified live).
            # The watchdog loop below guards against silent subscription loss.
            await self._watchdog_loop(client, eventsub, bot_uid, stop)
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

    # ------------------------------------------------------------------ #
    def _expected_sub_count(self) -> int:
        per_uid = 3 if self.chat_router is not None else 2
        return len(self._uid_to_login) * per_uid

    async def _subscribe_all(self, client, eventsub, bot_uid: str) -> None:
        """(Re)create every EventSub websocket subscription.

        Safe to call on an already-subscribed client: Twitch answers 409 for
        duplicates, which we treat as success.
        """
        for uid in self._uid_to_login:
            payloads = [
                eventsub.StreamOnlineSubscription(broadcaster_user_id=uid),
                eventsub.StreamOfflineSubscription(broadcaster_user_id=uid),
            ]
            if self.chat_router is not None:
                payloads.append(
                    eventsub.ChatMessageSubscription(broadcaster_user_id=uid, user_id=bot_uid)
                )
            for payload in payloads:
                try:
                    await client.subscribe_websocket(payload=payload, token_for=bot_uid)
                except Exception as e:
                    if "409" in str(e) or "already exists" in str(e).lower():
                        continue
                    raise
            log.info(
                "twitch subscribed EventSub for uid=%s (chat=%s)",
                uid,
                self.chat_router is not None,
            )

    async def _poll_live_now(self, client, bot_uid: str) -> None:
        """Fire on_live for any target currently live, per Helix.

        Idempotent: LiveEvent.video_id mirrors the EventSub handler (broadcaster
        uid), and the app dedupes sessions on it. Covers already-live streams at
        startup and any go-live event lost while the websocket was deaf.
        """
        uids = list(self._uid_to_login)
        if not uids:
            return
        streams = client.fetch_streams(user_ids=uids, type="live", token_for=bot_uid)
        async for s in streams:
            uid = str(getattr(getattr(s, "user", None), "id", "") or "")
            login = self._uid_to_login.get(uid) or str(
                getattr(getattr(s, "user", None), "name", "") or ""
            ).lower()
            if not login:
                continue
            log.info("twitch %s LIVE (helix poll)", login)
            if self._on_live:
                await self._on_live(
                    LiveEvent(
                        platform="twitch",
                        channel=login,
                        title=getattr(s, "title", "") or f"{login} live",
                        url=f"https://www.twitch.tv/{login}",
                        video_id=uid or login,
                    )
                )

    async def _watchdog_loop(self, client, eventsub, bot_uid: str, stop: asyncio.Event) -> None:
        """Periodically verify subscriptions against Helix and reconcile live state.

        Escalation ladder: healthy -> resubscribe missing subs -> after
        WATCHDOG_MAX_FAILURES consecutive failed cycles, exit the process so
        systemd restarts us with a fresh client.
        """
        failures = 0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=WATCHDOG_INTERVAL_SEC)
                return
            except asyncio.TimeoutError:
                pass
            try:
                resp = await client.fetch_eventsub_subscriptions(
                    token_for=bot_uid, status="enabled"
                )
                enabled = 0
                async for sub in resp.subscriptions:
                    enabled += 1
                expected = self._expected_sub_count()
                if enabled < expected:
                    log.error(
                        "twitch watchdog: %d/%d EventSub subscriptions enabled; resubscribing",
                        enabled,
                        expected,
                    )
                    await self._subscribe_all(client, eventsub, bot_uid)
                # Reconcile regardless: catches go-lives missed while deaf.
                await self._poll_live_now(client, bot_uid)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                log.exception(
                    "twitch watchdog cycle failed (%d/%d): %s",
                    failures,
                    WATCHDOG_MAX_FAILURES,
                    e,
                )
                if failures >= WATCHDOG_MAX_FAILURES:
                    log.critical(
                        "twitch watchdog: %d consecutive failures; exiting for systemd restart",
                        failures,
                    )
                    os._exit(70)
