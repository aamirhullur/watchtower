"""Parser for yt-dlp YouTube live-chat replay files.

``yt-dlp --write-subs --sub-langs live_chat`` produces a ``*.live_chat.json``
file with **one JSON object per line** (NDJSON). Each line is a
``replayChatItemAction`` carrying a ``videoOffsetTimeMsec`` (how far into the VOD
the message appeared) and, for actual chat text, a nested
``liveChatTextMessageRenderer``. Non-text events (memberships, super-chats,
ticker/paid renderers, viewer-count pings) carry no text renderer and are ignored.

Parsing is deliberately defensive: YouTube nests these payloads deeply and the
exact shape drifts between yt-dlp versions, so we search for the interesting keys
anywhere in the object rather than hard-coding one path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("watchtower.chat.live_chat_replay")


@dataclass
class ChatReplayEvent:
    """One replayed chat message with its offset (seconds) into the VOD."""

    offset_s: float
    author: str
    text: str


def _find_first(obj, key):
    """Depth-first, document-order search for the first value stored under ``key``.

    Returns the value (which may be falsy) or ``None`` if the key never appears.
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_first(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_first(item, key)
            if found is not None:
                return found
    return None


def _runs_text(message) -> str:
    """Concatenate ``message.runs[].text``, skipping emoji runs that carry no text."""
    if not isinstance(message, dict):
        return ""
    runs = message.get("runs")
    if not isinstance(runs, list):
        # Some renderers use a plain simpleText message instead of runs.
        simple = message.get("simpleText")
        return str(simple).strip() if simple else ""
    parts: list[str] = []
    for run in runs:
        if isinstance(run, dict):
            txt = run.get("text")
            if txt:  # emoji-only runs have no "text" and are skipped
                parts.append(str(txt))
    return "".join(parts).strip()


def _offset_seconds(obj) -> float:
    """Extract the video offset in seconds. Tolerates str or int, any nesting."""
    rcia = obj.get("replayChatItemAction") if isinstance(obj, dict) else None
    raw = None
    if isinstance(rcia, dict):
        raw = rcia.get("videoOffsetTimeMsec")
    if raw is None:
        raw = _find_first(obj, "videoOffsetTimeMsec")
    if raw is None:
        return 0.0
    try:
        return float(raw) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def parse_live_chat_line(line: str) -> ChatReplayEvent | None:
    """Parse a single NDJSON line. Returns ``None`` for blank/non-text/invalid lines."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    renderer = _find_first(obj, "liveChatTextMessageRenderer")
    if not isinstance(renderer, dict):
        return None  # membership / ticker / super-chat / etc.: no text message

    text = _runs_text(renderer.get("message"))
    if not text:
        return None  # emoji-only or empty message

    author = ""
    author_name = renderer.get("authorName")
    if isinstance(author_name, dict):
        author = str(author_name.get("simpleText") or "").strip()
    if not author:
        author = str(renderer.get("authorExternalChannelId") or "?")

    return ChatReplayEvent(offset_s=_offset_seconds(obj), author=author, text=text)


def parse_live_chat_file(path: str | Path) -> list[ChatReplayEvent]:
    """Parse a whole live_chat NDJSON file into offset-sorted chat events."""
    events: list[ChatReplayEvent] = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        event = parse_live_chat_line(line)
        if event is not None:
            events.append(event)
    events.sort(key=lambda e: e.offset_s)  # stable: preserves in-file order per offset
    log.info("parsed %d chat message(s) from %s", len(events), path)
    return events
