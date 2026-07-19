"""Groq Whisper API backend (optional fallback).

Uses Groq's OpenAI-compatible audio transcription endpoint. Key comes from the
env var named by ``SttConfig.groq_api_key_env`` (default ``GROQ_API_KEY``), never
from YAML.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import aiohttp

from ..config import SttConfig
from .base import STTBackend, STTError

log = logging.getLogger("watchtower.stt.groq")

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


class GroqBackend(STTBackend):
    name = "groq"

    def __init__(self, cfg: SttConfig, session: aiohttp.ClientSession | None = None):
        self.cfg = cfg
        self._session = session
        self._own = session is None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def transcribe(self, wav_path: Path) -> str:
        key = os.environ.get(self.cfg.groq_api_key_env)
        if not key:
            raise STTError(f"${self.cfg.groq_api_key_env} not set for groq backend")
        session = await self._ensure_session()
        data = aiohttp.FormData()
        data.add_field("model", self.cfg.groq_model)
        data.add_field("response_format", "text")
        data.add_field(
            "file",
            wav_path.read_bytes(),
            filename=wav_path.name,
            content_type="audio/wav",
        )
        try:
            async with session.post(
                GROQ_URL,
                data=data,
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise STTError(f"groq transcription failed status={resp.status}: {body[:300]}")
                return body.strip()
        except aiohttp.ClientError as e:
            raise STTError(f"groq request error: {e}") from e

    async def close(self) -> None:
        if self._own and self._session is not None:
            await self._session.close()
