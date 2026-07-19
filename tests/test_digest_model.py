"""Per-artifact model routing: digests may use a stronger backend than updates."""

from __future__ import annotations

import asyncio

from streamwatch.config import LlmConfig
from streamwatch.llm import build_digest_llm
from streamwatch.llm.base import LLMBackend, LLMResult
from streamwatch.summarize import DIGEST_TRANSCRIPT_CAP, Summarizer


def test_build_digest_llm_none_when_same():
    assert build_digest_llm(LlmConfig(backend="none", model="haiku")) is None
    assert build_digest_llm(LlmConfig(backend="none", model="haiku", digest_model="haiku")) is None


def test_build_digest_llm_distinct_model():
    llm = build_digest_llm(LlmConfig(backend="claude_cli", model="haiku", digest_model="sonnet"))
    assert llm is not None
    assert llm.cfg.model == "sonnet"


class NamedLLM(LLMBackend):
    name = "claude_cli"

    def __init__(self, tag: str):
        self.tag = tag
        self.calls = 0

    async def summarize(self, prompt: str) -> LLMResult:
        self.calls += 1
        return LLMResult(ok=True, text=f"{self.tag}-out")


def test_condense_uses_digest_backend():
    from streamwatch.config import Config

    update_llm = NamedLLM("update")
    digest_llm = NamedLLM("digest")
    s = Summarizer(Config(), db=None, llm=update_llm, poster=None, digest_llm=digest_llm)  # type: ignore[arg-type]
    text = "x" * (DIGEST_TRANSCRIPT_CAP + 10)
    out = asyncio.run(s._condense_if_long(channel="c", title="t", transcript=text))
    assert digest_llm.calls > 0
    assert update_llm.calls == 0
    assert "digest-out" in out


def test_summary_text_routes_to_given_backend():
    from streamwatch.config import Config

    update_llm = NamedLLM("update")
    digest_llm = NamedLLM("digest")
    s = Summarizer(Config(), db=None, llm=update_llm, poster=None, digest_llm=digest_llm)  # type: ignore[arg-type]
    out = asyncio.run(s._summary_text("prompt", "fallback", llm=s.digest_llm))
    assert out == "digest-out"
    assert update_llm.calls == 0
