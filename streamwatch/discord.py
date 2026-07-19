"""Discord webhook poster.

Pure embed-builder functions (unit-tested) + an async poster that handles 429
retry-after. Webhooks are plain HTTPS POSTs — no bot token needed, which suits an
outbound-only box behind Tailscale.
"""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

from .config import Config
from .util import truncate

log = logging.getLogger("streamwatch.discord")

# Discord brand-ish colours per message kind.
COLOR_ANNOUNCE = 0x9146FF  # twitch purple
COLOR_UPDATE = 0x5865F2  # blurple
COLOR_DIGEST = 0x57F287  # green
COLOR_REFINED = 0xFEE75C  # yellow
COLOR_TEST = 0xEB459E  # pink

_EMBED_DESC_CAP = 4096
_FIELD_VALUE_CAP = 1024


def _links_field(links: list[str], limit: int = 15) -> dict | None:
    if not links:
        return None
    shown = links[:limit]
    # Wrap each (chat/transcript-sourced, i.e. untrusted) URL in <angle brackets>
    # so Discord never unfurls an attacker-supplied link into a preview card.
    body = "\n".join(f"• <{u}>" for u in shown)
    if len(links) > limit:
        body += f"\n… (+{len(links) - limit} more)"
    return {"name": "Links", "value": truncate(body, _FIELD_VALUE_CAP), "inline": False}


def _finds_field(finds: list[dict] | None, limit: int = 5) -> dict | None:
    """Build the "🔎 Finds" embed field from a list of discovery dicts.

    Each dict carries ``name`` (required), ``detail`` and ``deeplink`` (both
    optional). Rendered as ``**name** — detail [↗](deeplink)``; the link markup is
    only appended when a non-empty deeplink is present. Kept under the 1024-char
    field cap via ``truncate``.
    """
    if not finds:
        return None
    lines: list[str] = []
    for f in finds[:limit]:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        line = f"**{name}**"
        detail = (f.get("detail") or "").strip()
        if detail:
            line += f" — {detail}"
        deeplink = (f.get("deeplink") or "").strip()
        if deeplink:
            line += f" [↗]({deeplink})"
        lines.append(line)
    if not lines:
        return None
    return {"name": "🔎 Finds", "value": truncate("\n".join(lines), _FIELD_VALUE_CAP), "inline": False}


def build_announce_embed(*, channel: str, platform: str, title: str, url: str) -> dict:
    return {
        "title": f"🔴 LIVE: {truncate(title or channel, 240)}",
        "url": url or None,
        "description": f"**{channel}** is now live on {platform}.",
        "color": COLOR_ANNOUNCE,
        "footer": {"text": "streamwatch • go-live"},
    }


def build_update_embed(
    *,
    channel: str,
    title: str,
    url: str,
    summary: str,
    links: list[str],
    max_chars: int,
    finds: list[dict] | None = None,
) -> dict:
    embed: dict = {
        "title": f"📝 Update — {truncate(title or channel, 240)}",
        "url": url or None,
        "description": truncate(summary, min(max_chars, _EMBED_DESC_CAP)),
        "color": COLOR_UPDATE,
        "footer": {"text": f"streamwatch • rolling update • {channel}"},
    }
    fields = [f for f in (_finds_field(finds), _links_field(links)) if f]
    if fields:
        embed["fields"] = fields
    return embed


def build_digest_embed(
    *,
    channel: str,
    title: str,
    url: str,
    summary: str,
    links: list[str],
    max_chars: int,
    refined: bool = False,
) -> dict:
    label = "Refined digest" if refined else "Final digest"
    emoji = "✨" if refined else "📄"
    embed: dict = {
        "title": f"{emoji} {label} — {truncate(title or channel, 220)}",
        "url": url or None,
        "description": truncate(summary, min(max_chars, _EMBED_DESC_CAP)),
        "color": COLOR_REFINED if refined else COLOR_DIGEST,
        "footer": {"text": f"streamwatch • {label.lower()} • {channel}"},
    }
    # Finds are NOT inlined here: a full stream's list blows the 1024-char field
    # cap (and the 6000-char message budget next to a 4096-char description), so
    # they ship as a standalone follow-up message — see build_finds_embed.
    fields = [f for f in (_links_field(links, limit=25),) if f]
    if fields:
        embed["fields"] = fields
    return embed


def build_finds_embed(*, channel: str, title: str, url: str, finds: list[dict]) -> dict | None:
    """Standalone end-of-stream "🔎 Finds" recap message.

    The full deduped list goes in the DESCRIPTION (4096-char cap) rather than an
    embed field (1024) so a long stream's finds all render. Lines are dropped
    whole (never mid-line) if the list somehow exceeds the cap, with a
    ``… (+N more)`` tail so truncation is visible instead of silent.
    """
    if not finds:
        return None
    lines: list[str] = []
    for f in finds:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        line = f"**{name}**"
        detail = (f.get("detail") or "").strip()
        if detail:
            line += f" — {detail}"
        deeplink = (f.get("deeplink") or "").strip()
        if deeplink:
            line += f" [↗]({deeplink})"
        lines.append(line)
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
        "title": f"🔎 Finds — {truncate(title or channel, 230)}",
        "url": url or None,
        "description": truncate("\n".join(body_lines), _EMBED_DESC_CAP),
        "color": COLOR_DIGEST,
        "footer": {"text": f"streamwatch • finds • {channel}"},
    }


def build_test_embed() -> dict:
    return {
        "title": "👋 streamwatch webhook test",
        "description": "If you can read this, the webhook works.",
        "color": COLOR_TEST,
        "footer": {"text": "streamwatch • test-webhook"},
    }


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
