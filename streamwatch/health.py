"""Health: heartbeat file + optional ntfy alerting on crash-loops.

* Heartbeat: a task touches ``health.heartbeat_file`` every N seconds. An external
  watchdog (systemd, cron, uptime-kuma) can alert if the mtime goes stale.
* Failure tracking: components report failures via ``record_failure(component)``.
  When a component crosses ``ntfy.failure_threshold`` consecutive failures we push
  a single ntfy alert (bearer token from env) and reset, so we don't spam.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from pathlib import Path

import aiohttp

from .config import HealthConfig
from .util import now_utc, utc_iso

log = logging.getLogger("streamwatch.health")


class HealthMonitor:
    def __init__(self, cfg: HealthConfig, session: aiohttp.ClientSession | None = None):
        self.cfg = cfg
        self._session = session
        self._own = session is None
        self._failures: dict[str, int] = defaultdict(int)

    async def __aenter__(self) -> "HealthMonitor":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own and self._session is not None:
            await self._session.close()

    # ---- heartbeat ----------------------------------------------------- #
    async def heartbeat_loop(self, stop: asyncio.Event) -> None:
        path = Path(self.cfg.heartbeat_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        while not stop.is_set():
            try:
                path.write_text(utc_iso(now_utc()))
            except OSError as e:
                log.warning("heartbeat write failed: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.heartbeat_interval_seconds)
            except asyncio.TimeoutError:
                pass

    # ---- failure tracking --------------------------------------------- #
    def record_success(self, component: str) -> None:
        self._failures[component] = 0

    async def record_failure(self, component: str, detail: str = "") -> None:
        self._failures[component] += 1
        count = self._failures[component]
        log.warning("component %s failure #%d: %s", component, count, detail)
        if self.cfg.ntfy.enabled and count >= self.cfg.ntfy.failure_threshold:
            await self._alert(
                f"streamwatch: {component} failed {count}x",
                detail or "repeated failures; check logs",
            )
            self._failures[component] = 0  # reset after alerting

    async def _alert(self, title: str, body: str) -> None:
        if not self.cfg.ntfy.url:
            log.error("ntfy alert requested but health.ntfy.url is empty")
            return
        token = os.environ.get(self.cfg.ntfy.token_env, "")
        headers = {"Title": title, "Priority": "high", "Tags": "warning"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        assert self._session is not None
        try:
            async with self._session.post(
                self.cfg.ntfy.url,
                data=body.encode("utf-8"),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 300:
                    log.error("ntfy alert failed status=%s", resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.error("ntfy alert error: %s", e)
