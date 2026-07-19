from __future__ import annotations

from conftest import read_fixture

from streamwatch.stt.whispercpp import parse_whisper_output


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
