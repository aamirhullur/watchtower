"""Pluggable LLM summarization backends."""

from __future__ import annotations

import dataclasses

from ..config import LlmConfig
from .base import LLMBackend, LLMError, LLMResult


def build_llm(cfg: LlmConfig) -> LLMBackend:
    if cfg.backend == "claude_cli":
        from .claude_cli import ClaudeCliBackend

        return ClaudeCliBackend(cfg)
    if cfg.backend == "codex_cli":
        from .codex_cli import CodexCliBackend

        return CodexCliBackend(cfg)
    if cfg.backend == "none":
        from .none import NoneBackend

        return NoneBackend(cfg)
    raise ValueError(f"unknown llm backend: {cfg.backend}")


def build_digest_llm(cfg: LlmConfig) -> LLMBackend | None:
    """Backend for digests when ``digest_model`` differs from ``model``.

    Returns None when digests should reuse the main backend.
    """
    digest_model = cfg.digest_model or cfg.model
    digest_effort = cfg.digest_effort or cfg.effort
    if digest_model == cfg.model and digest_effort == cfg.effort:
        return None
    return build_llm(dataclasses.replace(cfg, model=digest_model, effort=digest_effort))


__all__ = ["LLMBackend", "LLMError", "LLMResult", "build_digest_llm", "build_llm"]
