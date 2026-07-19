"""STT backend interface."""

from __future__ import annotations

import abc
from pathlib import Path


class STTError(RuntimeError):
    pass


class STTBackend(abc.ABC):
    """Transcribe a single audio chunk file to text."""

    name: str = "base"

    @abc.abstractmethod
    async def transcribe(self, wav_path: Path) -> str:
        """Return transcript text for a WAV file. May return '' for silence."""
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - optional override
        return None
