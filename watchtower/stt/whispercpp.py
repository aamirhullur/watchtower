"""whisper.cpp backend via the ``whisper-cli`` subprocess.

Invocation (verified against whisper.cpp CLI):
    whisper-cli -m <model.bin> -f <chunk.wav> -t <threads> -nt

``-nt`` (--no-timestamps) makes the stdout a clean transcript with no ``[00:00]``
prefixes. We still tolerate timestamped output via ``parse_whisper_output``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from ..config import SttConfig
from ..util import minimal_env, terminate_process
from .base import STTBackend, STTError

log = logging.getLogger("watchtower.stt.whispercpp")

# Matches a leading "[00:00:00.000 --> 00:00:05.000]" timestamp block.
_TS_RE = re.compile(r"^\s*\[[0-9:.\s]+-->[0-9:.\s]+\]\s*")


def parse_whisper_output(raw: str) -> str:
    """Parse whisper-cli stdout into a single clean transcript string.

    Handles both ``-nt`` (plain lines) and timestamped output. Drops
    whisper's non-speech markers like ``[BLANK_AUDIO]`` / ``(silence)``.
    """
    out: list[str] = []
    for line in (raw or "").splitlines():
        line = _TS_RE.sub("", line).strip()
        if not line:
            continue
        low = line.lower()
        if low in ("[blank_audio]", "[silence]", "(silence)", "[music]", "[ Silence ]".lower()):
            continue
        if line.startswith("[") and line.endswith("]") and "_" in line:
            # bracketed non-speech annotation e.g. [BLANK_AUDIO]
            continue
        out.append(line)
    return " ".join(out).strip()


class WhisperCppBackend(STTBackend):
    name = "whispercpp"

    def __init__(self, cfg: SttConfig):
        self.cfg = cfg

    def build_argv(self, wav_path: Path) -> list[str]:
        return [
            self.cfg.whisper_cli,
            "-m",
            self.cfg.whisper_model,
            "-f",
            str(wav_path),
            "-t",
            str(self.cfg.whisper_threads),
            "-nt",  # no timestamps -> clean transcript on stdout
        ]

    @property
    def _timeout(self) -> int:
        # 4x a segment, never below 120s (config resolves chunk_timeout_seconds at
        # load; fall back to 120 when a SttConfig is built directly).
        return self.cfg.chunk_timeout_seconds or 120

    async def transcribe(self, wav_path: Path) -> str:
        argv = self.build_argv(wav_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=minimal_env(),  # STT never needs process secrets
            )
        except FileNotFoundError as e:
            raise STTError(f"whisper-cli not found: {self.cfg.whisper_cli}") from e
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            # A wedged whisper-cli must not hang the transcribe loop forever.
            await terminate_process(proc)
            raise STTError(f"whisper-cli timed out after {self._timeout}s") from e
        if proc.returncode != 0:
            raise STTError(
                f"whisper-cli exited {proc.returncode}: {stderr.decode('utf-8', 'replace')[:300]}"
            )
        return parse_whisper_output(stdout.decode("utf-8", "replace"))
