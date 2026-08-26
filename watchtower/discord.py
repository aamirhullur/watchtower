"""Discord delivery adapter.

Pure renderers (unit-tested) that map the neutral notification model (see
``notify.py``) onto Discord embed dicts, plus an async poster that handles 429
retry-after. Every Discord-specific constraint lives here and only here: the
1024/4096/6000-char caps, whole-line truncation, field layout, colours, footers.
The domain never sees an embed. Webhooks are plain HTTPS POSTs (no bot token
needed), which suits an outbound-only box behind Tailscale.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

import aiohttp

from .config import Config
from .notify import Digest, Find, GoLive, Notification, RollingUpdate, WebhookTest
from .util import truncate

log = logging.getLogger("watchtower.discord")

# Discord brand-ish colours per message kind.
COLOR_ANNOUNCE = 0x9146FF  # twitch purple
COLOR_UPDATE = 0x5865F2  # blurple
COLOR_DIGEST = 0x57F287  # green
COLOR_REFINED = 0xFEE75C  # yellow
COLOR_TEST = 0xEB459E  # pink

_EMBED_DESC_CAP = 4096
_FIELD_VALUE_CAP = 1024

# Markdown-link injection defence for untrusted text (LLM summary + find
# name/detail). The Links field already wraps its URLs in <> so Discord won't
# unfurl them; but a bare URL or a `[label](url)` span echoed inside the summary
# or a find would still render as a clickable link, bypassing that control.
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
# A bare http(s) URL, stopping before whitespace/brackets/quotes (mirrors
# util._URL_RE) so we don't swallow a trailing ")" or "]".
_BARE_URL_RE = re.compile(r"<?(https?://[^\s<>\"'\)\]]+)>?", re.IGNORECASE)


def _defang_links(text: str) -> str:
    """Neutralise markdown-link syntax in untrusted text at the render boundary.

    Turns ``[label](url)`` into ``label (url)`` (dropping the ``](`` that forms a
    clickable link) and wraps any surviving bare http(s) URL in ``<>`` so Discord
    never unfurls it. Plain prose and our own bullets pass through unchanged; the
    ``**bold**`` our templates add is applied to the name AFTER this runs.
    """
    if not text:
        return text
    text = _MD_LINK_RE.sub(r"\1 (\2)", text)
    text = _BARE_URL_RE.sub(lambda m: f"<{m.group(1)}>", text)
    return text


def _find_line(f: Find) -> str | None:
    """Render one find as ``**name**: detail [↗](deeplink)``.

    Detail and the ``↗`` link markup are only appended when present; a find with a
    blank name renders to nothing (returns ``None``).
    """
    name = (f.name or "").strip()
    if not name:
        return None
    # Defang untrusted name/detail BEFORE wrapping the name in our own ** bold.
    line = f"**{_defang_links(name)}**"
    detail = (f.detail or "").strip()
    if detail:
        line += f": {_defang_links(detail)}"
    deeplink = (f.deeplink or "").strip()
    if deeplink:
        line += f" [↗]({deeplink})"
    return line


def _links_field(links: tuple[str, ...], limit: int = 15) -> dict | None:
    if not links:
        return None
    shown = links[:limit]
    # Wrap each (chat/transcript-sourced, i.e. untrusted) URL in <angle brackets>
    # so Discord never unfurls an attacker-supplied link into a preview card.
    body = "\n".join(f"• <{u}>" for u in shown)
    if len(links) > limit:
        body += f"\n… (+{len(links) - limit} more)"
    return {"name": "Links", "value": truncate(body, _FIELD_VALUE_CAP), "inline": False}


def _finds_field(finds: tuple[Find, ...] | None, limit: int = 5) -> dict | None:
    """Build the "🔎 Finds" embed field from finds, kept under the 1024-char cap."""
    if not finds:
        return None
    lines = [line for f in finds[:limit] if (line := _find_line(f))]
    if not lines:
        return None
    return {"name": "🔎 Finds", "value": truncate("\n".join(lines), _FIELD_VALUE_CAP), "inline": False}


def render_go_live(note: GoLive) -> dict:
    return {
        "title": f"🔴 LIVE: {truncate(note.title or note.channel, 240)}",
        "url": note.url or None,
        "description": f"**{note.channel}** is now live on {note.platform}.",
        "color": COLOR_ANNOUNCE,
        "footer": {"text": "watchtower • go-live"},
    }


def render_rolling_update(note: RollingUpdate, *, max_desc: int) -> dict:
    embed: dict = {
        "title": f"📝 Update: {truncate(note.title or note.channel, 240)}",
        "url": note.url or None,
        "description": truncate(_defang_links(note.summary), min(max_desc, _EMBED_DESC_CAP)),
        "color": COLOR_UPDATE,
        "footer": {"text": f"watchtower • rolling update • {note.channel}"},
    }
    fields = [f for f in (_finds_field(note.finds), _links_field(note.links)) if f]
    if fields:
        embed["fields"] = fields
    return embed


def render_digest(note: Digest, *, max_desc: int) -> dict:
    label = "Refined digest" if note.refined else "Final digest"
    emoji = "✨" if note.refined else "📄"
    embed: dict = {
        "title": f"{emoji} {label}: {truncate(note.title or note.channel, 220)}",
        "url": note.url or None,
        "description": truncate(_defang_links(note.summary), min(max_desc, _EMBED_DESC_CAP)),
        "color": COLOR_REFINED if note.refined else COLOR_DIGEST,
        "footer": {"text": f"watchtower • {label.lower()} • {note.channel}"},
    }
    # Finds are NOT inlined here: a full stream's list blows the 1024-char field
    # cap (and the 6000-char message budget next to a 4096-char description), so
    # they ship as a standalone follow-up message; see render_finds_recap.
    fields = [f for f in (_links_field(note.links, limit=25),) if f]
    if fields:
        embed["fields"] = fields
    return embed


def render_finds_recap(note: Digest) -> dict | None:
    """Standalone end-of-stream "🔎 Finds" recap message for a FINAL digest.

    Discord's embed-field cap (1024) can't hold a full stream's find list, so it
    ships as its own follow-up message after the digest, driven from the Digest
    note's ``finds``. The full deduped list goes in the DESCRIPTION (4096-char
    cap); lines are dropped whole (never mid-line) if the list somehow exceeds the
    cap, with a ``… (+N more)`` tail so truncation is visible instead of silent.

    Returns None for a refined digest (the recap follows the final digest only;
    the refined pass ~30 min later would just duplicate it) or when there is
    nothing renderable.
    """
    if note.refined or not note.finds:
        return None
    lines = [line for f in note.finds if (line := _find_line(f))]
    if not lines:
        return None
    tail_reserve = 24  # room for the "… (+NN more)" marker
    body_lines: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        cost = len(line) + (1 if body_lines else 0)  # +1 for the joining newline
        if used + cost > _EMBED_DESC_CAP - tail_reserve and body_lines:
            body_lines.append(f"… (+{len(lines) - i} more)")
            break
        body_lines.append(line)
        used += cost
    return {
        "title": f"🔎 Finds: {truncate(note.title or note.channel, 230)}",
        "url": note.url or None,
        "description": truncate("\n".join(body_lines), _EMBED_DESC_CAP),
        "color": COLOR_DIGEST,
        "footer": {"text": f"watchtower • finds • {note.channel}"},
    }


def render_test() -> dict:
    return {
        "title": "👋 watchtower webhook test",
        "description": "If you can read this, the webhook works.",
        "color": COLOR_TEST,
        "footer": {"text": "watchtower • test-webhook"},
    }


def render(note: Notification, *, max_desc: int) -> tuple[str, dict]:
    """Map a neutral notification to its primary ``(webhook kind, embed)``.

    A non-refined ``Digest`` carrying finds also has a standalone follow-up recap
    message; that second embed is emitted by the poster (see ``DiscordPoster.post``
    / ``render_finds_recap``), not returned here.
    """
    if isinstance(note, GoLive):
        return "announce", render_go_live(note)
    if isinstance(note, RollingUpdate):
        return "update", render_rolling_update(note, max_desc=max_desc)
    if isinstance(note, Digest):
        return ("refined" if note.refined else "digest"), render_digest(note, max_desc=max_desc)
    if isinstance(note, WebhookTest):
        return "test", render_test()
    raise TypeError(f"unknown notification type: {type(note).__name__}")


class DiscordPoster:
    """Posts embeds to purpose-specific webhooks with 429 handling."""

    def __init__(self, cfg: Config, session: aiohttp.ClientSession | None = None, health=None, db=None):
        self.cfg = cfg
        self._session = session
        self._own_session = session is None
        self.health = health
        # Thread store: needs get_thread_id/set_thread_id coroutines (Database
        # supplies them). Absent => threads mode falls back to flat posting so the
        # webhook-test path can construct a poster without a DB.
        self.db = db

    async def __aenter__(self) -> "DiscordPoster":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()

    # ---- webhook resolution ------------------------------------------- #
    def _default_webhook(self) -> str:
        return os.environ.get(self.cfg.discord.default_webhook_env, "")

    def webhook_for(self, kind: str) -> str:
        d = self.cfg.discord
        override = {
            "announce": d.announce_webhook,
            "update": d.update_webhook,
            "digest": d.digest_webhook,
            "refined": d.digest_webhook,
            "test": "",  # always route the test post to the default webhook
        }.get(kind, "")
        return override or self._default_webhook()

    def _forum_webhook(self) -> str:
        """The single webhook used in threads mode (all kinds share one forum channel)."""
        return self.cfg.discord.forum_webhook or self._default_webhook()

    def _threads_enabled(self) -> bool:
        return bool(self.cfg.discord.threads and self.db is not None)

    # ---- posting ------------------------------------------------------- #
    async def post(self, note: Notification, *, thread_key: str | None = None, max_retries: int = 4) -> bool:
        """Deliver a neutral notification: render it here at the boundary, then POST.

        A non-refined ``Digest`` with finds sends the digest embed and THEN a
        standalone "🔎 Finds" recap embed (best-effort: a recap failure never
        affects the digest's returned result).

        ``thread_key`` is a neutral grouping hint ("these notifications belong to
        the same stream"). In threads mode the Discord adapter maps it to one forum
        thread: the first post for a key creates the thread, later posts land in it.
        """
        kind, embed = render(note, max_desc=self.cfg.discord.max_description_chars)
        if thread_key is not None and self._threads_enabled():
            return await self._post_threaded(note, embed, thread_key, max_retries=max_retries)
        ok = await self.post_embed(kind, embed, max_retries=max_retries)
        if isinstance(note, Digest):
            recap = render_finds_recap(note)
            if recap is not None:
                await self.post_embed("digest", recap, max_retries=max_retries)
        return ok

    # ---- threaded posting (Discord forum channel) ---------------------- #
    async def _post_threaded(
        self, note: Notification, embed: dict, thread_key: str, *, max_retries: int
    ) -> bool:
        """Deliver a notification into its stream's forum thread.

        The first post for ``thread_key`` (normally the go-live) creates the thread
        and records its id; every later post for the same key posts into it. A
        Digest's finds recap follows into the same thread.
        """
        url = self._forum_webhook()
        if not url:
            log.error("no forum webhook configured (set $%s or discord.forum_webhook)", self.cfg.discord.default_webhook_env)
            return False

        thread_id = await self.db.get_thread_id(thread_key)
        if thread_id is None:
            thread_id = await self._create_thread(url, self._thread_name(note), embed, max_retries=max_retries)
            if thread_id is None:
                if self.health is not None:
                    await self.health.record_failure("discord", f"create thread key={thread_key} failed")
                return False
            await self.db.set_thread_id(thread_key, thread_id)
            ok = True
        else:
            ok, status = await self._post_into_thread(url, thread_id, embed, max_retries=max_retries)
            if not ok:
                if status == 404:
                    # The stored thread is gone server-side (deleted). Clear the
                    # stale mapping so the NEXT post for this key recreates a fresh
                    # thread instead of 404-ing forever. Deliberately no inline
                    # recreate here: next-post recreation keeps the flow simple.
                    await self.db.delete_thread_id(thread_key)
                if self.health is not None:
                    await self.health.record_failure("discord", f"post into thread key={thread_key} failed")

        if isinstance(note, Digest):
            recap = render_finds_recap(note)
            if recap is not None:
                # Best-effort recap: it never changes the returned result, but a
                # failure is surfaced to health (matching the flat path's
                # post_embed) instead of being swallowed silently.
                recap_ok, _ = await self._post_into_thread(url, thread_id, recap, max_retries=max_retries)
                if not recap_ok and self.health is not None:
                    await self.health.record_failure("discord", f"finds recap into thread key={thread_key} failed")
        return ok

    @staticmethod
    def _thread_name(note: Notification) -> str:
        """Forum thread title for a stream (Discord caps thread names at 100 chars)."""
        if isinstance(note, GoLive):
            return truncate(f"🔴 LIVE: {note.title or note.channel}", 100)
        title = getattr(note, "title", "") or getattr(note, "channel", "") or "watchtower"
        return truncate(title, 100)

    async def post_embed(self, kind: str, embed: dict, *, max_retries: int = 4) -> bool:
        url = self.webhook_for(kind)
        if not url:
            log.error("no webhook configured for kind=%s (set $%s)", kind, self.cfg.discord.default_webhook_env)
            return False
        ok = await self._post(url, self._payload(embed), max_retries=max_retries)
        if not ok and self.health is not None:
            # Cursor has already advanced (we don't re-summarize); surface the lost
            # post to health so a broken webhook is visible instead of silent.
            await self.health.record_failure("discord", f"post kind={kind} failed all retries")
        return ok

    def _payload(self, embed: dict, *, thread_name: str | None = None) -> dict:
        payload = {
            "username": self.cfg.discord.username,
            "embeds": [embed],
            # Suppress @everyone/@here/role/user pings from any untrusted text that
            # made it into the embed.
            "allowed_mentions": {"parse": []},
        }
        if thread_name is not None:
            # thread_name on a forum-channel webhook creates a new thread (post).
            payload["thread_name"] = thread_name
        return payload

    async def _create_thread(self, url: str, thread_name: str, embed: dict, *, max_retries: int) -> str | None:
        """Create a forum thread whose root message is ``embed``; return its thread id.

        Needs ``?wait=true`` so Discord returns the created message, whose
        ``channel_id`` is the new thread (that later updates post into). Returns
        None on failure or if the response lacks the id.

        Known limitation (accepted): the create POST is not idempotent. On a
        timeout-then-retry, Discord may already have created the thread but the
        response was lost, so the retry creates a SECOND forum thread. We accept
        this rare duplicate rather than add server-side dedup (which a webhook,
        with no bot token, can't easily do).
        """
        ok, data, _ = await self._send(
            url, self._payload(embed, thread_name=thread_name), params={"wait": "true"}, max_retries=max_retries
        )
        if not ok or not data:
            return None
        thread_id = data.get("channel_id")
        return str(thread_id) if thread_id else None

    async def _post_into_thread(
        self, url: str, thread_id: str, embed: dict, *, max_retries: int
    ) -> tuple[bool, int | None]:
        """Post ``embed`` into an existing forum thread; return (ok, last-status).

        The status lets the caller detect a definitive 404 (the stored thread was
        deleted server-side) and clear the stale mapping.
        """
        ok, _, status = await self._send(
            url, self._payload(embed), params={"thread_id": thread_id}, max_retries=max_retries
        )
        return ok, status

    async def _post(self, url: str, payload: dict, *, max_retries: int) -> bool:
        ok, _, _ = await self._send(url, payload, max_retries=max_retries)
        return ok

    async def _send(
        self, url: str, payload: dict, *, params: dict | None = None, max_retries: int = 4
    ) -> tuple[bool, dict | None, int | None]:
        """POST an embed payload, returning (ok, response-json, last-status).

        Response json is parsed only when ``?wait=true`` was requested (thread
        creation); flat posts return (ok, None, status). ``last-status`` is the
        last HTTP status observed, or None if every attempt raised before any
        response (e.g. all timeouts), so callers can act on a definitive 4xx (a
        404 posting into a deleted thread). Shares 429 back-off and 5xx retry.
        """
        assert self._session is not None
        want_json = bool(params and params.get("wait") == "true")
        last_status: int | None = None
        for attempt in range(1, max_retries + 1):
            try:
                async with self._session.post(
                    url, params=params, json=payload, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    last_status = resp.status
                    if resp.status == 429:
                        # Cap the honoured back-off so a hostile/buggy Retry-After
                        # can't park the poster for minutes.
                        retry_after = min(await self._retry_after(resp), 60.0)
                        log.warning("discord 429; retrying after %.1fs (attempt %d)", retry_after, attempt)
                        await asyncio.sleep(retry_after)
                        continue
                    if 200 <= resp.status < 300:
                        data = None
                        if want_json:
                            try:
                                data = await resp.json()
                            except Exception:
                                data = None
                        return True, data, resp.status
                    body = await resp.text()
                    log.error("discord POST failed status=%s body=%s", resp.status, truncate(body, 300))
                    if 500 <= resp.status < 600:
                        await asyncio.sleep(min(2**attempt, 30))
                        continue
                    return False, None, resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("discord POST error: %s (attempt %d)", e, attempt)
                await asyncio.sleep(min(2**attempt, 30))
        log.error("discord POST giving up after %d attempts", max_retries)
        return False, None, last_status

    @staticmethod
    async def _retry_after(resp: aiohttp.ClientResponse) -> float:
        # Discord sends retry-after both as a header and in the JSON body.
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        try:
            data = await resp.json()
            return float(data.get("retry_after", 1.0))
        except Exception:
            return 1.0
