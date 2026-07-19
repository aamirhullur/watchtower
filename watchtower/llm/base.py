"""LLM backend interface.

All backends take a prompt string and return an ``LLMResult``. Backends must never
raise on model/timeout failure that the caller can recover from. Instead they
return ``LLMResult(ok=False, ...)`` so the summarizer can fall back to a stats-only
post and still get an update out. Only misconfiguration raises ``LLMError``.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass


class LLMError(RuntimeError):
    """Unrecoverable misconfiguration (e.g. binary path invalid)."""


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    error: str = ""


class LLMBackend(abc.ABC):
    name: str = "base"
    # Set by CLI-shelling backends (see ``_run_cli``): the human label used in
    # error strings / log lines and the per-backend logger.
    _label: str = ""
    _log: logging.Logger = logging.getLogger("watchtower.llm")

    @abc.abstractmethod
    async def summarize(self, prompt: str) -> LLMResult:
        raise NotImplementedError

    async def _run_cli(self, argv: list[str], prompt: str) -> LLMResult:
        """Run a headless CLI backend: spawn ``argv``, pipe ``prompt`` on stdin in
        an isolated cwd, enforce the timeout, and map exit/empty output onto an
        ``LLMResult``. This loop is identical across the claude/codex backends; each
        supplies only its argv construction. Requires ``self.cfg`` (timeout/workdir)
        and ``self.binary`` on the subclass.

        Never raises on a recoverable failure (timeout / non-zero exit / missing
        binary): returns ``LLMResult(ok=False, ...)`` so the summarizer falls back
        to a stats-only post.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Run in a dedicated empty dir, never the service cwd, so even if a
                # tool slipped through there is nothing local to read.
                cwd=self.cfg.workdir or None,
            )
        except FileNotFoundError:
            return LLMResult(ok=False, error=f"{self._label} binary not found: {self.binary}")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self.cfg.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self._log.warning("%s CLI timed out after %ss", self._label, self.cfg.timeout_seconds)
            return LLMResult(ok=False, error=f"timeout after {self.cfg.timeout_seconds}s")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace").strip()[:400]
            self._log.warning("%s CLI exited %s: %s", self._label, proc.returncode, err)
            return LLMResult(ok=False, error=f"exit {proc.returncode}: {err}")

        text = stdout.decode("utf-8", "replace").strip()
        if not text:
            return LLMResult(ok=False, error=f"empty output from {self._label} CLI")
        return LLMResult(ok=True, text=text)
