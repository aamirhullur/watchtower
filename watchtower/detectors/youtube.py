"""YouTube go-live detection by polling the channel /live page.

No API key needed: we GET ``https://www.youtube.com/@<handle>/live`` with a
browser User-Agent and inspect the returned HTML. When a channel is live, the
initial-data JSON embedded in the page contains ``"isLive":true`` and a canonical
watch URL / video id. When not live, YouTube serves the channel page or an
upcoming/holding page without those markers.

``parse_live_page`` is a pure function so it is unit-tested against saved HTML
fixtures (live + not-live).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import aiohttp

from . import LiveEvent

log = logging.getLogger("watchtower.detectors.youtube")

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# canonical <link rel="canonical" href="https://www.youtube.com/watch?v=VIDEOID">
_CANONICAL_RE = re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', re.IGNORECASE)
_WATCH_ID_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
# "videoId":"VIDEOID"
_VIDEOID_RE = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')
# Currently-live signal. NOTE: "isLiveContent":true only means the video is a
# livestream *type* (true for scheduled/upcoming and finished VODs too), so it is
# deliberately NOT treated as "currently live".
_ISLIVE_RE = re.compile(r'"(?:isLiveNow|isLive)"\s*:\s*true')
_ISUPCOMING_RE = re.compile(r'"isUpcoming"\s*:\s*true')
# title
_TITLE_META_RE = re.compile(r'<meta[^>]+name="title"[^>]+content="([^"]*)"', re.IGNORECASE)
_TITLE_OG_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.IGNORECASE)


# Sentinel distinguishing "fetch failed" (network error / timeout / non-200) from
# "fetched OK but the channel is not live" (html present, parse returns None). A
# fetch failure must NOT be conflated with going offline, or one 429/timeout
# mid-stream would fire a premature digest + duplicate re-announce.
FETCH_ERROR = object()


@dataclass
class LiveInfo:
    video_id: str
    title: str
    url: str


def _html_unescape(s: str) -> str:
    return (
        s.replace("\\u0026", "&")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("\\/", "/")
    )


def parse_live_page(html: str) -> LiveInfo | None:
    """Return LiveInfo if the /live page indicates a currently-live stream, else None."""
    if not html:
        return None

    if _ISUPCOMING_RE.search(html) and not _ISLIVE_RE.search(html):
        # Scheduled premiere / upcoming stream — not live yet.
        return None

    is_live = bool(_ISLIVE_RE.search(html))

    # Resolve video id: prefer canonical watch URL, fall back to videoId token.
    video_id = ""
    canonical = ""
    m = _CANONICAL_RE.search(html)
    if m:
        canonical = _html_unescape(m.group(1))
        mv = _WATCH_ID_RE.search(canonical)
        if mv:
            video_id = mv.group(1)
    if not video_id:
        mv = _VIDEOID_RE.search(html)
        if mv:
            video_id = mv.group(1)

    # A live page must both signal isLive AND resolve to a watch video id.
    if not (is_live and video_id):
        return None

    title = ""
    mt = _TITLE_META_RE.search(html) or _TITLE_OG_RE.search(html)
    if mt:
        title = _html_unescape(mt.group(1)).strip()

    # Never trust the scraped <link rel=canonical> href as the URL. video_id is
    # already validated ([A-Za-z0-9_-]{11}); reconstruct a canonical watch URL from
    # it so a doctored canonical tag can't inject an arbitrary link into announce
    # embeds or downstream capture.
    url = f"https://www.youtube.com/watch?v={video_id}"
    return LiveInfo(video_id=video_id, title=title or "Live stream", url=url)


class YouTubeDetector:
    """Polls a single YouTube channel and fires a callback on go-live/offline transitions."""

    def __init__(
        self,
        handle: str,
        channel_name: str,
        poll_interval_minutes: int,
        session: aiohttp.ClientSession,
        offline_confirmations: int = 2,
    ):
        self.handle = handle.lstrip("@")
        self.channel_name = channel_name
        self.poll_interval = max(1, poll_interval_minutes) * 60
        self.session = session
        # Consecutive fetched-and-not-live polls required to declare offline.
        self.offline_confirmations = max(1, offline_confirmations)
        self._current_video_id: str | None = None
        self._not_live_streak = 0

    @property
    def live_url(self) -> str:
        return f"https://www.youtube.com/@{self.handle}/live"

    async def fetch_html(self):
        """Return page HTML on a 200, or ``FETCH_ERROR`` on any network error /
        timeout / non-200 status. Never returns None (that ambiguity is what
        conflated fetch failures with 'not live')."""
        try:
            async with self.session.get(
                self.live_url,
                headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    log.debug("youtube %s live-page status=%s", self.handle, resp.status)
                    return FETCH_ERROR
                return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("youtube %s poll error: %s", self.handle, e)
            return FETCH_ERROR

    async def poll_once(self):
        """Tri-state: ``LiveInfo`` if live, ``None`` if fetched-and-not-live,
        ``FETCH_ERROR`` if the fetch failed."""
        html = await self.fetch_html()
        if html is FETCH_ERROR:
            return FETCH_ERROR
        return parse_live_page(html)

    async def run(self, on_live, on_offline, stop: asyncio.Event) -> None:
        """Poll loop. Calls on_live(LiveEvent) on a fresh go-live and
        on_offline(channel) once a previously-live stream has been confirmed gone
        for ``offline_confirmations`` consecutive polls. Fetch errors are treated
        as 'unknown' — they never advance the offline streak."""
        while not stop.is_set():
            result = await self.poll_once()
            if result is FETCH_ERROR:
                # Transient: don't count toward offline, don't reset a real streak.
                log.debug("youtube %s poll fetch-error; holding state", self.handle)
            elif result is not None:
                # Live.
                self._not_live_streak = 0
                if result.video_id != self._current_video_id:
                    self._current_video_id = result.video_id
                    log.info("youtube %s LIVE: %s (%s)", self.handle, result.title, result.video_id)
                    await on_live(
                        LiveEvent(
                            platform="youtube",
                            channel=self.handle,
                            title=result.title,
                            url=result.url,
                            video_id=result.video_id,
                        )
                    )
            else:
                # Fetched OK but not live.
                if self._current_video_id is not None:
                    self._not_live_streak += 1
                    if self._not_live_streak >= self.offline_confirmations:
                        log.info(
                            "youtube %s went offline (%d consecutive not-live polls)",
                            self.handle,
                            self._not_live_streak,
                        )
                        self._current_video_id = None
                        self._not_live_streak = 0
                        await on_offline("youtube", self.handle)

            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass
