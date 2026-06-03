"""Per-scope isolation tests for the holographic memory provider."""

import json
import pytest

from gateway.session_context import set_session_vars, clear_session_vars
from plugins.memory.holographic import (
    HolographicMemoryProvider,
    _resolve_scope,
    _sanitize_scope_id,
)


def _with_scope(**kwargs):
    """Context manager-ish helper: set session vars, yield, then clear."""
    return set_session_vars(**kwargs)


class TestResolveScope:
    def test_dm_resolves_to_user(self):
        tokens = set_session_vars(chat_type="dm", user_id="U_SHAI", chat_id="D123")
        try:
            assert _resolve_scope() == ("user", "U_SHAI")
        finally:
            clear_session_vars(tokens)

    def test_channel_resolves_to_chat(self):
        tokens = set_session_vars(chat_type="channel", user_id="U_SHAI", chat_id="C_PROD")
        try:
            assert _resolve_scope() == ("chat", "C_PROD")
        finally:
            clear_session_vars(tokens)

    def test_group_and_thread_resolve_to_chat(self):
        for ctype in ("group", "thread"):
            tokens = set_session_vars(chat_type=ctype, user_id="U1", chat_id="C9")
            try:
                assert _resolve_scope() == ("chat", "C9")
            finally:
                clear_session_vars(tokens)

    def test_dm_without_chat_type_falls_back_to_user(self):
        tokens = set_session_vars(chat_type="", user_id="U_ONLY", chat_id="")
        try:
            assert _resolve_scope() == ("user", "U_ONLY")
        finally:
            clear_session_vars(tokens)

    def test_no_identity_resolves_to_default(self):
        tokens = set_session_vars(chat_type="", user_id="", chat_id="")
        try:
            assert _resolve_scope() == ("default", "default")
        finally:
            clear_session_vars(tokens)
