from __future__ import annotations

from watchtower.util import extract_urls, truncate


def test_extract_urls_dedup_and_order():
    text = "see https://a.com/x and https://b.com then https://a.com/x again"
    assert extract_urls(text) == ["https://a.com/x", "https://b.com"]


def test_extract_urls_strips_trailing_punctuation():
    assert extract_urls("go to https://example.com/tool.") == ["https://example.com/tool"]
    assert extract_urls("(https://example.com)") == ["https://example.com"]


def test_extract_urls_none():
    assert extract_urls("no links here") == []
    assert extract_urls("") == []


def test_truncate():
    assert truncate("hello", 10) == "hello"
    out = truncate("hello world", 6)
    assert out.endswith("…")
    assert len(out) == 6
