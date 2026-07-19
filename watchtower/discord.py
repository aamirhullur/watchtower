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

    def __init__(self, cfg: Config, session: aiohttp.ClientSession | None = None, health=None):
        self.cfg = cfg
        self._session = session
        self._own_session = session is None
        self.health = health

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

    # ---- posting ------------------------------------------------------- #
    async def post(self, note: Notification, *, max_retries: int = 4) -> bool:
        """Deliver a neutral notification: render it here at the boundary, then POST.

        A non-refined ``Digest`` with finds sends the digest embed and THEN a
        standalone "🔎 Finds" recap embed (same webhook kind, best-effort: a recap
        failure never affects the digest's returned result).
        """
        kind, embed = render(note, max_desc=self.cfg.discord.max_description_chars)
        ok = await self.post_embed(kind, embed, max_retries=max_retries)
        if isinstance(note, Digest):
            recap = render_finds_recap(note)
            if recap is not None:
                await self.post_embed("digest", recap, max_retries=max_retries)
        return ok

    async def post_embed(self, kind: str, embed: dict, *, max_retries: int = 4) -> bool:
        url = self.webhook_for(kind)
        if not url:
            log.error("no webhook configured for kind=%s (set $%s)", kind, self.cfg.discord.default_webhook_env)
            return False
        payload = {
            "username": self.cfg.discord.username,
            "embeds": [embed],
            # Suppress @everyone/@here/role/user pings from any untrusted text that
            # made it into the embed.
            "allowed_mentions": {"parse": []},
        }
        ok = await self._post(url, payload, max_retries=max_retries)
        if not ok and self.health is not None:
            # Cursor has already advanced (we don't re-summarize); surface the lost
            # post to health so a broken webhook is visible instead of silent.
            await self.health.record_failure("discord", f"post kind={kind} failed all retries")
        return ok

    async def _post(self, url: str, payload: dict, *, max_retries: int) -> bool:
        assert self._session is not None
        for attempt in range(1, max_retries + 1):
            try:
                async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 429:
                        # Cap the honoured back-off so a hostile/buggy Retry-After
                        # can't park the poster for minutes.
                        retry_after = min(await self._retry_after(resp), 60.0)
                        log.warning("discord 429; retrying after %.1fs (attempt %d)", retry_after, attempt)
                        await asyncio.sleep(retry_after)
                        continue
                    if 200 <= resp.status < 300:
                        return True
                    body = await resp.text()
                    log.error("discord POST failed status=%s body=%s", resp.status, truncate(body, 300))
                    if 500 <= resp.status < 600:
                        await asyncio.sleep(min(2**attempt, 30))
                        continue
                    return False
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("discord POST error: %s (attempt %d)", e, attempt)
                await asyncio.sleep(min(2**attempt, 30))
        log.error("discord POST giving up after %d attempts", max_retries)
        return False

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
