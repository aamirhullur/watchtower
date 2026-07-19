from __future__ import annotations

from streamwatch.discord import (
    COLOR_ANNOUNCE,
    COLOR_DIGEST,
    COLOR_REFINED,
    build_announce_embed,
    build_digest_embed,
    build_test_embed,
    build_update_embed,
)


def test_announce_embed():
    e = build_announce_embed(channel="Chan", platform="twitch", title="Playing games", url="https://t.tv/x")
    assert "LIVE" in e["title"]
    assert e["url"] == "https://t.tv/x"
    assert e["color"] == COLOR_ANNOUNCE
    assert "Chan" in e["description"]


def test_update_embed_with_links_field():
    e = build_update_embed(
        channel="C", title="T", url="https://u", summary="did stuff",
        links=["https://a.com", "https://b.com"], max_chars=3800,
    )
    assert e["description"] == "did stuff"
    fields = e.get("fields", [])
    assert fields and fields[0]["name"] == "Links"
    assert "https://a.com" in fields[0]["value"]


def test_update_embed_truncates_long_summary():
    e = build_update_embed(
        channel="C", title="T", url="", summary="x" * 5000, links=[], max_chars=100,
    )
    assert len(e["description"]) <= 100


def test_update_embed_no_links_no_field():
    e = build_update_embed(channel="C", title="T", url="", summary="s", links=[], max_chars=3800)
    assert "fields" not in e


def test_links_field_wraps_urls_in_angle_brackets():
    # M7: untrusted (chat/transcript) URLs are wrapped in <> so Discord won't unfurl.
    e = build_update_embed(
        channel="C", title="T", url="", summary="s", links=["https://a.com"], max_chars=3800
    )
    value = e["fields"][0]["value"]
    assert "<https://a.com>" in value


def test_digest_embed_final_vs_refined():
    final = build_digest_embed(channel="C", title="T", url="", summary="s", links=[], max_chars=3800)
    assert final["color"] == COLOR_DIGEST
    assert "Final digest" in final["title"]

    refined = build_digest_embed(
        channel="C", title="T", url="", summary="s", links=[], max_chars=3800, refined=True
    )
    assert refined["color"] == COLOR_REFINED
    assert "Refined digest" in refined["title"]


def test_test_embed():
    e = build_test_embed()
    assert "test" in e["title"].lower()
    assert e["description"]
