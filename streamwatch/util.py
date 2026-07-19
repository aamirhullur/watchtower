"""Small shared utilities: logging, time, URL extraction, VTT parsing."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone

log = logging.getLogger("streamwatch")


async def terminate_process(proc, grace: float = 5.0) -> None:
    """Terminate a subprocess, escalating to SIGKILL after ``grace`` seconds.

    Safe to call with ``None`` or an already-exited process. Used in every
    subprocess ``finally`` so task cancellation can never leak an orphaned
    downloader / ffmpeg / whisper child running forever.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


def minimal_env() -> dict[str, str]:
    """A scrubbed environment for NON-LLM subprocesses (yt-dlp, streamlink, ffmpeg,
    whisper-cli, external chat binary).

    Passing only PATH/HOME/LANG means these tools never inherit process secrets
    (Discord webhook, Twitch tokens, GROQ/NTFY keys, CLAUDE_CODE_OAUTH_TOKEN),
    which they have no need for. LLM subprocesses deliberately keep the full env.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    if "PATH" not in env:
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return env


def setup_logging(level: str = "INFO") -> None:
    """Configure structured, journald-friendly logging to stdout.

    journald already timestamps each line, so we keep the format compact but
    include level + logger name for filtering.
    """
    lvl = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(lvl)
    # aiohttp access logs are noisy; keep them at WARNING.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(timezone.utc).isoformat()


# URL matcher: http/https URLs, trimmed of common trailing punctuation.
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>\"'"


def extract_urls(text: str) -> list[str]:
    """Extract, normalise and de-duplicate URLs from a blob of text.

    Order-preserving so timeline ordering is stable.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(_TRAILING)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ---- WebVTT caption parsing -------------------------------------------------

_VTT_TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s+-->")
_VTT_TAG = re.compile(r"<[^>]+>")


def parse_vtt(text: str) -> str:
    """Parse WebVTT (e.g. yt-dlp auto-subs) into de-duplicated plain text.

    YouTube auto-captions repeat rolling lines across cues; we collapse
    consecutive duplicate lines so the transcript reads once through.
    """
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("WEBVTT"):
            continue
        if line.startswith(("NOTE", "STYLE", "REGION", "Kind:", "Language:")):
            continue
        if _VTT_TIMESTAMP.match(line):
            continue
        if line.isdigit():  # cue index
            continue
        # Strip inline timing tags like <00:00:01.000> and <c> styling.
        clean = _VTT_TAG.sub("", line).strip()
        if not clean:
            continue
        if lines and lines[-1] == clean:
            continue
        lines.append(clean)
    # Collapse fully-contained rolling duplicates (auto-caption artefact).
    collapsed: list[str] = []
    for line in lines:
        if collapsed and (line in collapsed[-1] or collapsed[-1] in line):
            # keep the longer of the two
            if len(line) > len(collapsed[-1]):
                collapsed[-1] = line
            continue
        collapsed.append(line)
    return " ".join(collapsed).strip()


def deep_link(url: str, platform: str, offset_seconds: int) -> str:
    """Build a timestamped VOD deep link for a discovery mention.

    Only YouTube is supported in v1: a watch URL gets a ``&t=<seconds>s`` fragment
    so the link lands right on (slightly before) the mention. Twitch VOD ids are
    unknown while the stream is live, so other platforms return "".

    The offset is rounded DOWN to the start of the chunk's minute and pulled back a
    30s margin (floored at 0), so the viewer lands just before the mention rather
    than in the middle of it.
    """
    if not url:
        return ""
    if platform != "youtube":
        return ""
    offset = max(0, (offset_seconds // 60) * 60 - 30)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={offset}s"


def truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
