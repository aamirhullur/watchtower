"""Claude Code CLI headless backend.

Verified against the official docs (https://code.claude.com/docs/en/headless):

    claude -p --model <alias> --output-format text

* ``-p`` / ``--print`` runs the full agent loop non-interactively and prints the
  result. The prompt is piped on **stdin** (docs: "Non-interactive mode reads
  stdin, so you can pipe data in") which avoids ARG_MAX limits on long windows.
* ``--model`` accepts aliases ``haiku`` / ``sonnet`` / ``opus`` (docs show
  ``/model sonnet``). Default here is ``haiku``: cheap and fast, plenty for
  summarization.
* ``--output-format text`` (default) => plain text on stdout.
* We deliberately do NOT pass ``--bare``: bare mode skips OAuth/keychain and
  requires ANTHROPIC_API_KEY, whereas we rely on ``CLAUDE_CODE_OAUTH_TOKEN``
  (set via systemd EnvironmentFile). No tools are needed for pure summarization.

On timeout / non-zero exit we return ``LLMResult(ok=False)`` so the summarizer
falls back to a stats-only post.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import LlmConfig
from .base import LLMBackend, LLMResult

log = logging.getLogger("watchtower.llm.claude")


def build_argv(binary: str, model: str, effort: str = "") -> list[str]:
    # Untrusted chat/transcript text is piped in on stdin. Headless ``claude -p``
    # would otherwise run a full agent loop with read tools auto-permitted, so a
    # prompt-injection line could exfiltrate secrets or read the box. We lock it
    # down to a single, tool-less generation turn:
    #   --disallowedTools "*"   -> no tool is ever permitted
    #   --max-turns 1           -> one model turn, no agent loop
    #   --setting-sources ""    -> empty list: ignore user/project/local settings
    #                              & CLAUDE.md ("none" is not an accepted value)
    return [
        binary or "claude",
        "-p",
        "--model",
        model,
        "--output-format",
        "text",
        "--disallowedTools",
        "*",
        "--max-turns",
        "1",
        "--setting-sources",
        "",
        # --effort only for models that accept it (sonnet/opus tiers); haiku
        # rejects the flag, so the config leaves `effort` empty for it.
        *(["--effort", effort] if effort else []),
    ]


class ClaudeCliBackend(LLMBackend):
    name = "claude_cli"

    def __init__(self, cfg: LlmConfig):
        self.cfg = cfg
        self.binary = cfg.binary or "claude"

    async def summarize(self, prompt: str) -> LLMResult:
        argv = build_argv(self.binary, self.cfg.model, self.cfg.effort)
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
            return LLMResult(ok=False, error=f"claude binary not found: {self.binary}")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self.cfg.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("claude CLI timed out after %ss", self.cfg.timeout_seconds)
            return LLMResult(ok=False, error=f"timeout after {self.cfg.timeout_seconds}s")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace").strip()[:400]
            log.warning("claude CLI exited %s: %s", proc.returncode, err)
            return LLMResult(ok=False, error=f"exit {proc.returncode}: {err}")

        text = stdout.decode("utf-8", "replace").strip()
        if not text:
            return LLMResult(ok=False, error="empty output from claude CLI")
        return LLMResult(ok=True, text=text)
