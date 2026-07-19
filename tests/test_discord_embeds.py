from __future__ import annotations

from streamwatch.discord import (
    COLOR_ANNOUNCE,
    COLOR_DIGEST,
    COLOR_REFINED,
    render,
    render_digest,
    render_go_live,
    render_rolling_update,
    render_test,
)
from streamwatch.notify import Digest, Find, FindsRecap, GoLive, RollingUpdate, WebhookTest


def test_announce_embed():
    e = render_go_live(GoLive(channel="Chan", platform="twitch", title="Playing games", url="https://t.tv/x"))
    assert "LIVE" in e["title"]
    assert e["url"] == "https://t.tv/x"
    assert e["color"] == COLOR_ANNOUNCE
    assert "Chan" in e["description"]


def test_update_embed_with_links_field():
    e = render_rolling_update(
        RollingUpdate(
            channel="C", title="T", url="https://u", summary="did stuff",
            links=("https://a.com", "https://b.com"),
        ),
        max_desc=3800,
    )
    assert e["description"] == "did stuff"
    fields = e.get("fields", [])
    assert fields and fields[0]["name"] == "Links"
    assert "https://a.com" in fields[0]["value"]


def test_update_embed_truncates_long_summary():
    e = render_rolling_update(
        RollingUpdate(channel="C", title="T", url="", summary="x" * 5000),
        max_desc=100,
    )
    assert len(e["description"]) <= 100


def test_update_embed_no_links_no_field():
    e = render_rolling_update(RollingUpdate(channel="C", title="T", url="", summary="s"), max_desc=3800)
    assert "fields" not in e


def test_links_field_wraps_urls_in_angle_brackets():
    # M7: untrusted (chat/transcript) URLs are wrapped in <> so Discord won't unfurl.
    e = render_rolling_update(
        RollingUpdate(channel="C", title="T", url="", summary="s", links=("https://a.com",)),
        max_desc=3800,
    )
    value = e["fields"][0]["value"]
    assert "<https://a.com>" in value


def test_digest_embed_final_vs_refined():
    final = render_digest(Digest(channel="C", title="T", url="", summary="s"), max_desc=3800)
    assert final["color"] == COLOR_DIGEST
    assert "Final digest" in final["title"]

    refined = render_digest(Digest(channel="C", title="T", url="", summary="s", refined=True), max_desc=3800)
    assert refined["color"] == COLOR_REFINED
    assert "Refined digest" in refined["title"]


def test_test_embed():
    e = render_test()
    assert "test" in e["title"].lower()
    assert e["description"]


def test_render_dispatch_routes_to_webhook_kinds():
    # The delivery boundary maps each neutral payload to its webhook kind.
    assert render(GoLive(channel="c", platform="twitch", title="t", url="u"), max_desc=4096)[0] == "announce"
    assert render(RollingUpdate(channel="c", title="t", url="u", summary="s"), max_desc=4096)[0] == "update"
    assert render(Digest(channel="c", title="t", url="u", summary="s"), max_desc=4096)[0] == "digest"
    assert render(Digest(channel="c", title="t", url="u", summary="s", refined=True), max_desc=4096)[0] == "refined"
    assert render(FindsRecap(channel="c", title="t", url="u"), max_desc=4096)[0] == "digest"
    assert render(WebhookTest(), max_desc=4096)[0] == "test"


def test_render_returns_none_embed_for_empty_finds_recap():
    kind, embed = render(FindsRecap(channel="c", title="t", url="u", finds=(Find(name="  "),)), max_desc=4096)
    assert kind == "digest"
    assert embed is None
