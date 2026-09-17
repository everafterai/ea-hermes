import json

import pytest

from agent.cost_attribution import (
    ChannelKey,
    channel_from_origin,
    cron_job_id_from_session_id,
    is_thread_origin,
)


class TestCronJobId:
    def test_twelve_hex_id(self):
        assert cron_job_id_from_session_id("cron_3f9a1c2d4e5b_20260917_081500") == "3f9a1c2d4e5b"

    def test_hand_written_id_with_underscores(self):
        assert cron_job_id_from_session_id("cron_daily_digest_v2_20260917_081500") == "daily_digest_v2"

    def test_non_cron_id(self):
        assert cron_job_id_from_session_id("slack_C123_1700000000") is None

    def test_missing_timestamp_is_not_cron(self):
        assert cron_job_id_from_session_id("cron_3f9a1c2d4e5b") is None

    def test_none(self):
        assert cron_job_id_from_session_id(None) is None


class TestChannelFromOrigin:
    def test_channel_session(self):
        origin = {"platform": "slack", "chat_id": "C123", "chat_type": "channel", "chat_name": "issues"}
        key = channel_from_origin(source="slack", chat_id="C123", chat_type="channel", user_id="U1",
                                  origin_json=json.dumps(origin), display_name="issues")
        assert key == ChannelKey(platform="slack", chat_id="C123", name="issues")
        assert key.label == "slack:issues"

    def test_thread_rolls_up_to_parent_channel(self):
        origin = {"platform": "slack", "chat_id": "C123:1700.1", "chat_type": "thread",
                  "parent_chat_id": "C123", "chat_name": "issues"}
        key = channel_from_origin(source="slack", chat_id="C123:1700.1", chat_type="thread", user_id="U1",
                                  origin_json=json.dumps(origin), display_name="issues")
        assert key.chat_id == "C123"
        assert key.platform == "slack"
        assert is_thread_origin(json.dumps(origin))

    def test_dm_keys_on_user(self):
        origin = {"platform": "slack", "chat_id": "D999", "chat_type": "dm"}
        key = channel_from_origin(source="slack", chat_id="D999", chat_type="dm", user_id="U42",
                                  origin_json=json.dumps(origin), display_name=None)
        assert key == ChannelKey(platform="slack", chat_id="dm:U42", name="dm:U42")

    def test_no_origin_falls_back_to_columns(self):
        key = channel_from_origin(source="slack", chat_id="C7", chat_type="channel", user_id=None,
                                  origin_json=None, display_name="general")
        assert key == ChannelKey(platform="slack", chat_id="C7", name="general")

    def test_nothing_gives_none(self):
        assert channel_from_origin(source="cli", chat_id=None, chat_type=None, user_id=None,
                                   origin_json=None, display_name=None) is None

    def test_malformed_origin_json_is_ignored(self):
        key = channel_from_origin(source="slack", chat_id="C7", chat_type="channel", user_id=None,
                                  origin_json="{not json", display_name=None)
        assert key == ChannelKey(platform="slack", chat_id="C7", name="C7")
        assert not is_thread_origin("{not json")
