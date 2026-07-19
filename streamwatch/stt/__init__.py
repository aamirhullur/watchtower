"""Speech-to-text backends."""

from __future__ import annotations

from ..config import SttConfig
from .base import STTBackend


def build_stt(cfg: SttConfig) -> STTBackend:
    if cfg.backend == "whispercpp":
        from .whispercpp import WhisperCppBackend

        return WhisperCppBackend(cfg)
    if cfg.backend == "groq":
        from .groq import GroqBackend

        return GroqBackend(cfg)
    raise ValueError(f"unknown stt backend: {cfg.backend}")


__all__ = ["STTBackend", "build_stt"]
