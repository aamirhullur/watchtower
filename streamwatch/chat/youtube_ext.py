"""YouTube chat ingest via an optional external NDJSON-emitting binary.

v1 has no native YouTube live-chat client. If ``youtube_chat_binary`` is set in
config, we spawn it with the video id/URL and read newline-delimited JSON from its
stdout, one object per chat message: ``{"author": "...", "text": "...", "ts": ...}``.
Absent binary => degrade cleanly to no YouTube chat.

This keeps the heavy YouTube-chat scraping concern out-of-process and swappable
(e.g. ``chat_downloader`` or a custom tool) without adding a dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging

from ..util import minimal_env, now_utc, utc_iso
from .base import ChatAdapter, ChatMessage

log = logging.getLogger("streamwatch.chat.youtube")


class YouTubeExternalChatAdapter(ChatAdapter):
    name = "youtube_ext"

    def __init__(self, binary: str, video_id: str, url: str):
        self.binary = binary
        self.video_id = video_id
        self.url = url

    @staticmethod
    def parse_line(line: str) -> ChatMessage | None:
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        text = str(obj.get("text") or obj.get("message") or "").strip()
        if not text:
            return None
        author = str(obj.get("author") or obj.get("name") or "?")
        ts = str(obj.get("ts") or obj.get("timestamp") or utc_iso(now_utc()))
        return ChatMessage(author=author, text=text, ts=ts)

    async def run(self, sink, stop: asyncio.Event) -> None:
        if not self.binary:
            log.info("no youtube_chat_binary configured; skipping YouTube chat")
            return
        argv = [self.binary, "--video-id", self.video_id, "--url", self.url]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=minimal_env(),  # external chat binary never needs process secrets
            )
        except FileNotFoundError:
            log.error("youtube_chat_binary not found: %s (degrading to no YT chat)", self.binary)
            return

        assert proc.stdout is not None
        try:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                if not raw:
                    break
                msg = self.parse_line(raw.decode("utf-8", "replace"))
                if msg:
                    await sink(msg)
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
