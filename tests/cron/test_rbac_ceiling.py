from cron.rbac_ceiling import apply_cron_toolset_ceiling


def test_no_grant_returns_resolved_unchanged():
    assert apply_cron_toolset_ceiling(["terminal", "web"], None) == ["terminal", "web"]
    assert apply_cron_toolset_ceiling(None, None) is None


def test_wildcard_grant_returns_resolved_unchanged():
    assert apply_cron_toolset_ceiling(["terminal"], frozenset({"*"})) == ["terminal"]
    assert apply_cron_toolset_ceiling(None, frozenset({"*"})) is None


def test_caps_to_grant_and_keeps_floor():
    grant = frozenset({"web", "vision"})
    out = apply_cron_toolset_ceiling(["terminal", "web", "todo"], grant)
    assert "terminal" not in out
    assert "web" in out
    assert "todo" in out  # 'todo' is a FLOOR toolset, always kept


def test_chat_only_grant_caps_to_floor_only():
    out = apply_cron_toolset_ceiling(["terminal", "web", "todo"], frozenset())
    assert out == ["todo"]


def test_unset_resolved_expands_then_caps(monkeypatch):
    monkeypatch.setattr("toolsets.get_all_toolsets", lambda: ["terminal", "web", "file"])
    out = apply_cron_toolset_ceiling(None, frozenset({"web"}))
    assert out == ["web"]
