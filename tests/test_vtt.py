from __future__ import annotations

from conftest import read_fixture

from streamwatch.util import parse_vtt


def test_parse_vtt_dedups_rolling_captions():
    text = parse_vtt(read_fixture("sample.vtt"))
    assert "hello everyone welcome back" in text
    assert "checking out the new tool" in text
    # The rolling duplicate "hello everyone welcome back" should not repeat 3x.
    assert text.count("hello everyone welcome back") == 1
    assert "WEBVTT" not in text
    assert "-->" not in text
    assert "<c>" not in text


def test_parse_vtt_empty():
    assert parse_vtt("") == ""
    assert parse_vtt("WEBVTT\n\n") == ""
