from __future__ import annotations

from conftest import read_fixture

from watchtower.detectors.youtube import parse_live_page


def test_parse_live_page_detects_live():
    info = parse_live_page(read_fixture("youtube_live.html"))
    assert info is not None
    assert info.video_id == "dQw4w9WgXcQ"
    assert info.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert "Rust CLI" in info.title


def test_parse_live_page_not_live():
    assert parse_live_page(read_fixture("youtube_notlive.html")) is None


def test_parse_live_page_upcoming_is_not_live():
    # Scheduled premiere must not be treated as live.
    assert parse_live_page(read_fixture("youtube_upcoming.html")) is None


def test_parse_live_page_empty():
    assert parse_live_page("") is None
    assert parse_live_page("<html></html>") is None


def test_parse_live_page_requires_both_islive_and_videoid():
    # isLive true but no resolvable watch video id -> not live.
    html = '<html>"isLive":true</html>'
    assert parse_live_page(html) is None


def test_parse_live_page_reconstructs_url_ignoring_canonical_host():
    # M6: never trust the scraped canonical href — reconstruct the watch URL from
    # the validated video id even if the canonical points at an attacker host.
    html = (
        '"isLive":true '
        '<link rel="canonical" href="https://evil.example/watch?v=abcdefghijk">'
    )
    info = parse_live_page(html)
    assert info is not None
    assert info.video_id == "abcdefghijk"
    assert info.url == "https://www.youtube.com/watch?v=abcdefghijk"
