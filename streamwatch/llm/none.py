"""No-LLM backend: signals the summarizer to build a stats-only update.

Returns ok=False with a sentinel error so the summarizer takes its stats-only
path. This keeps the code path identical to a real backend failing — the update
still goes out, just without prose.
"""

from __future__ import annotations

from ..config import LlmConfig
from .base import LLMBackend, LLMResult


class NoneBackend(LLMBackend):
    name = "none"

    def __init__(self, cfg: LlmConfig):
        self.cfg = cfg

    async def summarize(self, prompt: str) -> LLMResult:
        return LLMResult(ok=False, error="llm backend disabled (none)")
