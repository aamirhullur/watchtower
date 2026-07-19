"""Application wiring + run loop.

Assembles shared resources (DB, Discord poster, LLM, STT, health) and per-channel
detectors, routes go-live/offline events into ``StreamSession`` lifecycles, and
handles graceful SIGTERM/SIGINT shutdown (stop captures, flush, exit).
"""

from __future__ import annotations

import asyncio
import logging
import signal

import aiohttp

from .chat.base import ChatMessage
from .chat.youtube_ext import YouTubeExternalChatAdapter
from .config import Config, WatchTarget
from .db import Database
from .detectors import LiveEvent
from .detectors.youtube import YouTubeDetector
from .discord import DiscordPoster
from .health import HealthMonitor
from .llm import build_digest_llm, build_llm
from .pipeline import StreamSession
from .stt import build_stt
from .summarize import Summarizer

log = logging.getLogger("watchtower.app")


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop = asyncio.Event()
        self._sessions: dict[tuple[str, str], StreamSession] = {}
        self._session_tasks: set[asyncio.Task] = set()
        self._refined_tasks: set[asyncio.Task] = set()
        self._targets: dict[tuple[str, str], WatchTarget] = {}

    async def run(self) -> None:
        cfg = self.cfg
        db = Database(cfg.state_db)
        await db.connect()

        # Dedicated empty working dir for the LLM CLI subprocess (prompt-injection
        # containment; see LlmConfig.workdir / claude_cli.build_argv).
        try:
            cfg.llm_workdir().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("could not create llm workdir %s: %s", cfg.llm_workdir(), e)

        # Retention prune at startup (daily thereafter via _retention_loop).
        try:
            pruned = await db.prune_old_streams(cfg.retention_days)
            if pruned:
                log.info("retention: pruned data for %d old stream(s)", pruned)
        except Exception as e:
            log.warning("retention prune failed: %s", e)

        session = aiohttp.ClientSession()
        try:
            health = HealthMonitor(cfg.health, session=session)
            poster = DiscordPoster(cfg, session=session, health=health)
            stt = build_stt(cfg.stt)
            llm = build_llm(cfg.llm)
            summarizer = Summarizer(cfg, db, llm, poster, digest_llm=build_digest_llm(cfg.llm))

            self._db = db
            self._stt = stt
            self._summarizer = summarizer
            self._poster = poster
            self._health = health

            for t in cfg.watch:
                if t.enabled:
                    self._targets[(t.platform, t.handle.lower())] = t

            self._install_signal_handlers()

            detector_tasks: list[asyncio.Task] = []

            # Heartbeat.
            detector_tasks.append(asyncio.create_task(health.heartbeat_loop(self.stop), name="heartbeat"))

            # Daily retention prune.
            detector_tasks.append(asyncio.create_task(self._retention_loop(db), name="retention"))

            # YouTube detectors (one per channel).
            for t in cfg.watch:
                if t.platform == "youtube" and t.enabled:
                    det = YouTubeDetector(
                        t.handle,
                        t.display(),
                        cfg.poll_interval_for(t),
                        session,
                        offline_confirmations=cfg.youtube_offline_confirmations,
                    )
                    detector_tasks.append(
                        asyncio.create_task(
                            det.run(self._on_live, self._on_offline, self.stop),
                            name=f"yt:{t.handle}",
                        )
                    )

            # Twitch: single detector covering all twitch targets.
            twitch_targets = [t for t in cfg.watch if t.platform == "twitch" and t.enabled]
            if twitch_targets:
                from .detectors.twitch import TwitchDetector

                det = TwitchDetector(cfg, twitch_targets, chat_router=self._twitch_chat_router)
                detector_tasks.append(
                    asyncio.create_task(
                        det.run(self._on_live, self._on_offline, self.stop),
                        name="twitch",
                    )
                )

            log.info(
                "watchtower running: %d youtube, %d twitch targets; stt=%s llm=%s",
                sum(1 for t in cfg.watch if t.platform == "youtube" and t.enabled),
                len(twitch_targets),
                cfg.stt.backend,
                cfg.llm.backend,
            )

            await self.stop.wait()
            log.info("shutdown requested; stopping sessions")

            # Ask all live sessions to stop, then wait for them (bounded).
            for s in list(self._sessions.values()):
                s.request_stop()
            if self._session_tasks:
                await asyncio.wait(self._session_tasks, timeout=60)
            for t in [*detector_tasks, *self._session_tasks, *self._refined_tasks]:
                t.cancel()
            await asyncio.gather(
                *detector_tasks, *self._session_tasks, *self._refined_tasks, return_exceptions=True
            )
        finally:
            await session.close()
            await db.close()
            log.info("watchtower stopped")

    # ------------------------------------------------------------------ #
    async def _retention_loop(self, db: Database) -> None:
        """Prune data for streams ended past the retention window, once a day."""
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=86400)
                break  # stop set
            except asyncio.TimeoutError:
                pass
            try:
                pruned = await db.prune_old_streams(self.cfg.retention_days)
                if pruned:
                    log.info("retention: pruned data for %d old stream(s)", pruned)
            except Exception as e:
                log.warning("retention prune failed: %s", e)

    # ------------------------------------------------------------------ #
    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown, sig)
            except NotImplementedError:  # pragma: no cover (non-unix)
                pass

    def _request_shutdown(self, sig) -> None:
        log.info("received signal %s", getattr(sig, "name", sig))
        self.stop.set()

    # ------------------------------------------------------------------ #
    async def _on_live(self, event: LiveEvent) -> None:
        key = (event.platform, event.channel.lower())
        existing = self._sessions.get(key)
        if existing is not None:
            if existing.event.video_id == event.video_id:
                log.debug("already tracking %s; ignoring duplicate go-live", key)
                return
            # A new broadcast id arrived for a channel we're already tracking: the
            # old stream ended and a fresh one started. Stop the old session cleanly
            # (that fires its digest + schedules its refined digest) and rotate in a
            # new session for the new id, rather than wedging on the ended URL.
            log.info(
                "%s new video id %s (was %s); rotating session",
                key,
                event.video_id,
                existing.event.video_id,
            )
            existing.request_stop()
            # Free the key so the new session can claim it; the old task finalizes
            # itself and, thanks to the guard in _run_session, won't evict us.
            self._sessions.pop(key, None)
        target = self._targets.get(key) or WatchTarget(platform=event.platform, handle=event.channel)

        chat_adapter = None
        if event.platform == "youtube" and self.cfg.youtube_chat_binary:
            chat_adapter = YouTubeExternalChatAdapter(
                self.cfg.youtube_chat_binary, event.video_id, event.url
            )

        sess = StreamSession(
            self.cfg,
            target,
            event,
            self._db,
            self._stt,
            self._summarizer,
            self._poster,
            self._health,
            chat_adapter=chat_adapter,
        )
        self._sessions[key] = sess
        task = asyncio.create_task(self._run_session(key, sess), name=f"session:{key}")
        self._session_tasks.add(task)
        task.add_done_callback(self._session_tasks.discard)

    async def _run_session(self, key: tuple[str, str], sess: StreamSession) -> None:
        try:
            await sess.run()
        except Exception as e:
            log.exception("session %s crashed: %s", key, e)
        finally:
            # Only evict ourselves. A rotated-in newer session may already own the
            # key (see _on_live video-id rotation).
            if self._sessions.get(key) is sess:
                self._sessions.pop(key, None)
        # Schedule refined digest (YouTube) unless we're shutting down.
        if not self.stop.is_set() and key[0] == "youtube":
            rt = asyncio.create_task(sess.run_refined_digest(), name=f"refined:{key}")
            self._refined_tasks.add(rt)
            rt.add_done_callback(self._refined_tasks.discard)

    async def _on_offline(self, platform: str, channel: str) -> None:
        key = (platform, channel.lower())
        sess = self._sessions.get(key)
        if sess is not None:
            log.info("stopping session for %s (offline)", key)
            sess.request_stop()

    async def _twitch_chat_router(self, login: str, author: str, text: str, ts: str) -> None:
        sess = self._sessions.get(("twitch", login.lower()))
        if sess is not None:
            await sess.enqueue_chat(ChatMessage(author=author, text=text, ts=ts))


async def run_app(cfg: Config) -> None:
    await App(cfg).run()
