"""Window assembly + summarization + Discord posting.

Pure helpers (window assembly, emptiness check, prompt building, stats fallback)
are unit-tested. The ``Summarizer`` glues DB reads -> LLM -> Discord post and
records high-water marks so consecutive updates don't repeat content.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field

from .config import Config, WatchTarget
from .db import Cursor, Database
from .discord import DiscordPoster
from .llm.base import LLMBackend
from .notify import Digest, Find, FindsRecap, RollingUpdate
from .util import deep_link, extract_urls, truncate

log = logging.getLogger("streamwatch.summarize")

# Upper bound on chat rows loaded for a digest (prompt uses the first 400).
DIGEST_CHAT_LIMIT = 500
# A digest prompt can hold this much transcript. Multi-hour streams far exceed it
# (4 h of speech ≈ 140k chars), so anything longer is map-reduced first: the
# transcript is condensed segment-by-segment through the LLM, then the digest is
# written from the condensations — otherwise the digest only ever sees the head
# of the stream and silently drops the rest.
DIGEST_TRANSCRIPT_CAP = 24000
DIGEST_SEGMENT_CHARS = 18000
# Cap concurrent segment-condense LLM calls (each is a CLI subprocess).
DIGEST_SEGMENT_CONCURRENCY = 4
# Max discoveries stored/shown per rolling window.
FINDS_PER_WINDOW_CAP = 10
# Max deduped discoveries listed in a digest.
FINDS_DIGEST_CAP = 25


@dataclass
class Window:
    """A slice of new content since the last update."""

    transcript: str = ""
    chat_lines: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    chunk_count: int = 0
    chat_count: int = 0
    cursor: Cursor = field(default_factory=Cursor)
    # Seq of the first transcript chunk in this window (-1 if none). Used to compute
    # the window's starting stream offset for timestamped deep links.
    first_seq: int = -1

    @property
    def transcript_chars(self) -> int:
        return len(self.transcript)


def assemble_window(
    chunk_rows: list[sqlite3.Row],
    chat_rows: list[sqlite3.Row],
    prev: Cursor,
) -> Window:
    """Build a Window from new transcript chunks + chat rows.

    Advances the cursor to the last seen chunk seq / chat id. Extracts links from
    both transcript and chat text.
    """
    transcript_parts: list[str] = []
    last_seq = prev.last_chunk_seq
    first_seq = chunk_rows[0]["seq"] if chunk_rows else -1
    for r in chunk_rows:
        text = (r["text"] or "").strip()
        if text:
            transcript_parts.append(text)
        last_seq = max(last_seq, r["seq"])

    chat_lines: list[str] = []
    last_chat = prev.last_chat_id
    for r in chat_rows:
        author = r["author"] or "?"
        text = (r["text"] or "").strip()
        if text:
            chat_lines.append(f"{author}: {text}")
        last_chat = max(last_chat, r["id"])

    transcript = " ".join(transcript_parts).strip()
    link_source = transcript + "\n" + "\n".join(chat_lines)
    links = extract_urls(link_source)

    return Window(
        transcript=transcript,
        chat_lines=chat_lines,
        links=links,
        chunk_count=len(chunk_rows),
        chat_count=len(chat_rows),
        cursor=Cursor(last_chunk_seq=last_seq, last_chat_id=last_chat),
        first_seq=first_seq,
    )


def window_is_empty(window: Window, min_transcript_chars: int = 40) -> bool:
    """True if the window has essentially nothing worth posting.

    A window with only a couple of chat messages and no transcript is treated as
    empty to avoid spamming Discord during quiet stretches.
    """
    if window.transcript_chars >= min_transcript_chars:
        return False
    if window.chat_count >= 5:
        return False
    if window.links:
        return False
    return True


def _links_block(links: list[str], limit: int = 30) -> str:
    if not links:
        return "(none)"
    return "\n".join(links[:limit])


def build_update_prompt(*, channel: str, title: str, window: Window, style: str) -> str:
    chat = "\n".join(window.chat_lines[:200]) or "(no chat captured)"
    return (
        f"You are summarizing a live stream in progress for a Discord update.\n"
        f"{style}\n\n"
        f"Channel: {channel}\nStream title: {title}\n\n"
        f"Write a short update (3-6 bullets) covering what has happened since the last update: "
        f"key topics, notable moments, and any products/tools/links mentioned. "
        f"Do not preamble; output only the bullets.\n\n"
        f"=== NEW TRANSCRIPT ===\n{window.transcript or '(no speech transcribed)'}\n\n"
        f"=== NEW CHAT ===\n{chat}\n\n"
        f"=== LINKS MENTIONED ===\n{_links_block(window.links)}\n"
    )


def build_digest_prompt(
    *, channel: str, title: str, transcript: str, chat_lines: list[str], links: list[str], style: str, refined: bool
) -> str:
    kind = "refined final digest (from higher-quality VOD captions)" if refined else "final digest"
    chat = "\n".join(chat_lines[:400]) or "(no chat captured)"
    return (
        f"You are writing the {kind} of a completed live stream for Discord.\n"
        f"{style}\n\n"
        f"Channel: {channel}\nStream title: {title}\n\n"
        f"Produce:\n"
        f"1. A 2-3 sentence overview.\n"
        f"2. Main topics / timeline (bullets).\n"
        f"3. All products, tools, and links mentioned (bullets).\n"
        f"Output only the digest.\n\n"
        f"=== FULL TRANSCRIPT ===\n{truncate(transcript, DIGEST_TRANSCRIPT_CAP) or '(no speech transcribed)'}\n\n"
        f"=== CHAT ===\n{truncate(chat, 8000)}\n\n"
        f"=== LINKS ===\n{_links_block(links, limit=60)}\n"
    )


def build_segment_condense_prompt(
    *, channel: str, title: str, seg_index: int, seg_total: int, segment: str, style: str
) -> str:
    return (
        f"You are condensing part {seg_index} of {seg_total} of a live stream transcript; "
        f"the condensed parts will later be merged into a final digest.\n"
        f"{style}\n\n"
        f"Channel: {channel}\nStream title: {title}\n\n"
        f"Condense this transcript segment into 4-8 dense bullets. Preserve every specific "
        f"name exactly: products, hardware models, tools, prices, links, people, and "
        f"announcements. Do not preamble; output only the bullets.\n\n"
        f"=== TRANSCRIPT SEGMENT ({seg_index}/{seg_total}) ===\n{segment}\n"
    )


def build_finds_prompt(*, window: Window, style: str) -> str:
    """Prompt the update LLM to extract concrete, named discoveries as JSON."""
    chat = "\n".join(window.chat_lines[:200]) or "(no chat captured)"
    return (
        "You are extracting concrete discoveries from a segment of a live stream.\n"
        f"{style}\n\n"
        "From the transcript and chat below, extract ONLY specifically named things "
        "worth discovering: products, hardware, tools, software, benchmarks, games, "
        "books, media, services, and notable tips. Include a thing only if it is "
        "concrete and specifically named so a viewer could look it up later. Ignore "
        "vague mentions, generic topics, and anything not explicitly named.\n\n"
        "Return a JSON array where each element has exactly these keys:\n"
        '{"type": "product|tool|benchmark|game|media|tip|other", '
        '"name": "the specific name", '
        '"detail": "one sentence of what was said about it", '
        '"sentiment": "positive|negative|neutral"}\n\n'
        "Return [] if there are no such discoveries. Output ONLY the JSON array — no "
        "prose, no explanation, no code fences.\n\n"
        f"=== TRANSCRIPT ===\n{window.transcript or '(no speech transcribed)'}\n\n"
        f"=== CHAT ===\n{chat}\n"
    )


def _first_json_array(text: str) -> str | None:
    """Return the substring from the first ``[`` to its matching ``]`` (or None).

    Bracket-depth aware and string-aware so brackets inside string values don't
    prematurely close the array. Lets us pull a JSON array out of a response that
    wrapped it in prose or a code fence.
    """
    start = (text or "").find("[")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_finds(text: str, cap: int = FINDS_PER_WINDOW_CAP) -> list[dict]:
    """Robustly parse a finds LLM response into a list of validated find dicts.

    Extracts the first JSON array from the response, json.loads it, and keeps only
    entries that are objects with non-empty ``type`` and ``name``. Malformed
    entries are skipped; a non-JSON / arrayless response yields ``[]``. Capped.
    """
    raw = _first_json_array(text or "")
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ftype = entry.get("type")
        name = entry.get("name")
        if not isinstance(ftype, str) or not isinstance(name, str):
            continue
        ftype, name = ftype.strip(), name.strip()
        if not ftype or not name:
            continue
        detail = entry.get("detail")
        detail = detail.strip() if isinstance(detail, str) else ""
        sentiment = entry.get("sentiment")
        sentiment = sentiment.strip() if isinstance(sentiment, str) else "neutral"
        out.append({"type": ftype, "name": name, "detail": detail, "sentiment": sentiment or "neutral"})
        if len(out) >= cap:
            break
    return out


def dedupe_finds(rows, url: str, platform: str, cap: int = FINDS_DIGEST_CAP) -> list[dict]:
    """Dedupe stored find rows by name (case-insensitive, keep first) and attach a
    timestamped deep link for each. Accepts sqlite Rows or dicts. Capped."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        name = (r["name"] or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "name": name,
                "detail": (r["detail"] or "").strip(),
                "deeplink": deep_link(url, platform, r["offset_seconds"] or 0),
            }
        )
        if len(out) >= cap:
            break
    return out


def build_stats_summary(window: Window, *, elapsed_label: str = "") -> str:
    """Fallback (no-LLM or LLM failure) update body: plain stats + transcript peek."""
    parts = [
        f"**{window.chunk_count}** new transcript chunk(s), "
        f"**{window.chat_count}** chat message(s)."
    ]
    if elapsed_label:
        parts[0] = elapsed_label + " — " + parts[0]
    if window.transcript:
        parts.append("> " + truncate(window.transcript, 500))
    if window.links:
        parts.append("**Links:**\n" + "\n".join(f"• {u}" for u in window.links[:10]))
    return "\n\n".join(parts)


class Summarizer:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        llm: LLMBackend,
        poster: DiscordPoster,
        digest_llm: LLMBackend | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        # Digests (final + refined + segment condensing) may use a stronger model.
        self.digest_llm = digest_llm or llm
        self.poster = poster

    async def post_update(self, stream_id: int, target: WatchTarget) -> bool:
        """Assemble the window since the last update and post a rolling update.

        Returns True if an update was posted, False if skipped (empty window).
        """
        stream = await self.db.get_stream(stream_id)
        if stream is None:
            return False
        prev = await self.db.last_cursor(stream_id)
        chunk_rows = await self.db.chunks_since(stream_id, prev.last_chunk_seq)
        chat_rows = await self.db.chat_since(stream_id, prev.last_chat_id)
        window = assemble_window(chunk_rows, chat_rows, prev)

        if window_is_empty(window):
            log.info("stream %s: window empty, skipping update", stream_id)
            return False

        summary = await self._summary_text(
            build_update_prompt(
                channel=target.display(),
                title=stream["title"] or "",
                window=window,
                style=self.cfg.llm.style,
            ),
            fallback=build_stats_summary(window),
        )

        # Extract structured discoveries from the same window (best-effort: never
        # let a finds failure block the update).
        finds = await self._extract_finds(stream_id, stream, window)

        note = RollingUpdate(
            channel=target.display(),
            title=stream["title"] or "",
            url=stream["url"] or "",
            summary=summary,
            links=tuple(window.links),
            finds=tuple(Find(**f) for f in finds),
        )
        ok = await self.poster.post(note)
        # Advance cursor regardless of Discord success so we don't re-summarize.
        await self.db.record_update(stream_id, "update", window.cursor)
        return ok

    async def post_digest(self, stream_id: int, target: WatchTarget, *, refined: bool = False) -> bool:
        stream = await self.db.get_stream(stream_id)
        if stream is None:
            return False
        transcript = await self.db.all_transcript(stream_id)
        transcript = await self._condense_if_long(
            channel=target.display(), title=stream["title"] or "", transcript=transcript
        )
        # Cap the chat pulled for a digest with a SQL LIMIT rather than loading an
        # entire stream's chat into memory; the prompt only consumes the first few
        # hundred lines anyway.
        chat_rows = await self.db.chat_since(stream_id, 0, limit=DIGEST_CHAT_LIMIT)
        chat_lines = [f"{r['author'] or '?'}: {r['text']}" for r in chat_rows if r["text"]]
        link_rows = await self.db.links_for(stream_id)
        links = [r["url"] for r in link_rows]
        find_rows = await self.db.finds_for(stream_id)
        finds = dedupe_finds(find_rows, stream["url"] or "", stream["platform"] or "")

        summary = await self._summary_text(
            build_digest_prompt(
                channel=target.display(),
                title=stream["title"] or "",
                transcript=transcript,
                chat_lines=chat_lines,
                links=links,
                style=self.cfg.llm.style,
                refined=refined,
            ),
            fallback=build_stats_summary(
                Window(transcript=transcript, chat_count=len(chat_lines), links=links),
                elapsed_label="Stream ended",
            ),
            llm=self.digest_llm,
        )

        note = Digest(
            channel=target.display(),
            title=stream["title"] or "",
            url=stream["url"] or "",
            summary=summary,
            links=tuple(links),
            refined=refined,
        )
        ok = await self.poster.post(note)
        await self.db.record_update(stream_id, "refined" if refined else "digest")

        # The complete deduped finds list ships as its own follow-up message
        # (embed description holds 4096 chars vs a field's 1024). Posted after
        # the FINAL digest only — the refined digest ~30 min later would just
        # duplicate it. Best-effort: a finds post failure never fails the digest.
        if not refined and finds:
            recap = FindsRecap(
                channel=target.display(),
                title=stream["title"] or "",
                url=stream["url"] or "",
                finds=tuple(Find(**f) for f in finds),
            )
            await self.poster.post(recap)
        return ok

    async def _extract_finds(self, stream_id: int, stream, window: Window) -> list[dict]:
        """Extract structured discoveries from a window, store them, and return
        embed-ready dicts (name/detail/deeplink) for the update field.

        Uses the cheap UPDATE backend (runs every window). Fully best-effort: any
        LLM, parse, or DB error is logged and swallowed — finds enhance updates and
        must never break them. Returns [] when there is nothing (or no real LLM).
        """
        if self.llm.name == "none":
            return []
        try:
            result = await self.llm.summarize(
                build_finds_prompt(window=window, style=self.cfg.llm.style)
            )
        except Exception as e:  # defensive: backends shouldn't raise, but never break updates
            log.warning("stream %s: finds LLM call failed: %s", stream_id, e)
            return []
        if not result.ok or not result.text.strip():
            if result.error:
                log.info("stream %s: finds extraction skipped (%s)", stream_id, result.error)
            return []
        try:
            finds = parse_finds(result.text)
        except Exception as e:  # parse_finds is defensive, but guard anyway
            log.warning("stream %s: finds parse failed: %s", stream_id, e)
            return []
        if not finds:
            return []

        seg = self.cfg.capture.segment_seconds
        start_offset = max(0, (window.first_seq - 1) * seg) if window.first_seq >= 1 else 0
        url = stream["url"] or ""
        platform = stream["platform"] or ""
        out: list[dict] = []
        for f in finds:
            try:
                await self.db.add_find(
                    stream_id, start_offset, f["type"], f["name"], f["detail"], f["sentiment"]
                )
            except Exception as e:
                log.warning("stream %s: failed to store find %r: %s", stream_id, f.get("name"), e)
                continue
            out.append(
                {
                    "name": f["name"],
                    "detail": f["detail"],
                    "deeplink": deep_link(url, platform, start_offset),
                }
            )
        return out

    async def _condense_if_long(self, *, channel: str, title: str, transcript: str) -> str:
        """Map-reduce a transcript that exceeds the digest prompt cap.

        Each segment is condensed by the LLM (preserving names/products/prices);
        the joined condensations become the digest's "transcript". A failed
        segment falls back to a hard-truncated slice so the digest never loses a
        whole span of the stream silently. With no real LLM (backend "none"),
        head-truncation is the only option — same as before.
        """
        if len(transcript) <= DIGEST_TRANSCRIPT_CAP or self.digest_llm.name == "none":
            return transcript
        segments = [
            transcript[i : i + DIGEST_SEGMENT_CHARS]
            for i in range(0, len(transcript), DIGEST_SEGMENT_CHARS)
        ]
        log.info("digest transcript %d chars -> condensing %d segments", len(transcript), len(segments))
        sem = asyncio.Semaphore(DIGEST_SEGMENT_CONCURRENCY)

        async def condense(i: int, seg: str) -> str:
            prompt = build_segment_condense_prompt(
                channel=channel,
                title=title,
                seg_index=i + 1,
                seg_total=len(segments),
                segment=seg,
                style=self.cfg.llm.style,
            )
            async with sem:
                result = await self.digest_llm.summarize(prompt)
            if result.ok and result.text.strip():
                return f"[part {i + 1}/{len(segments)}]\n{result.text.strip()}"
            log.warning("segment %d/%d condense failed (%s); using truncated raw", i + 1, len(segments), result.error)
            return f"[part {i + 1}/{len(segments)} — raw excerpt]\n{truncate(seg, 2000)}"

        condensed = await asyncio.gather(*(condense(i, s) for i, s in enumerate(segments)))
        return "\n\n".join(condensed)

    async def _summary_text(self, prompt: str, fallback: str, llm: LLMBackend | None = None) -> str:
        backend = llm or self.llm
        result = await backend.summarize(prompt)
        if result.ok and result.text.strip():
            return result.text.strip()
        if backend.name != "none":
            log.warning("llm backend %s failed (%s); using stats fallback", backend.name, result.error)
        return fallback
