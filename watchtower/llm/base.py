"""LLM backend interface.

All backends take a prompt string and return an ``LLMResult``. Backends must never
raise on model/timeout failure that the caller can recover from. Instead they
return ``LLMResult(ok=False, ...)`` so the summarizer can fall back to a stats-only
post and still get an update out. Only misconfiguration raises ``LLMError``.
"""

from __future__ import annotations

import abc
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

    @abc.abstractmethod
    async def summarize(self, prompt: str) -> LLMResult:
        raise NotImplementedError
