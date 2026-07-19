"""Per-stream pipeline: capture -> transcribe -> chat -> rolling updates -> digest.

A ``StreamSession`` owns one live occurrence of one channel. It wires together the
capture supervisor, transcriber, chat adapter and summarizer loop, then produces
the final digest on stream end. For YouTube it also schedules the post-stream
refined digest from VOD auto-captions.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from .capture import CaptureSession, Chunk, build_ffmpeg_file_argv
from .config import Config, WatchTarget
from .db import Database
from .detectors import LiveEvent
from .discord import DiscordPoster
from .health import HealthMonitor
from .notify import GoLive
from .stt.base import STTBackend, STTError
from .summarize import Summarizer
from .util import minimal_env, now_utc, parse_vtt, terminate_process, utc_iso

log = logging.getLogger("watchtower.pipeline")


class StreamSession:
    def __init__(
        self,
        cfg: Config,
        target: WatchTarget,
        event: LiveEvent,
        db: Database,
        stt: STTBackend,
        summarizer: Summarizer,
        poster: DiscordPoster,
        health: HealthMonitor,
        chat_adapter=None,
    ):
        self.cfg = cfg
        self.target = target
        self.event = event
        self.db = db
        self.stt = stt
        self.summarizer = summarizer
        self.poster = poster
        self.health = health
        self.chat_adapter = chat_adapter
        self.stop = asyncio.Event()
        self.stream_id: int | None = None
        self._workdir: Path | None = None
        # Chat from any source (external binary adapter OR pushed twitch payloads)
        # funnels through this bounded queue and is drained by _chat_loop. Bounding
        # it prevents an unbounded memory blow-up if a chat firehose outruns the
        # DB writer; overflow drops the oldest message.
        self.chat_queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, cfg.chat_queue_max))
        self._chat_dropped = 0

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        # Reconcile with any pre-existing live row (restart safety).
        existing = await self.db.find_active_stream(self.event.platform, self.event.video_id)
        if existing is not None:
            self.stream_id = int(existing["id"])
            log.info("resuming existing stream row %s", self.stream_id)
        else:
            self.stream_id = await self.db.open_stream(
                platform=self.event.platform,
                channel=self.event.channel,
                title=self.event.title,
                url=self.event.url,
                video_id=self.event.video_id,
            )
            await self._announce()

        self._workdir = Path(self.cfg.capture.workdir) / f"{self.event.platform}-{self.stream_id}"
        self._workdir.mkdir(parents=True, exist_ok=True)

        capture = CaptureSession(
            self.cfg.capture, self.event.platform, self.event.url, self._workdir, health=self.health
        )

        tasks = [
            asyncio.create_task(capture.run(self.stop), name="capture"),
            asyncio.create_task(self._transcribe_loop(capture), name="transcribe"),
            asyncio.create_task(self._summarize_loop(), name="summarize"),
            asyncio.create_task(self._chat_loop(), name="chat"),
        ]
        if self.chat_adapter is not None:
            tasks.append(asyncio.create_task(self._run_chat_adapter(), name="chat-src"))
        # If any session task dies unexpectedly, log it and stop the whole session
        # rather than limping along looking healthy while chunks pile up.
        for t in tasks:
            t.add_done_callback(self._on_task_done)

        try:
            await self.stop.wait()
        finally:
            self.stop.set()
            # capture flushes remaining chunks into its queue; give transcriber a
            # moment to drain before cancelling.
            await asyncio.sleep(0.1)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        await self._finalize()

    def request_stop(self) -> None:
        self.stop.set()

    def _on_task_done(self, task: asyncio.Task) -> None:
        """Done-callback for session tasks: surface unexpected crashes and stop the
        session so a dead loop doesn't leave the session looking healthy."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("session task %s crashed: %r; stopping session", task.get_name(), exc)
            self.stop.set()

    # ------------------------------------------------------------------ #
    async def _announce(self) -> None:
        note = GoLive(
            channel=self.target.display(),
            platform=self.event.platform,
            title=self.event.title,
            url=self.event.url,
        )
        await self.poster.post(note)

    async def _transcribe_loop(self, capture: CaptureSession) -> None:
        assert self.stream_id is not None
        seg = self.cfg.capture.segment_seconds
        while True:
            try:
                chunk: Chunk = await asyncio.wait_for(capture.chunks.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if self.stop.is_set() and capture.chunks.empty():
                    break
                continue

            # Guard the whole iteration: a DB/sqlite error (or anything else) must
            # not silently kill the loop and let chunks pile up unnoticed.
            try:
                # Lag handling: queue depth in seconds of audio behind.
                behind = capture.chunks.qsize() * seg
                if behind > self.cfg.stt.lag_warn_seconds:
                    log.warning("transcription lagging capture by ~%ss", behind)
                    if self.cfg.stt.skip_when_lagging:
                        log.warning("skipping chunk seq=%s due to lag", chunk.seq)
                        capture.cleanup_chunk(chunk)
                        continue

                try:
                    text = await self.stt.transcribe(chunk.path)
                    self.health.record_success("transcribe")
                except STTError as e:
                    await self.health.record_failure("transcribe", str(e))
                    capture.cleanup_chunk(chunk)
                    continue

                if text:
                    await self.db.add_chunk(self.stream_id, chunk.seq, chunk.started_at, text)
                    await self.db.add_links_from(self.stream_id, text, "transcript", chunk.started_at)
                capture.cleanup_chunk(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("transcribe loop iteration failed: %s", e)
                await self.health.record_failure("transcribe", str(e))

    async def enqueue_chat(self, msg) -> None:
        """Feed a ChatMessage into the session (used by adapter sinks and the
        Twitch chat router in the app).

        Non-blocking with drop-oldest overflow: if the bounded queue is full we
        discard the oldest buffered message so a chat firehose can never block the
        producer or grow memory without bound.
        """
        try:
            self.chat_queue.put_nowait(msg)
            return
        except asyncio.QueueFull:
            pass
        try:
            self.chat_queue.get_nowait()  # drop oldest
        except asyncio.QueueEmpty:
            pass
        try:
            self.chat_queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass
        self._chat_dropped += 1
        if self._chat_dropped == 1 or self._chat_dropped % 100 == 0:
            log.warning("chat queue full; dropped %d oldest message(s) so far", self._chat_dropped)

    async def _run_chat_adapter(self) -> None:
        try:
            await self.chat_adapter.run(self.enqueue_chat, self.stop)
        except Exception as e:
            log.warning("chat adapter error: %s", e)

    async def _chat_loop(self) -> None:
        assert self.stream_id is not None
        while True:
            try:
                msg = await asyncio.wait_for(self.chat_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if self.stop.is_set() and self.chat_queue.empty():
                    break
                continue
            try:
                await self.db.add_chat(self.stream_id, msg.author, msg.text, msg.ts)
                await self.db.add_links_from(self.stream_id, msg.text, "chat", msg.ts)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("chat loop iteration failed: %s", e)
                await self.health.record_failure("chat", str(e))

    async def _summarize_loop(self) -> None:
        assert self.stream_id is not None
        interval = self.cfg.update_interval_for(self.target) * 60
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=interval)
                break  # stop set -> exit; final digest handled in _finalize
            except asyncio.TimeoutError:
                pass
            try:
                await self.summarizer.post_update(self.stream_id, self.target)
                self.health.record_success("summarize")
            except Exception as e:
                await self.health.record_failure("summarize", str(e))

    async def _finalize(self) -> None:
        assert self.stream_id is not None
        log.info("stream %s finalizing", self.stream_id)
        await self.db.end_stream(self.stream_id)
        try:
            await self.summarizer.post_digest(self.stream_id, self.target, refined=False)
        except Exception as e:
            log.warning("final digest failed: %s", e)

    # ------------------------------------------------------------------ #
    async def run_refined_digest(self) -> None:
        """YouTube only: after a delay, pull VOD auto-captions and repost a
        refined digest. Retries once, then gives up quietly."""
        if self.event.platform != "youtube" or not self.cfg.refined_digest.enabled:
            return
        assert self.stream_id is not None
        rd = self.cfg.refined_digest
        delays = [rd.delay_minutes, rd.retry_delay_minutes]
        for attempt, delay in enumerate(delays, start=1):
            await asyncio.sleep(delay * 60)
            transcript = await self._fetch_vod_captions(self.event.video_id, self.event.url)
            if transcript:
                await self.db.replace_transcript(self.stream_id, transcript)
                await self.db.add_links_from(self.stream_id, transcript, "transcript", utc_iso(now_utc()))
                await self.summarizer.post_digest(self.stream_id, self.target, refined=True)
                log.info("stream %s refined digest posted (attempt %d)", self.stream_id, attempt)
                return
            log.info("refined captions not ready for %s (attempt %d)", self.event.video_id, attempt)
        log.info("refined digest gave up for %s", self.event.video_id)

    async def _fetch_vod_captions(self, video_id: str, url: str) -> str:
        """Run yt-dlp to download auto-subs (works on unlisted VODs) and parse VTT."""
        with tempfile.TemporaryDirectory(prefix="watchtower-subs-") as tmp:
            out_tmpl = str(Path(tmp) / "%(id)s")
            argv = [
                self.cfg.capture.yt_dlp,
                *(["--proxy", self.cfg.capture.proxy] if self.cfg.capture.proxy else []),
                *(["--cookies", self.cfg.capture.cookies_file] if self.cfg.capture.cookies_file else []),
                "--skip-download",
                "--write-auto-subs",
                "--sub-format",
                "vtt",
                "--sub-langs",
                "en.*",
                "-o",
                out_tmpl,
                url or f"https://www.youtube.com/watch?v={video_id}",
            ]
            timeout = self.cfg.subprocess_timeout()
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=minimal_env(),
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except FileNotFoundError:
                log.error("yt-dlp not found for caption fetch")
                return ""
            except asyncio.TimeoutError:
                log.warning("yt-dlp caption fetch timed out after %ss", timeout)
                await self.health.record_failure("refined", f"caption fetch timeout after {timeout}s")
                return ""
            finally:
                # Never leak the yt-dlp child on timeout/cancellation.
                await terminate_process(proc)
            if proc.returncode != 0:
                log.debug("yt-dlp subs rc=%s: %s", proc.returncode, stderr.decode("utf-8", "replace")[:200])
            vtts = list(Path(tmp).glob("*.vtt"))
            if not vtts:
                return ""
            return parse_vtt(vtts[0].read_text(encoding="utf-8", errors="replace"))


async def chunk_local_file(cfg: Config, input_path: str, workdir: Path) -> list[Chunk]:
    """Chunk a local audio/video file with ffmpeg (used by ``simulate``).

    Returns the completed chunk list in order. Runs faster-than-realtime.
    """
    gen = workdir / "gen0"
    gen.mkdir(parents=True, exist_ok=True)
    out_pattern = str(gen / "chunk_%05d.wav")
    argv = build_ffmpeg_file_argv(cfg.capture, input_path, out_pattern)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=minimal_env(),
        )
        _, stderr = await proc.communicate()
    finally:
        await terminate_process(proc)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg chunking failed: {stderr.decode('utf-8', 'replace')[:300]}")
    chunks: list[Chunk] = []
    for i, p in enumerate(sorted(gen.glob("chunk_*.wav"))):
        chunks.append(Chunk(seq=i + 1, path=p, started_at=utc_iso(now_utc())))
    return chunks
