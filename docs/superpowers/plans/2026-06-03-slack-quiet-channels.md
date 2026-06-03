# Slack Quiet Channels + slack_react Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Slack bot act as a low-noise "hidden assistant" in designated channels — hide tool-call progress, allow it to react with arbitrary emoji via a new `slack_react` tool, and permit silent (emoji-only) completion — while always keeping text replies available.

**Architecture:** Three composable pieces. (1) A new self-registering `slack_react` tool that POSTs to Slack's `reactions.add`/`reactions.remove` Web API using a fresh `aiohttp` session (loop-safe, mirrors `tools/send_message_tool.py:_send_slack`), targeting the triggering message from session contextvars. (2) A `slack.quiet_channels` config list consumed in `gateway/run.py` that (a) forces `tool_progress: off` and (b) suppresses only the "no response generated" warning on a *successful empty* turn. (3) Existing `channel_prompts` drive the emoji-first behavior — no code. Emoji-first is prompt-driven, never hard-enforced.

**Tech Stack:** Python, `aiohttp`, the in-repo tool registry (`tools/registry.py`), the gateway (`gateway/run.py`, `gateway/platforms/slack.py`), toolsets (`toolsets.py`), RBAC (`gateway/tool_access.py`). Tests via `scripts/run_tests.sh` (CI-parity wrapper — never bare pytest).

**Spec:** [docs/superpowers/specs/2026-06-03-slack-quiet-channels-design.md](../specs/2026-06-03-slack-quiet-channels-design.md)

---

## File Structure

**Create:**
- `tools/slack_react_tool.py` — the `slack_react` tool (schema, async handler, Slack API poster, registration). Auto-discovered by `discover_builtin_tools()` (glob over `tools/*.py`).
- `tests/tools/test_slack_react_tool.py` — unit tests for the tool.
- `tests/gateway/test_quiet_channels.py` — unit tests for quiet-channel helpers + normalize gating.

**Modify:**
- `hermes_cli/config.py:1470-1475` — add `quiet_channels` default to the `slack` block.
- `gateway/run.py` — add `_parse_channel_id_list()` + `_is_quiet_channel()` helpers; add `quiet_completion_ok` param to `_normalize_empty_agent_response()`; wire both run.py call sites (normalize ~8867, tool-progress ~15962).
- `toolsets.py:~282` — add a `"slack"` toolset.
- `hermes_cli/tools_config.py:54,132` — register `slack` in `CONFIGURABLE_TOOLSETS` + `_TOOLSET_PLATFORM_RESTRICTIONS`.
- `CLAUDE.md` — document the fork-specific feature.

**Conventions to follow:**
- Tool handlers take `(args: dict, **kwargs)` and return a **JSON string**. Async handlers set `is_async=True` and are bridged by the registry via `_run_async`.
- Tests must not write to `~/.hermes/` (autouse fixture redirects `HERMES_HOME`).
- Use `scripts/run_tests.sh <path>` for every test run.

---

## Task 1: Add `quiet_channels` config default

**Files:**
- Modify: `hermes_cli/config.py:1470-1475`
- Test: `tests/hermes_cli/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/hermes_cli/test_config.py` (near the existing Slack default tests):

```python
def test_default_config_includes_slack_quiet_channels(self):
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["slack"]["quiet_channels"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/hermes_cli/test_config.py -k slack_quiet_channels`
Expected: FAIL with `KeyError: 'quiet_channels'`.

- [ ] **Step 3: Add the default**

In `hermes_cli/config.py`, change the `slack` block (currently lines 1470-1475) to:

```python
    "slack": {
        "require_mention": True,       # Require @mention to respond in channels
        "free_response_channels": "",  # Comma-separated channel IDs where bot responds without mention
        "allowed_channels": "",        # If set, bot ONLY responds in these channel IDs (whitelist)
        "quiet_channels": "",          # Comma-separated channel IDs: hide tool-progress + allow emoji-only (silent) completion
        "channel_prompts": {},         # Per-channel ephemeral system prompts
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/hermes_cli/test_config.py -k slack_quiet_channels`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/config.py tests/hermes_cli/test_config.py
git commit -m "feat(slack): add quiet_channels config default"
```

---

## Task 2: Quiet-channel helpers in run.py

Two pure helpers: parse a comma-separated channel list, and decide whether a `SessionSource` is a quiet Slack channel (matching `chat_id` or `parent_chat_id`).

**Files:**
- Modify: `gateway/run.py` (add helpers near `_load_gateway_config`, around line 1433)
- Test: `tests/gateway/test_quiet_channels.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/gateway/test_quiet_channels.py`:

```python
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.run import _parse_channel_id_list, _is_quiet_channel


def _src(chat_id="C1", parent=None, platform=Platform.SLACK):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type="channel",
        parent_chat_id=parent,
    )


def test_parse_channel_id_list_splits_and_strips():
    assert _parse_channel_id_list("C1, C2 ,, C3") == {"C1", "C2", "C3"}


def test_parse_channel_id_list_handles_empty():
    assert _parse_channel_id_list("") == set()
    assert _parse_channel_id_list(None) == set()


def test_is_quiet_channel_matches_chat_id():
    cfg = {"slack": {"quiet_channels": "C1,C2"}}
    assert _is_quiet_channel(_src("C1"), cfg) is True
    assert _is_quiet_channel(_src("C9"), cfg) is False


def test_is_quiet_channel_matches_parent_for_threads():
    cfg = {"slack": {"quiet_channels": "C1"}}
    assert _is_quiet_channel(_src("T123", parent="C1"), cfg) is True


def test_is_quiet_channel_false_for_non_slack():
    cfg = {"slack": {"quiet_channels": "C1"}}
    assert _is_quiet_channel(_src("C1", platform=Platform.DISCORD), cfg) is False


def test_is_quiet_channel_false_when_unconfigured():
    assert _is_quiet_channel(_src("C1"), {}) is False
    assert _is_quiet_channel(_src("C1"), {"slack": {}}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/gateway/test_quiet_channels.py`
Expected: FAIL with `ImportError: cannot import name '_parse_channel_id_list'`.

- [ ] **Step 3: Add the helpers**

In `gateway/run.py`, immediately after `_load_gateway_config()` (after line 1432), add:

```python
def _parse_channel_id_list(value) -> set:
    """Parse a comma-separated channel-ID string into a set of trimmed IDs.

    Mirrors how ``free_response_channels`` is expressed in config.yaml.
    Returns an empty set for None/empty/non-string input.
    """
    if not value or not isinstance(value, str):
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


def _is_quiet_channel(source, cfg: dict) -> bool:
    """Return True when *source* is a Slack channel listed in slack.quiet_channels.

    Matches the triggering channel (``chat_id``) or, for threads, the parent
    channel (``parent_chat_id``) so thread replies inherit the channel's setting.
    Only applies to Slack; other platforms always return False.
    """
    from gateway.config import Platform
    if getattr(source, "platform", None) != Platform.SLACK:
        return False
    quiet = _parse_channel_id_list(
        (cfg.get("slack") or {}).get("quiet_channels")
    )
    if not quiet:
        return False
    candidates = {
        c for c in (getattr(source, "chat_id", None),
                    getattr(source, "parent_chat_id", None)) if c
    }
    return bool(candidates & quiet)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/gateway/test_quiet_channels.py`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py tests/gateway/test_quiet_channels.py
git commit -m "feat(slack): add quiet-channel resolution helpers"
```

---

## Task 3: Gate the empty-response warning for quiet channels

Add a `quiet_completion_ok` flag to `_normalize_empty_agent_response`. When set, a *successful empty* turn (api_calls>0, not failed, not partial, not interrupted) returns `""` (silent) instead of the "no response was generated" warning. Errors and partial failures still surface.

**Files:**
- Modify: `gateway/run.py:1558-1601` (`_normalize_empty_agent_response`)
- Test: `tests/gateway/test_quiet_channels.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/gateway/test_quiet_channels.py`:

```python
from gateway.run import _normalize_empty_agent_response


def test_normalize_suppresses_empty_success_when_quiet():
    result = {"api_calls": 2}  # did work, no error, no partial
    out = _normalize_empty_agent_response(result, "", quiet_completion_ok=True)
    assert out == ""


def test_normalize_keeps_empty_success_warning_when_not_quiet():
    result = {"api_calls": 2}
    out = _normalize_empty_agent_response(result, "", quiet_completion_ok=False)
    assert "no response was generated" in out


def test_normalize_surfaces_errors_even_when_quiet():
    result = {"failed": True, "error": "boom"}
    out = _normalize_empty_agent_response(result, "", quiet_completion_ok=True)
    assert "boom" in out


def test_normalize_surfaces_partial_even_when_quiet():
    result = {"api_calls": 1, "partial": True, "error": "stopped early"}
    out = _normalize_empty_agent_response(result, "", quiet_completion_ok=True)
    assert "stopped early" in out


def test_normalize_passes_through_real_text_when_quiet():
    out = _normalize_empty_agent_response({"api_calls": 1}, "hello", quiet_completion_ok=True)
    assert out == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/gateway/test_quiet_channels.py -k normalize`
Expected: FAIL — `_normalize_empty_agent_response()` got an unexpected keyword argument `quiet_completion_ok`.

- [ ] **Step 3: Add the parameter**

In `gateway/run.py`, change the signature and the success-catch-all branch of `_normalize_empty_agent_response`:

Signature (lines 1558-1563) becomes:

```python
def _normalize_empty_agent_response(
    agent_result: dict,
    response: str,
    *,
    history_len: int = 0,
    quiet_completion_ok: bool = False,
) -> str:
```

Then change the final success-with-no-text branch (currently lines 1591-1599) to:

```python
    api_calls = int(agent_result.get("api_calls", 0) or 0)
    if api_calls > 0 and not agent_result.get("interrupted"):
        if agent_result.get("partial"):
            err = agent_result.get("error", "processing incomplete")
            return f"⚠️ Processing stopped: {str(err)[:200]}. Try again."
        if quiet_completion_ok:
            # Quiet channel: a successful turn that produced no text is a
            # legitimate emoji-only completion. Stay silent instead of warning.
            return ""
        return (
            "⚠️ Processing completed but no response was generated. "
            "This may be a transient error — try sending your message again."
        )

    return response
```

(The `failed` branch above it is unchanged, so errors still surface.)

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/gateway/test_quiet_channels.py -k normalize`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py tests/gateway/test_quiet_channels.py
git commit -m "feat(slack): allow silent empty completion in quiet channels"
```

---

## Task 4: Wire quiet detection into the two run.py call sites

Pass `quiet_completion_ok` to the normalize call, and force `tool_progress` off for quiet channels. Both sites already have `source` and the loaded config in scope.

**Files:**
- Modify: `gateway/run.py:8867` (normalize call), `gateway/run.py:15962` (tool-progress)

- [ ] **Step 1: Wire the normalize call site**

At `gateway/run.py:8867`, the call currently reads:

```python
            response = _normalize_empty_agent_response(
                agent_result, response, history_len=len(history),
            )
```

Change it to:

```python
            response = _normalize_empty_agent_response(
                agent_result, response, history_len=len(history),
                quiet_completion_ok=_is_quiet_channel(source, _load_gateway_config()),
            )
```

- [ ] **Step 2: Wire the tool-progress site**

At `gateway/run.py`, just after `progress_mode` is computed (the block ending at line 15958) and before `tool_progress_enabled` is set (line 15962), insert:

```python
        # Quiet channels hide tool-progress entirely, regardless of global config.
        if _is_quiet_channel(source, user_config):
            progress_mode = "off"
```

(`user_config` is already loaded at line 15909; `source` is the function parameter.)

- [ ] **Step 3: Verify nothing else broke (run the gateway suite slice)**

Run: `scripts/run_tests.sh tests/gateway/test_quiet_channels.py tests/gateway/ -k "quiet or normalize"`
Expected: PASS (no regressions; existing gateway tests unaffected).

- [ ] **Step 4: Commit**

```bash
git add gateway/run.py
git commit -m "feat(slack): apply quiet-channel behavior at run.py call sites"
```

---

## Task 5: Create the `slack_react` tool

A self-registering tool. The handler reads target channel/message from session contextvars (or explicit args), resolves the Slack bot token, and POSTs to `reactions.add`/`reactions.remove`. Network call is isolated in `_post_reaction()` so tests can monkeypatch it.

**Files:**
- Create: `tools/slack_react_tool.py`
- Test: `tests/tools/test_slack_react_tool.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_slack_react_tool.py`:

```python
import json
import pytest

import tools.slack_react_tool as srt


@pytest.fixture(autouse=True)
def _fake_post(monkeypatch):
    calls = []

    async def fake_post(token, channel, ts, emoji, remove):
        calls.append({"token": token, "channel": channel, "ts": ts,
                      "emoji": emoji, "remove": remove})
        return {"ok": True}

    monkeypatch.setattr(srt, "_post_reaction", fake_post)
    monkeypatch.setattr(srt, "_resolve_slack_token", lambda: "xoxb-test")
    return calls


def _run(args):
    # Tool is registered is_async=True; call the underlying coroutine directly.
    from model_tools import _run_async
    return json.loads(_run_async(srt._slack_react_handler(args)))


def test_defaults_to_session_message_and_channel(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1",
        "HERMES_SESSION_MESSAGE_ID": "111.222",
    }.get(k, d))
    out = _run({"emoji": "party_sloth"})
    assert out["success"] is True
    assert _fake_post[0] == {"token": "xoxb-test", "channel": "C1",
                             "ts": "111.222", "emoji": "party_sloth", "remove": False}


def test_strips_colons_from_emoji(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    _run({"emoji": ":white_check_mark:"})
    assert _fake_post[0]["emoji"] == "white_check_mark"


def test_explicit_message_id_overrides_session(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    _run({"emoji": "eyes", "message_id": "9.9"})
    assert _fake_post[0]["ts"] == "9.9"


def test_remove_flag(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    _run({"emoji": "eyes", "remove": True})
    assert _fake_post[0]["remove"] is True


def test_error_when_no_emoji(monkeypatch, _fake_post):
    out = _run({})
    assert "error" in out


def test_error_when_no_target_channel(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": "")  # nothing in context
    out = _run({"emoji": "eyes"})
    assert "error" in out


def test_error_when_no_token(monkeypatch, _fake_post):
    monkeypatch.setattr(srt, "_session", lambda k, d="": {
        "HERMES_SESSION_CHAT_ID": "C1", "HERMES_SESSION_MESSAGE_ID": "1.2"}.get(k, d))
    monkeypatch.setattr(srt, "_resolve_slack_token", lambda: "")
    out = _run({"emoji": "eyes"})
    assert "error" in out


def test_registered_in_registry():
    from tools.registry import registry
    import tools.slack_react_tool  # noqa: F401  (ensure import/registration)
    assert registry.get_entry("slack_react") is not None
    assert registry.get_toolset_for_tool("slack_react") == "slack"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_slack_react_tool.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools.slack_react_tool'`.

- [ ] **Step 3: Create the tool**

Create `tools/slack_react_tool.py`:

```python
"""slack_react — let the agent add/remove a Slack emoji reaction.

Mirrors ``tools/send_message_tool.py:_send_slack``: a fresh aiohttp session
posted to the Slack Web API, so the call is safe from any event loop (the tool
runs in a worker thread bridged by the registry's ``_run_async``). Targets the
triggering message via session contextvars unless an explicit message_id is
given. Intended for quiet/observer Slack channels (emoji-first), but usable in
any Slack channel.
"""

from __future__ import annotations

import os

from tools.registry import registry, tool_error, tool_result


SLACK_REACT_SCHEMA = {
    "name": "slack_react",
    "description": (
        "Add (or remove) an emoji reaction on a Slack message. By default it "
        "reacts to the message that triggered the current turn — ideal for "
        "acknowledging or signaling completion without posting a text reply. "
        "Provide the emoji by its Slack short name WITHOUT colons "
        "(e.g. 'white_check_mark', 'eyes', 'party_sloth')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "description": "Slack emoji short name without colons, e.g. 'party_sloth'.",
            },
            "message_id": {
                "type": "string",
                "description": "Target message timestamp (ts). Defaults to the triggering message.",
            },
            "remove": {
                "type": "boolean",
                "description": "Remove the reaction instead of adding it. Defaults to false.",
                "default": False,
            },
        },
        "required": ["emoji"],
    },
}


def _session(name: str, default: str = "") -> str:
    """Thin wrapper around session context (indirection for tests)."""
    from gateway.session_context import get_session_env
    return get_session_env(name, default)


def _resolve_slack_token() -> str:
    """Resolve the Slack bot token from gateway config, falling back to env."""
    try:
        from gateway.config import load_gateway_config, Platform
        cfg = load_gateway_config()
        pconfig = cfg.platforms.get(Platform.SLACK)
        if pconfig and getattr(pconfig, "token", ""):
            return pconfig.token
    except Exception:
        pass
    return os.getenv("SLACK_BOT_TOKEN", "").strip()


async def _post_reaction(token: str, channel: str, ts: str, emoji: str, remove: bool) -> dict:
    """POST to Slack reactions.add / reactions.remove. Returns parsed JSON."""
    import aiohttp
    from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp

    method = "reactions.remove" if remove else "reactions.add"
    url = f"https://slack.com/api/{method}"
    _proxy = resolve_proxy_url()
    _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"channel": channel, "timestamp": ts, "name": emoji}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), **_sess_kw) as session:
        async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:
            return await resp.json()


async def _slack_react_handler(args: dict, **_kw) -> str:
    emoji = (args.get("emoji") or "").strip().strip(":")
    if not emoji:
        return tool_error("'emoji' is required (Slack short name without colons).")

    channel = (args.get("channel") or _session("HERMES_SESSION_CHAT_ID")).strip()
    ts = (args.get("message_id") or _session("HERMES_SESSION_MESSAGE_ID")).strip()
    if not channel or not ts:
        return tool_error(
            "No target Slack message in context. slack_react only works on a live "
            "Slack turn (or pass an explicit message_id)."
        )

    token = _resolve_slack_token()
    if not token:
        return tool_error("Slack bot token not configured (SLACK_BOT_TOKEN).")

    remove = bool(args.get("remove", False))
    try:
        data = await _post_reaction(token, channel, ts, emoji, remove)
    except Exception as e:  # network / aiohttp errors
        return tool_error(f"Slack reaction request failed: {e}")

    if data.get("ok"):
        return tool_result(success=True, emoji=emoji, channel=channel, ts=ts, removed=remove)
    # 'already_reacted' / 'no_reaction' are benign no-ops — report softly.
    err = data.get("error", "unknown")
    if err in {"already_reacted", "no_reaction"}:
        return tool_result(success=True, emoji=emoji, noop=err)
    return tool_error(f"Slack API error: {err}")


def _check_slack_react() -> bool:
    """Available whenever a Slack token is resolvable."""
    return bool(_resolve_slack_token())


registry.register(
    name="slack_react",
    toolset="slack",
    schema=SLACK_REACT_SCHEMA,
    handler=lambda args, **kw: _slack_react_handler(args, **kw),
    check_fn=_check_slack_react,
    requires_env=[],
    is_async=True,
    emoji="🦥",
    max_result_size_chars=2000,
)
```

(`tool_result(**kwargs)` and `tool_error(message, **extra)` both accept the keyword form used above — confirmed in `tools/registry.py:563-589`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_slack_react_tool.py`
Expected: PASS (8 tests). The `test_registered_in_registry` test fails until Task 6 if `resolve_toolset`/toolset wiring is checked — but `get_toolset_for_tool` reads the registry directly, so it passes now.

- [ ] **Step 5: Commit**

```bash
git add tools/slack_react_tool.py tests/tools/test_slack_react_tool.py
git commit -m "feat(slack): add slack_react agent tool"
```

---

## Task 6: Register the `slack` toolset

**Files:**
- Modify: `toolsets.py` (after the `discord_admin` entry, ~line 282)
- Test: `tests/test_toolsets.py` (or create `tests/test_slack_toolset.py`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_slack_toolset.py`:

```python
def test_slack_toolset_resolves_to_slack_react():
    import tools.slack_react_tool  # noqa: F401  (register the tool)
    from toolsets import resolve_toolset
    assert "slack_react" in resolve_toolset("slack")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/test_slack_toolset.py`
Expected: FAIL — `resolve_toolset("slack")` returns empty / raises for unknown toolset.

- [ ] **Step 3: Add the toolset**

In `toolsets.py`, after the `discord_admin` block (ends ~line 281), add:

```python
    "slack": {
        "description": "Slack interaction tools (emoji reactions on messages)",
        "tools": ["slack_react"],
        "includes": [],
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/test_slack_toolset.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add toolsets.py tests/test_slack_toolset.py
git commit -m "feat(slack): register slack toolset for slack_react"
```

---

## Task 7: Platform-restrict the `slack` toolset

Make the `slack` toolset show only for the Slack platform in the `hermes tools` UI, mirroring `discord`.

**Files:**
- Modify: `hermes_cli/tools_config.py:54` (CONFIGURABLE_TOOLSETS) and `:132` (_TOOLSET_PLATFORM_RESTRICTIONS)
- Test: `tests/hermes_cli/test_tools_config.py` (or create)

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/test_slack_toolset_restriction.py`:

```python
from hermes_cli.tools_config import _toolset_allowed_for_platform


def test_slack_toolset_only_on_slack():
    assert _toolset_allowed_for_platform("slack", "slack") is True
    assert _toolset_allowed_for_platform("slack", "discord") is False
    assert _toolset_allowed_for_platform("slack", "telegram") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/hermes_cli/test_slack_toolset_restriction.py`
Expected: FAIL — with no restriction entry, `slack` is allowed on all platforms (returns True for discord).

- [ ] **Step 3: Add the restriction + configurable entry**

In `hermes_cli/tools_config.py`, add to `_TOOLSET_PLATFORM_RESTRICTIONS` (lines 132-135):

```python
_TOOLSET_PLATFORM_RESTRICTIONS: Dict[str, Set[str]] = {
    "discord": {"discord"},
    "discord_admin": {"discord"},
    "slack": {"slack"},
}
```

And add to `CONFIGURABLE_TOOLSETS` (near the `discord` tuple, ~line 77):

```python
    ("slack", "💬 Slack (reactions)", "react to Slack messages with emoji"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/hermes_cli/test_slack_toolset_restriction.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/tools_config.py tests/hermes_cli/test_slack_toolset_restriction.py
git commit -m "feat(slack): platform-restrict slack toolset to Slack"
```

---

## Task 8: RBAC coverage for `slack_react`

No new code — the RBAC policy gates by **toolset**, and the `pre_tool_call` backstop maps a tool to its toolset (`registry.get_toolset_for_tool`) before calling `ToolAccessPolicy.can_use_tool(user_id, toolset)`. Add a regression test proving (a) `slack_react` resolves to the `slack` toolset, (b) a role granting `slack` permits it, a no-tools role does not, and (c) `admin` (`*`) permits it.

**Files:**
- Test: `tests/gateway/test_tool_access.py` (append)

- [ ] **Step 1: Write the test**

Append to `tests/gateway/test_tool_access.py`. Build policies with `policy_from_extra` (the public builder; `ToolAccessPolicy` is a frozen dataclass, not a kwargs constructor):

```python
def test_slack_react_maps_to_slack_toolset():
    import tools.slack_react_tool  # noqa: F401  (register the tool)
    from tools.registry import registry
    assert registry.get_toolset_for_tool("slack_react") == "slack"


def test_slack_toolset_gating():
    from gateway.tool_access import policy_from_extra
    policy = policy_from_extra({
        "user_roles": {"U_react": "reactor", "U_chat": "chat_only"},
        "roles": {"reactor": ["slack"], "chat_only": []},
    })
    # A role granting the slack toolset may react.
    assert policy.can_use_tool("U_react", "slack") is True
    # A no-tools role may not.
    assert policy.can_use_tool("U_chat", "slack") is False


def test_admin_wildcard_allows_slack_toolset():
    from gateway.tool_access import policy_from_extra
    policy = policy_from_extra({
        "user_roles": {"U_admin": "admin"},
        "roles": {"admin": ["*"]},
    })
    assert policy.can_use_tool("U_admin", "slack") is True
```

- [ ] **Step 2: Run the tests**

Run: `scripts/run_tests.sh tests/gateway/test_tool_access.py -k "slack"`
Expected: PASS (3 tests). If `_coerce_roles`/`_coerce_user_roles` reject the inline dict shape, mirror the exact `extra` shape used by the existing tests in this file (they already exercise `policy_from_extra`).

- [ ] **Step 3: Commit**

```bash
git add tests/gateway/test_tool_access.py
git commit -m "test(slack): RBAC coverage for slack_react toolset gating"
```

---

## Task 9: Documentation

**Files:**
- Modify: `CLAUDE.md` (fork-specific section)

- [ ] **Step 1: Add a fork-feature subsection**

In `CLAUDE.md`, under the fork-specific work area, add:

```markdown
### Slack quiet channels + `slack_react`

For low-noise "hidden assistant" channels. Config under the top-level `slack:`
block in `~/.hermes/config.yaml`:

- `quiet_channels: 'C123,C456'` — comma-separated channel IDs (mirrors
  `free_response_channels`). In these channels the gateway forces
  `tool_progress: off` and allows **silent (emoji-only) completion**: a
  successful turn that produces no text stays silent instead of posting the
  "no response generated" warning. Resolved in [gateway/run.py](gateway/run.py)
  via `_is_quiet_channel` (matches `chat_id` or thread `parent_chat_id`).
  Errors, approvals, and clarifications still surface. **Text replies are never
  suppressed** — the bot can always answer.
- `slack_react` tool ([tools/slack_react_tool.py](tools/slack_react_tool.py)) —
  lets the agent add/remove an emoji reaction on the triggering Slack message
  (or an explicit `message_id`). Lives in the platform-restricted `slack`
  toolset; grant it to a role via `slack.roles`. Emoji-first behavior is driven
  by `channel_prompts`, not enforced.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(slack): document quiet_channels + slack_react"
```

---

## Task 10: Full verification

- [ ] **Step 1: Run the full affected suites**

Run:
```bash
scripts/run_tests.sh tests/tools/test_slack_react_tool.py tests/gateway/test_quiet_channels.py tests/test_slack_toolset.py tests/hermes_cli/test_slack_toolset_restriction.py tests/gateway/test_tool_access.py tests/hermes_cli/test_config.py
```
Expected: all PASS.

- [ ] **Step 2: Lint / typecheck**

Run:
```bash
ruff check tools/slack_react_tool.py gateway/run.py
ty check
```
Expected: clean (ruff is near-disabled; fix any PLW1514 issues).

- [ ] **Step 3: Sanity-check tool discovery end-to-end**

Run:
```bash
python -c "import model_tools; from tools.registry import registry; print('slack_react' in registry.get_all_tool_names())"
```
Expected: `True`.

- [ ] **Step 4: Final commit (if any cleanup)**

```bash
git add -A
git commit -m "chore(slack): quiet channels + slack_react cleanup" || true
```

---

## Manual verification (post-merge, on the VM gateway)

The gateway runs on the GCP VM; local config changes do not affect it. To try the feature:

1. On the VM's `~/.hermes/config.yaml`, under `slack:` set `quiet_channels: 'C03B4BC9D2P'`, grant the `slack` toolset to the relevant role, and add a `channel_prompts` entry instructing emoji-first behavior with `slack_react`.
2. Restart/reload the gateway.
3. @mention the bot in that channel with a small task. Expect: 👀 → work happens (no tool-progress lines) → the agent's chosen emoji (e.g. 🦥) appears → no text reply.
4. Ask it a direct question ("explain what you changed"). Expect: a normal text reply (silence is not enforced).
