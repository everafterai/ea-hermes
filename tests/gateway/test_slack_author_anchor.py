"""Unit tests for the Slack current-message author anchor.

Without an explicit author label on the *current* (triggering) message, the
agent defaults to guessing the sender — often a name found in quoted/forwarded/
screenshot content or another thread participant. The anchor prevents that.
"""
from gateway.platforms.slack import _anchor_message_author


def test_anchors_channel_message_with_author():
    out = _anchor_message_author("the pipeline isn't rendering", "Tom", is_dm=False)
    assert out == "[Message from Tom]\nthe pipeline isn't rendering"


def test_dm_message_left_unchanged():
    # 1:1 DMs have an unambiguous sender — no anchor needed.
    assert _anchor_message_author("hi", "Tom", is_dm=True) == "hi"


def test_missing_author_left_unchanged():
    # Unresolved name (falls back to user_id or empty) → don't fabricate a label.
    assert _anchor_message_author("hi", "", is_dm=False) == "hi"


def test_blank_text_left_unchanged():
    assert _anchor_message_author("   ", "Tom", is_dm=False) == "   "


def test_anchor_precedes_existing_text_verbatim():
    # The body must be preserved exactly; only a labeled line is prepended.
    body = "line one\nline two"
    assert _anchor_message_author(body, "Noa Danon", is_dm=False) == (
        "[Message from Noa Danon]\nline one\nline two"
    )
