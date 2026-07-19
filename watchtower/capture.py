"""Capture supervisor.

On go-live we pipe the live stream through a downloader into ffmpeg, producing
rolling mono 16 kHz WAV segments:

    <downloader>  |  ffmpeg -f segment -segment_time 60 ... genN/chunk_%05d.wav

* YouTube -> ``yt-dlp -o - <url>``  (bestaudio)
* Twitch  -> ``streamlink --stdout <url> audio_only,best``

The two processes are connected with an OS pipe. The downloader is supervised:
if it crashes while the stream is still live, we restart it into a **new
generation** subdir (so segment numbering never collides) with capped backoff.

A watcher task discovers *completed* segments (a segment is complete once any
later segment exists, or once capture stops) and pushes them onto ``chunks`` as
``Chunk(seq, path, started_at)`` for the transcriber. Chunks are deleted after
transcription unless ``capture.keep_chunks`` is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .config import CaptureConfig
from .util import minimal_env, now_utc, terminate_process, utc_iso

log = logging.getLogger("watchtower.capture")


@dataclass
class Chunk:
    seq: int
    path: Path
    started_at: str


def build_downloader_argv(cfg: CaptureConfig, platform: str, url: str) -> list[str]:
    if platform == "youtube":
        proxy = ["--proxy", cfg.proxy] if cfg.proxy else []
        cookies = ["--cookies", cfg.cookies_file] if cfg.cookies_file else []
        return [
            cfg.yt_dlp,
            "--quiet",
            "--no-warnings",
            "--no-part",
            *proxy,
            *cookies,
            "-f",
            "bestaudio/best",
            "-o",
            "-",
            url,
        ]
    if platform == "twitch":
        return [
            cfg.streamlink,
            "--stdout",
            "--twitch-disable-ads",
            url,
            "audio_only,best",
        ]
    raise ValueError(f"unknown platform for capture: {platform}")


def build_ffmpeg_segment_argv(cfg: CaptureConfig, out_pattern: str, from_stdin: bool = True) -> list[str]:
    src = ["-i", "pipe:0"] if from_stdin else []
    return [
        cfg.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        *src,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(cfg.sample_rate),
        "-f",
        "segment",
        "-segment_time",
        str(cfg.segment_seconds),
        "-reset_timestamps",
        "1",
        out_pattern,
    ]


def build_ffmpeg_file_argv(cfg: CaptureConfig, input_path: str, out_pattern: str) -> list[str]:
    """Chunk a local file as fast as the CPU allows (used by ``simulate``)."""
    return [
        cfg.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        input_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(cfg.sample_rate),
        "-f",
        "segment",
        "-segment_time",
        str(cfg.segment_seconds),
        "-reset_timestamps",
        "1",
        out_pattern,
    ]


def _chunk_sort_key(p: Path) -> tuple[int, int]:
    # genN/chunk_00042.wav -> (N, 42)
    gen = 0
    try:
        gen = int(p.parent.name.replace("gen", ""))
    except ValueError:
        gen = 0
    try:
        idx = int(p.stem.split("_")[-1])
    except ValueError:
        idx = 0
    return (gen, idx)


class CaptureSession:
    """Supervises capture for a single live stream into ``workdir``."""

    def __init__(self, cfg: CaptureConfig, platform: str, url: str, workdir: Path, health=None):
        self.cfg = cfg
        self.platform = platform
        self.url = url
        self.workdir = workdir
        self.health = health
        self.chunks: asyncio.Queue[Chunk] = asyncio.Queue()
        self._seq = 0
        self._emitted: set[tuple[int, int]] = set()
        self._generation = 0

    def _gen_dir(self) -> Path:
        d = self.workdir / f"gen{self._generation}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _scan_completed(self, final: bool) -> list[Path]:
        """Return newly-completed chunk paths in order.

        While running, the highest-keyed existing file is still being written, so
        it's excluded. On ``final`` flush, everything remaining is emitted.
        """
        all_chunks = sorted(self.workdir.glob("gen*/chunk_*.wav"), key=_chunk_sort_key)
        if not all_chunks:
            return []
        candidates = all_chunks if final else all_chunks[:-1]
        out: list[Path] = []
        for p in candidates:
            key = _chunk_sort_key(p)
            if key in self._emitted:
                continue
            if p.stat().st_size == 0:
                continue
            self._emitted.add(key)
            out.append(p)
        return out

    async def _watch_chunks(self, stop: asyncio.Event) -> None:
        interval = 1.0
        while not stop.is_set():
            for p in self._scan_completed(final=False):
                self._seq += 1
                await self.chunks.put(Chunk(seq=self._seq, path=p, started_at=utc_iso(now_utc())))
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        # final flush
        for p in self._scan_completed(final=True):
            self._seq += 1
            await self.chunks.put(Chunk(seq=self._seq, path=p, started_at=utc_iso(now_utc())))

    async def _run_pipeline_once(self) -> int:
        """Run one downloader|ffmpeg pipeline generation. Returns ffmpeg's rc.

        The whole body is wrapped in try/finally so that on ANY exit path —
        normal end, error, or task cancellation (CancelledError) — BOTH the
        downloader and ffmpeg are terminated (then killed after a grace period).
        Without this, cancelling the capture task leaked orphaned processes that
        kept the stream open forever.
        """
        out_pattern = str(self._gen_dir() / "chunk_%05d.wav")
        dl_argv = build_downloader_argv(self.cfg, self.platform, self.url)
        ff_argv = build_ffmpeg_segment_argv(self.cfg, out_pattern, from_stdin=True)
        # Media tools never need process secrets — pass a scrubbed env.
        env = minimal_env()

        read_fd: int | None
        write_fd: int | None
        read_fd, write_fd = os.pipe()
        downloader = None
        ffmpeg = None
        try:
            try:
                downloader = await asyncio.create_subprocess_exec(
                    *dl_argv, stdout=write_fd, stderr=asyncio.subprocess.DEVNULL, env=env
                )
            except FileNotFoundError:
                log.error("downloader binary not found: %s", dl_argv[0])
                return 127
            finally:
                os.close(write_fd)
                write_fd = None

            try:
                ffmpeg = await asyncio.create_subprocess_exec(
                    *ff_argv, stdin=read_fd, stderr=asyncio.subprocess.DEVNULL, env=env
                )
            except FileNotFoundError:
                log.error("ffmpeg not found: %s", ff_argv[0])
                return 127
            finally:
                os.close(read_fd)
                read_fd = None

            # ffmpeg exits when the downloader closes the pipe (stream ended/crashed).
            ff_rc = await ffmpeg.wait()
            log.info(
                "capture gen%d ended: downloader rc=%s ffmpeg rc=%s",
                self._generation,
                downloader.returncode,
                ff_rc,
            )
            return ff_rc
        finally:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            await terminate_process(ffmpeg)
            await terminate_process(downloader)

    async def run(self, stop: asyncio.Event) -> None:
        """Supervise capture until ``stop`` is set (stream ended)."""
        watcher = asyncio.create_task(self._watch_chunks(stop))
        backoff = self.cfg.restart_backoff_seconds
        try:
            while not stop.is_set():
                rc = await self._run_pipeline_once()
                if stop.is_set():
                    break
                # Downloader ended but stream may still be live -> restart. This is
                # an unexpected end; surface it to health so a capture crash-loop is
                # visible instead of silently churning through generations.
                if self.health is not None:
                    await self.health.record_failure(
                        "capture", f"pipeline gen{self._generation} ended rc={rc} while live"
                    )
                log.warning("capture pipeline ended while live; restarting in %ss", backoff)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                if stop.is_set():
                    break
                self._generation += 1
                backoff = min(backoff * 2, self.cfg.max_restart_backoff_seconds)
        finally:
            stop.set()
            await watcher

    def cleanup_chunk(self, chunk: Chunk) -> None:
        if self.cfg.keep_chunks:
            return
        try:
            chunk.path.unlink(missing_ok=True)
        except OSError as e:
            log.debug("could not delete chunk %s: %s", chunk.path, e)
