"""OpenAI Codex CLI headless backend.

Verified against the official docs (developers.openai.com / learn.chatgpt.com
"Non-interactive mode" and codex/docs/exec.md):

    codex exec --model <model> --sandbox read-only --skip-git-repo-check -

* ``codex exec`` is the non-interactive entrypoint; it streams progress to stderr
  and prints only the final agent message to stdout.
* Passing ``-`` as the prompt argument makes Codex read the **prompt** from stdin
  (docs: "use ``codex exec -`` when stdin should become the full prompt"). This
  also sidesteps the documented hang where a positional prompt in a non-TTY child
  process waits forever on stdin EOF (openai/codex#20919, #27019).
* ``--sandbox read-only`` — summarization needs no writes; read-only never prompts.
* ``--skip-git-repo-check`` — we run outside a git repo.
* ``--model`` sets the model (a real model id, not an alias).

On timeout / non-zero exit we return ``LLMResult(ok=False)`` so the summarizer
falls back to a stats-only post.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import LlmConfig
from .base import LLMBackend, LLMResult

log = logging.getLogger("streamwatch.llm.codex")


def build_argv(binary: str, model: str, workdir: str = "") -> list[str]:
    argv = [binary or "codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check"]
    if workdir:
        # Confine Codex to a dedicated empty dir (not the service cwd) so untrusted
        # piped text can't steer it into reading local files.
        argv += ["--cd", workdir]
    if model:
        argv += ["--model", model]
    argv.append("-")  # read prompt from stdin
    return argv


class CodexCliBackend(LLMBackend):
    name = "codex_cli"

    def __init__(self, cfg: LlmConfig):
        self.cfg = cfg
        self.binary = cfg.binary or "codex"

    async def summarize(self, prompt: str) -> LLMResult:
        argv = build_argv(self.binary, self.cfg.model, self.cfg.workdir)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cfg.workdir or None,
            )
        except FileNotFoundError:
            return LLMResult(ok=False, error=f"codex binary not found: {self.binary}")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self.cfg.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("codex CLI timed out after %ss", self.cfg.timeout_seconds)
            return LLMResult(ok=False, error=f"timeout after {self.cfg.timeout_seconds}s")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace").strip()[:400]
            log.warning("codex CLI exited %s: %s", proc.returncode, err)
            return LLMResult(ok=False, error=f"exit {proc.returncode}: {err}")

        text = stdout.decode("utf-8", "replace").strip()
        if not text:
            return LLMResult(ok=False, error="empty output from codex CLI")
        return LLMResult(ok=True, text=text)
