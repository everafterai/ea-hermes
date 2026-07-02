import tools.cronjob_tools as cj


def test_unattended_ack_required_for_gated_tool(monkeypatch):
    # A job whose toolset makes a gated tool callable, but not acknowledged.
    monkeypatch.setattr("tools.approval.tool_requires_approval",
                        lambda name: name == "stripe_api_write")
    monkeypatch.setattr(cj, "_registry_tools_for_toolset",
                        lambda ts: ["stripe_api_write"] if ts == "mcp-stripe" else [])
    err = cj._unattended_ack_error(
        enabled_toolsets=["mcp-stripe"],
        unattended_approved_tools=[],
    )
    assert err is not None and "stripe_api_write" in err


def test_unattended_ack_satisfied(monkeypatch):
    monkeypatch.setattr("tools.approval.tool_requires_approval",
                        lambda name: name == "stripe_api_write")
    monkeypatch.setattr(cj, "_registry_tools_for_toolset",
                        lambda ts: ["stripe_api_write"] if ts == "mcp-stripe" else [])
    err = cj._unattended_ack_error(
        enabled_toolsets=["mcp-stripe"],
        unattended_approved_tools=["stripe_api_write"],
    )
    assert err is None


def test_unattended_ack_inert_when_no_gated_tool(monkeypatch):
    monkeypatch.setattr("tools.approval.tool_requires_approval", lambda name: False)
    monkeypatch.setattr(cj, "_registry_tools_for_toolset", lambda ts: ["read_file"])
    assert cj._unattended_ack_error(enabled_toolsets=["file"], unattended_approved_tools=[]) is None


def test_registry_tools_for_toolset_uses_real_registry(monkeypatch):
    # Guards the get_registry import bug: proves `from tools.registry import registry`
    # works and _registry_tools_for_toolset delegates to it (not a swallowed ImportError → []).
    import tools.cronjob_tools as cj_module
    monkeypatch.setattr("tools.registry.registry.get_tool_names_for_toolset",
                        lambda ts: ["sentinel_tool"] if ts == "some_ts" else [])
    assert cj_module._registry_tools_for_toolset("some_ts") == ["sentinel_tool"]
