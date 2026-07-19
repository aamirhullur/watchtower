"""Map-reduce digest condensation for transcripts exceeding the prompt cap."""

from __future__ import annotations

import asyncio

import pytest

from watchtower.llm.base import LLMBackend, LLMResult
from watchtower.summarize import (
    DIGEST_SEGMENT_CHARS,
    DIGEST_TRANSCRIPT_CAP,
    Summarizer,
    build_segment_condense_prompt,
)


class RecordingLLM(LLMBackend):
    name = "claude_cli"

    def __init__(self, fail_indices: set[int] | None = None):
        self.prompts: list[str] = []
        self.fail_indices = fail_indices or set()

    async def summarize(self, prompt: str) -> LLMResult:
        self.prompts.append(prompt)
        idx = len(self.prompts) - 1
        if idx in self.fail_indices:
            return LLMResult(ok=False, error="boom")
        return LLMResult(ok=True, text=f"condensed-{idx}")


def make_summarizer(llm: LLMBackend) -> Summarizer:
    from watchtower.config import Config

    return Summarizer(Config(), db=None, llm=llm, poster=None)  # type: ignore[arg-type]


def test_short_transcript_passes_through():
    llm = RecordingLLM()
    s = make_summarizer(llm)
    text = "short transcript"
    out = asyncio.run(s._condense_if_long(channel="c", title="t", transcript=text))
    assert out == text
    assert llm.prompts == []


def test_long_transcript_is_condensed_per_segment():
    llm = RecordingLLM()
    s = make_summarizer(llm)
    text = "x" * (DIGEST_TRANSCRIPT_CAP + DIGEST_SEGMENT_CHARS + 100)
    out = asyncio.run(s._condense_if_long(channel="c", title="t", transcript=text))
    expected_segments = 3  # ceil(42100 / 18000)
    assert len(llm.prompts) == expected_segments
    for i in range(expected_segments):
        assert f"[part {i + 1}/{expected_segments}]" in out
        assert f"condensed-" in out
    assert len(out) < len(text)


def test_failed_segment_falls_back_to_raw_excerpt():
    llm = RecordingLLM(fail_indices={1})
    s = make_summarizer(llm)
    text = "y" * (DIGEST_TRANSCRIPT_CAP + DIGEST_SEGMENT_CHARS + 100)
    out = asyncio.run(s._condense_if_long(channel="c", title="t", transcript=text))
    assert "raw excerpt" in out
    assert "condensed-0" in out and "condensed-2" in out


def test_none_backend_skips_condensing():
    class NoneLLM(RecordingLLM):
        name = "none"

    llm = NoneLLM()
    s = make_summarizer(llm)
    text = "z" * (DIGEST_TRANSCRIPT_CAP * 2)
    out = asyncio.run(s._condense_if_long(channel="c", title="t", transcript=text))
    assert out == text
    assert llm.prompts == []


def test_condense_prompt_demands_name_preservation():
    p = build_segment_condense_prompt(
        channel="c", title="t", seg_index=2, seg_total=5, segment="seg", style="style"
    )
    assert "2 of 5" in p
    assert "products" in p and "prices" in p
