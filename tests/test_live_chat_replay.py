"""Parsing of yt-dlp live_chat replay files (--sub-langs live_chat NDJSON)."""

from __future__ import annotations

from conftest import FIXTURES

from watchtower.chat.live_chat_replay import parse_live_chat_file, parse_live_chat_line


def test_parse_live_chat_file_fixture():
    events = parse_live_chat_file(FIXTURES / "sample_live_chat.json")
    # 5 candidate lines: normal, emoji-only (skip), membership (skip), normal, normal.
    # Plus a blank line and an invalid-JSON line (both skipped). => 3 kept events.
    assert len(events) == 3

    # Offset-sorted: 3000ms, 5000ms, 12000ms.
    assert [round(e.offset_s, 3) for e in events] == [3.0, 5.0, 12.0]

    # authorExternalChannelId fallback when authorName absent.
    assert events[0].author == "UCzzz"
    assert events[0].text == "no name here"

    # Normal message: runs concatenated, link preserved, simpleText author.
    assert events[1].author == "Alice"
    assert events[1].text == "hello check https://example.com/tool"

    # Emoji run inside a text message is skipped, surrounding text kept.
    assert events[2].author == "Dave"
    assert events[2].text == "gg wp"


def test_parse_line_string_and_int_offset_equivalent():
    line_str = (
        '{"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": '
        '{"liveChatTextMessageRenderer": {"message": {"runs": [{"text": "hi"}]}, '
        '"authorName": {"simpleText": "A"}}}}}], "videoOffsetTimeMsec": "7000"}}'
    )
    line_int = line_str.replace('"7000"', "7000")
    a = parse_live_chat_line(line_str)
    b = parse_live_chat_line(line_int)
    assert a is not None and b is not None
    assert a.offset_s == b.offset_s == 7.0


def test_emoji_only_and_membership_and_junk_ignored():
    emoji_only = (
        '{"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": '
        '{"liveChatTextMessageRenderer": {"message": {"runs": [{"emoji": {"emojiId": "x"}}]}, '
        '"authorName": {"simpleText": "B"}}}}}], "videoOffsetTimeMsec": 100}}'
    )
    membership = (
        '{"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": '
        '{"liveChatMembershipItemRenderer": {"authorName": {"simpleText": "C"}}}}}], '
        '"videoOffsetTimeMsec": 200}}'
    )
    assert parse_live_chat_line(emoji_only) is None
    assert parse_live_chat_line(membership) is None
    assert parse_live_chat_line("") is None
    assert parse_live_chat_line("garbage") is None
