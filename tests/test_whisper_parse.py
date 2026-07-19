from __future__ import annotations

import asyncio
import shutil

import pytest
from conftest import read_fixture

import watchtower.stt.whispercpp as wc
from watchtower.config import SttConfig
from watchtower.stt.whispercpp import WhisperCppBackend, parse_whisper_output


def test_parse_strips_timestamps_and_blank_markers():
    text = parse_whisper_output(read_fixture("whisper_output.txt"))
    assert "Welcome back everyone" in text
    assert "https://example.com/tool" in text
    assert "[00:00" not in text
    assert "BLANK_AUDIO" not in text
    # joined onto a single line
    assert "\n" not in text


def test_parse_plain_nt_output():
    raw = "Hello there.\nThis is clean text.\n"
    assert parse_whisper_output(raw) == "Hello there. This is clean text."


def test_parse_empty():
    assert parse_whisper_output("") == ""
    assert parse_whisper_output("[BLANK_AUDIO]\n") == ""


class _SleepBackend(WhisperCppBackend):
    """Backend whose transcribe subprocess is a long-running `sleep` we can cancel."""

    def build_argv(self, wav_path):  # noqa: D401 - test stub
        return ["sleep", "10"]


@pytest.mark.asyncio
async def test_transcribe_terminates_child_on_cancellation(monkeypatch, tmp_path):
    # A CancelledError during proc.communicate() must not orphan whisper-cli: the
    # finally clause terminates the child on any non-clean exit.
    if shutil.which("sleep") is None:
        pytest.skip("no `sleep` binary available")

    captured: dict = {}
    real_terminate = wc.terminate_process

    async def spy(proc, grace: float = 5.0):
        captured["proc"] = proc
        await real_terminate(proc, grace=grace)

    monkeypatch.setattr(wc, "terminate_process", spy)

    backend = _SleepBackend(SttConfig(chunk_timeout_seconds=30))
    wav = tmp_path / "chunk.wav"
    wav.write_bytes(b"")

    task = asyncio.create_task(backend.transcribe(wav))
    await asyncio.sleep(0.2)  # let the child spawn
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    proc = captured.get("proc")
    assert proc is not None  # terminate_process ran on the cancel path
    assert proc.returncode is not None  # child was reaped, not left running
