# Relevance Pre-Gate for Quiet Channels — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In a quiet Slack channel, run a cheap classifier on each non-@mention message *before* the full agent, so the agent only runs when the message is actually relevant (act) and stays silent otherwise (ignore).

**Architecture:** A core async gate in the gateway dispatch (`_handle_message`), right after the `pre_gateway_dispatch` hook. Pure module-level helpers decide the purpose and classify relevance (`async_call_llm`); the orchestrator fetches optional thread context and returns whether to skip. Coupled to `quiet_channels`; @mention/DM bypass; fail-open on classifier error.

**Tech Stack:** Python, the gateway (`gateway/run.py`, `gateway/platforms/slack.py`, `gateway/platforms/base.py`), `agent/auxiliary_client.async_call_llm`. Tests via `scripts/run_tests.sh` (never bare pytest).

**Spec:** [docs/superpowers/specs/2026-06-09-relevance-pre-gate-design.md](../specs/2026-06-09-relevance-pre-gate-design.md)

---

## File Structure

**Modify:**
- `hermes_cli/config.py` — add `relevance_gate_model` + `relevance_gate_purpose` defaults to the `slack` block.
- `gateway/platforms/base.py` — add `directly_addressed: bool = False` to `MessageEvent`.
- `gateway/platforms/slack.py` — set `directly_addressed` when building the inbound `MessageEvent`.
- `gateway/run.py` — add `_relevance_gate_purpose`, `_classify_relevance`, `_relevance_gate_should_skip` (module-level); call the gate in `_handle_message`.
- `CLAUDE.md` — document the feature.

**Test:**
- `tests/gateway/test_relevance_gate.py` (create) — helpers + orchestrator (model mocked).
- `tests/hermes_cli/test_config.py` — config defaults.
- `tests/gateway/test_slack.py` — adapter sets `directly_addressed`.

**Conventions:** `_is_quiet_channel` / `_parse_channel_id_list` already exist in `gateway/run.py` and read the raw `slack:` block from `_load_gateway_config()`. The gate reads config the same way (no bridging needed). Tests must not write to `~/.hermes/`.

---

## Task 1: Config defaults

**Files:**
- Modify: `hermes_cli/config.py` (the `slack` block in `DEFAULT_CONFIG`, near `quiet_channels`)
- Test: `tests/hermes_cli/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/hermes_cli/test_config.py` (near `test_default_config_includes_slack_quiet_channels`; match the surrounding class/method style):

```python
def test_default_config_includes_relevance_gate_keys(self):
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["slack"]["relevance_gate_model"] == ""
    assert DEFAULT_CONFIG["slack"]["relevance_gate_purpose"] == {}
```

- [ ] **Step 2: Run, verify it FAILS**

Run: `scripts/run_tests.sh tests/hermes_cli/test_config.py -k relevance_gate`
Expected: FAIL (`KeyError: 'relevance_gate_model'`).

- [ ] **Step 3: Add the defaults**

In `hermes_cli/config.py`, in the `slack` block (currently has `quiet_channels`), add after `quiet_channels`:

```python
        "quiet_channels": "",          # Comma-separated channel IDs: hide tool-progress + allow emoji-only (silent) completion
        "relevance_gate_model": "",    # Cheap/fast model for the quiet-channel relevance pre-gate (empty = use main turn model)
        "relevance_gate_purpose": {},  # Per-channel {chat_id: purpose}; classifier "what to act on" (falls back to channel_prompts)
```

- [ ] **Step 4: Run, verify it PASSES**

Run: `scripts/run_tests.sh tests/hermes_cli/test_config.py -k relevance_gate` → PASS.
Also run the whole file once: `scripts/run_tests.sh tests/hermes_cli/test_config.py` and fix any migration test that asserts the exact slack key set (add the two keys there if such a test exists; otherwise no change).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/config.py tests/hermes_cli/test_config.py
git commit -m "feat(slack): add relevance_gate_model + relevance_gate_purpose config defaults"
```

---

## Task 2: `MessageEvent.directly_addressed` field

**Files:**
- Modify: `gateway/platforms/base.py` (`MessageEvent`, near the `internal: bool = False` field ~line 1463)
- Test: `tests/gateway/test_relevance_gate.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/gateway/test_relevance_gate.py`:

```python
def test_message_event_directly_addressed_defaults_false():
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from gateway.config import Platform
    ev = MessageEvent(text="hi", source=SessionSource(platform=Platform.SLACK, chat_id="C1"))
    assert ev.directly_addressed is False
```

- [ ] **Step 2: Run, verify it FAILS**

Run: `scripts/run_tests.sh tests/gateway/test_relevance_gate.py -k directly_addressed`
Expected: FAIL (`AttributeError: 'MessageEvent' object has no attribute 'directly_addressed'`).

- [ ] **Step 3: Add the field**

In `gateway/platforms/base.py`, in the `MessageEvent` dataclass, add (next to `internal: bool = False`):

```python
    # True when the bot was explicitly addressed (Slack @mention or DM). Lets the
    # relevance pre-gate bypass classification for directly-addressed messages.
    directly_addressed: bool = False
```

- [ ] **Step 4: Run, verify it PASSES**

Run: `scripts/run_tests.sh tests/gateway/test_relevance_gate.py -k directly_addressed` → PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/platforms/base.py tests/gateway/test_relevance_gate.py
git commit -m "feat(gateway): add MessageEvent.directly_addressed flag"
```

---

## Task 3: Slack adapter sets `directly_addressed`

**Files:**
- Modify: `gateway/platforms/slack.py` (the inbound `msg_event = MessageEvent(...)` construction, ~line 2622; `is_dm` ~2279 and `is_mentioned` ~2304 are in scope)
- Test: `tests/gateway/test_slack.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/gateway/test_slack.py` in the `TestProgressMessageThread` class (it has the `adapter` fixture and the `_handle_slack_message` capture pattern):

```python
    @pytest.mark.asyncio
    async def test_dm_sets_directly_addressed(self, adapter):
        event = {
            "channel": "D_DM", "channel_type": "im", "user": "U_USER",
            "text": "hello", "ts": "1700000000.000010",
        }
        captured = []
        adapter.handle_message = AsyncMock(side_effect=lambda e: captured.append(e))
        with patch.object(adapter, "_resolve_user_name", new=AsyncMock(return_value="testuser")):
            await adapter._handle_slack_message(event)
        assert len(captured) == 1
        assert captured[0].directly_addressed is True
```

- [ ] **Step 2: Run, verify it FAILS**

Run: `scripts/run_tests.sh tests/gateway/test_slack.py` (the parallel wrapper takes paths, not `-k`; the new test fails because `directly_addressed` is `False`).
Expected: 1 failed (`assert False is True`).

- [ ] **Step 3: Set the flag on the event**

In `gateway/platforms/slack.py`, find the inbound `msg_event = MessageEvent(` construction (~line 2622). Add the kwarg (set from the existing `is_dm` and `is_mentioned` locals):

```python
        msg_event = MessageEvent(
            text=text,
            # ... existing kwargs unchanged ...
            directly_addressed=bool(is_dm or is_mentioned),
        )
```

(Insert the `directly_addressed=...` line among the existing kwargs. Confirm `is_dm` and `is_mentioned` are both defined above this point in the method — they are, at ~2279 and ~2304.)

- [ ] **Step 4: Run, verify it PASSES**

Run: `scripts/run_tests.sh tests/gateway/test_slack.py` → 0 failed (the new test passes; existing pass).

- [ ] **Step 5: Commit**

```bash
git add gateway/platforms/slack.py tests/gateway/test_slack.py
git commit -m "feat(slack): mark MessageEvent.directly_addressed on mention/DM"
```

---

## Task 4: `_relevance_gate_purpose` helper

Resolves the classifier purpose for a channel, or `None` when the channel isn't a quiet channel (gate inactive).

**Files:**
- Modify: `gateway/run.py` (add near `_is_quiet_channel`)
- Test: `tests/gateway/test_relevance_gate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/gateway/test_relevance_gate.py`:

```python
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.run import _relevance_gate_purpose

_DEFAULT_PURPOSE = (
    "Decide whether the assistant must take an action relevant to this "
    "channel; otherwise ignore."
)


def _src(chat_id="C1", platform=Platform.SLACK):
    return SessionSource(platform=platform, chat_id=chat_id, chat_type="channel")


def test_purpose_none_when_not_quiet_channel():
    assert _relevance_gate_purpose(_src("C1"), {"slack": {"quiet_channels": "C2"}}) is None


def test_purpose_explicit_map_wins():
    cfg = {"slack": {"quiet_channels": "C1",
                     "relevance_gate_purpose": {"C1": "track bugs"},
                     "channel_prompts": {"C1": "behavior prompt"}}}
    assert _relevance_gate_purpose(_src("C1"), cfg) == "track bugs"


def test_purpose_falls_back_to_channel_prompt():
    cfg = {"slack": {"quiet_channels": "C1", "channel_prompts": {"C1": "behavior prompt"}}}
    assert _relevance_gate_purpose(_src("C1"), cfg) == "behavior prompt"


def test_purpose_falls_back_to_default():
    cfg = {"slack": {"quiet_channels": "C1"}}
    assert _relevance_gate_purpose(_src("C1"), cfg) == _DEFAULT_PURPOSE
```

- [ ] **Step 2: Run, verify it FAILS**

Run: `scripts/run_tests.sh tests/gateway/test_relevance_gate.py -k purpose`
Expected: FAIL (`ImportError: cannot import name '_relevance_gate_purpose'`).

- [ ] **Step 3: Add the helper**

In `gateway/run.py`, immediately after the `_is_quiet_channel` function, add:

```python
_RELEVANCE_GATE_DEFAULT_PURPOSE = (
    "Decide whether the assistant must take an action relevant to this "
    "channel; otherwise ignore."
)


def _relevance_gate_purpose(source, cfg: dict):
    """Return the relevance-gate purpose for *source*'s channel, or None.

    None means the channel is not a quiet channel — the gate is inactive.
    Otherwise resolve: slack.relevance_gate_purpose[chat_id] →
    slack.channel_prompts[chat_id] → a generic default.
    """
    if not _is_quiet_channel(source, cfg):
        return None
    slack_cfg = cfg.get("slack") or {}
    chat_id = getattr(source, "chat_id", None)
    purposes = slack_cfg.get("relevance_gate_purpose") or {}
    if isinstance(purposes, dict):
        explicit = purposes.get(chat_id)
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
    prompts = slack_cfg.get("channel_prompts") or {}
    if isinstance(prompts, dict):
        prompt = prompts.get(chat_id)
        if isinstance(prompt, str) and prompt.strip():
            return prompt.strip()
    return _RELEVANCE_GATE_DEFAULT_PURPOSE
```

- [ ] **Step 4: Run, verify it PASSES**

Run: `scripts/run_tests.sh tests/gateway/test_relevance_gate.py -k purpose` → PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py tests/gateway/test_relevance_gate.py
git commit -m "feat(slack): relevance-gate purpose resolution helper"
```

---

## Task 5: `_classify_relevance` (async classifier)

**Files:**
- Modify: `gateway/run.py` (add after `_relevance_gate_purpose`)
- Test: `tests/gateway/test_relevance_gate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/gateway/test_relevance_gate.py`:

```python
import asyncio
import gateway.run as gr


class _FakeResp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


def _run(coro):
    return asyncio.run(coro)


# NOTE: _classify_relevance imports async_call_llm lazily from
# agent.auxiliary_client, so patch it on the SOURCE module (re-read each call).
def test_classify_act(monkeypatch):
    async def fake(**kw):
        return _FakeResp("act")
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake)
    assert _run(gr._classify_relevance("p", "msg", "", None)) is True


def test_classify_ignore(monkeypatch):
    async def fake(**kw):
        return _FakeResp("ignore")
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake)
    assert _run(gr._classify_relevance("p", "msg", "ctx", "gpt-5-nano")) is False


def test_classify_ignore_case_and_punctuation(monkeypatch):
    async def fake(**kw):
        return _FakeResp("  IGNORE.\n")
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake)
    assert _run(gr._classify_relevance("p", "msg", "", None)) is False


def test_classify_empty_or_garbage_fails_open_to_act(monkeypatch):
    async def fake(**kw):
        return _FakeResp("")
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake)
    assert _run(gr._classify_relevance("p", "msg", "", None)) is True


def test_classify_passes_model_through(monkeypatch):
    seen = {}
    async def fake(**kw):
        seen.update(kw)
        return _FakeResp("ignore")
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", fake)
    _run(gr._classify_relevance("PURPOSE", "the message", "the ctx", "gpt-5-nano"))
    assert seen.get("model") == "gpt-5-nano"
    assert seen.get("temperature") == 0
    # purpose, message and context must reach the model
    blob = str(seen.get("messages"))
    assert "PURPOSE" in blob and "the message" in blob and "the ctx" in blob
```

- [ ] **Step 2: Run, verify it FAILS**

Run: `scripts/run_tests.sh tests/gateway/test_relevance_gate.py -k classify`
Expected: FAIL (`AttributeError: module 'gateway.run' has no attribute '_classify_relevance'` / `async_call_llm`).

- [ ] **Step 3: Add the helper**

In `gateway/run.py`, add after `_relevance_gate_purpose`. Note the **lazy import** of `async_call_llm` (avoids any circular-import risk at module load, and lets tests patch `agent.auxiliary_client.async_call_llm`):

```python
async def _classify_relevance(purpose: str, message_text: str, thread_context: str, model) -> bool:
    """Return True ('act') unless the classifier clearly says 'ignore'.

    Lean-silent is enforced via the prompt (the model answers 'ignore' when
    unsure). Parse-level uncertainty (empty/garbage) returns True (act) so a
    real message is never silently dropped — fail-open. Raises propagate to the
    orchestrator, which also fails open.
    """
    from agent.auxiliary_client import async_call_llm
    system = (
        "You are a relevance filter for a Slack channel. Channel purpose: "
        f"{purpose}\n"
        "Decide if the assistant must ACT on the latest message (e.g. "
        "track/update/resolve something this channel is for) or IGNORE it "
        "(chatter, questions directed at people, general discussion). When "
        "unsure, answer IGNORE. Reply with exactly one word: act or ignore."
    )
    user = f"Recent thread context:\n{thread_context}\n\nLatest message:\n{message_text}"
    resp = await async_call_llm(
        model=model or None,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0,
        max_tokens=4,
    )
    try:
        content = (resp.choices[0].message.content or "").strip().lower()
    except Exception:
        content = ""
    # Skip only on an explicit 'ignore'; everything else (act / empty / garbage)
    # → act (fail-open).
    return not content.startswith("ignore")
```

- [ ] **Step 4: Run, verify it PASSES**

Run: `scripts/run_tests.sh tests/gateway/test_relevance_gate.py -k classify` → PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py tests/gateway/test_relevance_gate.py
git commit -m "feat(slack): relevance classifier via async_call_llm"
```

---

## Task 6: `_relevance_gate_should_skip` orchestrator

**Files:**
- Modify: `gateway/run.py` (add after `_classify_relevance`)
- Test: `tests/gateway/test_relevance_gate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/gateway/test_relevance_gate.py`:

```python
from types import SimpleNamespace
from gateway.run import _relevance_gate_should_skip


def _event(chat_id="C1", text="m", directly=False, thread_id=None, chat_type="channel",
           platform=Platform.SLACK):
    src = SessionSource(platform=platform, chat_id=chat_id, chat_type=chat_type,
                        thread_id=thread_id, message_id="1.1")
    return SimpleNamespace(text=text, source=src, directly_addressed=directly)


_QUIET_CFG = {"slack": {"quiet_channels": "C1"}}


def test_skip_when_classifier_says_ignore():
    async def classify(*a, **k):
        return False  # ignore
    assert _run(_relevance_gate_should_skip(_event(), _QUIET_CFG, None, classify=classify)) is True


def test_no_skip_when_classifier_says_act():
    async def classify(*a, **k):
        return True
    assert _run(_relevance_gate_should_skip(_event(), _QUIET_CFG, None, classify=classify)) is False


def test_no_skip_no_call_when_directly_addressed():
    called = {"n": 0}
    async def classify(*a, **k):
        called["n"] += 1
        return False
    res = _run(_relevance_gate_should_skip(_event(directly=True), _QUIET_CFG, None, classify=classify))
    assert res is False and called["n"] == 0


def test_no_skip_no_call_when_not_quiet_channel():
    called = {"n": 0}
    async def classify(*a, **k):
        called["n"] += 1
        return False
    cfg = {"slack": {"quiet_channels": "C2"}}
    res = _run(_relevance_gate_should_skip(_event("C1"), cfg, None, classify=classify))
    assert res is False and called["n"] == 0


def test_fail_open_on_classifier_error():
    async def classify(*a, **k):
        raise RuntimeError("boom")
    assert _run(_relevance_gate_should_skip(_event(), _QUIET_CFG, None, classify=classify)) is False
```

- [ ] **Step 2: Run, verify it FAILS**

Run: `scripts/run_tests.sh tests/gateway/test_relevance_gate.py -k "skip or fail_open or directly or not_quiet"`
Expected: FAIL (`ImportError: cannot import name '_relevance_gate_should_skip'`).

- [ ] **Step 3: Add the orchestrator**

In `gateway/run.py`, add after `_classify_relevance`:

```python
async def _relevance_gate_should_skip(event, cfg: dict, adapter, *, classify=_classify_relevance) -> bool:
    """Return True when a quiet-channel message should be skipped (no agent run).

    Gate is inactive (returns False) unless the channel is a quiet channel.
    Directly-addressed (@mention/DM) messages always run the agent. Classifier
    errors fail open (return False → agent runs). *adapter* is the platform
    adapter (for optional thread context); may be None. *classify* is injectable
    for tests.
    """
    source = getattr(event, "source", None)
    if source is None:
        return False
    purpose = _relevance_gate_purpose(source, cfg)
    if purpose is None:
        return False  # not a quiet channel — gate inactive
    if getattr(event, "directly_addressed", False):
        return False  # explicit @mention / DM → always act
    if getattr(source, "chat_type", "") == "dm":
        return False  # belt-and-suspenders

    model = (cfg.get("slack") or {}).get("relevance_gate_model") or None

    thread_context = ""
    thread_id = getattr(source, "thread_id", None)
    if adapter is not None and thread_id and hasattr(adapter, "_fetch_thread_context"):
        try:
            thread_context = await adapter._fetch_thread_context(
                channel_id=getattr(source, "chat_id", ""),
                thread_ts=thread_id,
                current_ts=getattr(source, "message_id", ""),
            ) or ""
        except Exception:
            thread_context = ""

    try:
        act = await classify(purpose, getattr(event, "text", "") or "", thread_context, model)
    except Exception as exc:
        logger.warning("relevance gate classifier failed — failing open (allow): %s", exc)
        return False  # fail-open

    return not act
```

- [ ] **Step 4: Run, verify it PASSES**

Run: `scripts/run_tests.sh tests/gateway/test_relevance_gate.py` → PASS (all gate tests).

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py tests/gateway/test_relevance_gate.py
git commit -m "feat(slack): relevance-gate orchestrator (quiet/mention/fail-open)"
```

---

## Task 7: Wire the gate into `_handle_message`

**Files:**
- Modify: `gateway/run.py` (`_handle_message`, right after the `pre_gateway_dispatch` hook loop ~line 7590)

- [ ] **Step 1: Add the gate call**

In `gateway/run.py`, in `async def _handle_message(self, event)`, locate the end of the `pre_gateway_dispatch` handling — the `for _result in _hook_results:` loop (~7571-7590). Immediately **after** that loop (and after the `is_internal` guard block it lives in), insert:

```python
        # Relevance pre-gate: in a quiet channel, a cheap classifier decides
        # whether this (non-@mention) message warrants running the full agent.
        # Skips silently when irrelevant; fail-open + @mention/DM bypass inside.
        if not is_internal:
            try:
                if await _relevance_gate_should_skip(
                    event,
                    _load_gateway_config(),
                    self.adapters.get(event.source.platform),
                ):
                    logger.info(
                        "relevance gate skip: platform=%s chat=%s",
                        event.source.platform.value if event.source.platform else "unknown",
                        event.source.chat_id or "unknown",
                    )
                    return None
            except Exception as _gate_exc:  # never let the gate break dispatch
                logger.warning("relevance gate raised — proceeding: %s", _gate_exc)
```

Confirm `is_internal`, `event`, and `self.adapters` are in scope at that point (they are — `is_internal` is set in the same method above the hook block, and `self.adapters` is the gateway's adapter dict).

- [ ] **Step 2: Verify no regression + syntax**

Run:
```bash
python -c "import ast; ast.parse(open('gateway/run.py').read()); print('run.py parses OK')"
scripts/run_tests.sh tests/gateway/test_relevance_gate.py tests/gateway/test_quiet_channels.py
```
Expected: parses OK; all PASS.

- [ ] **Step 3: Commit**

```bash
git add gateway/run.py
git commit -m "feat(slack): invoke relevance pre-gate in _handle_message"
```

---

## Task 8: Documentation

**Files:**
- Modify: `CLAUDE.md` (the "Slack quiet channels" subsection)

- [ ] **Step 1: Add the doc paragraph**

In `CLAUDE.md`, under the Slack quiet-channels subsection, add:

```markdown
- **Relevance pre-gate** (default for `quiet_channels`): before the full agent
  runs, a cheap classifier (`slack.relevance_gate_model`, empty = main model)
  decides act/ignore on each **non-@mention** message; `ignore` ends the turn
  with no agent run. Lives in `gateway/run.py` (`_relevance_gate_should_skip` →
  `_classify_relevance` via `async_call_llm`), invoked in `_handle_message`
  after `pre_gateway_dispatch`. @mention/DM bypass it (`MessageEvent.directly_addressed`,
  set in [slack.py](gateway/platforms/slack.py)). Purpose per channel:
  `slack.relevance_gate_purpose[chat_id]` → `channel_prompts[chat_id]` → default.
  **Fail-open**: classifier error → the agent runs (never silently drops a real
  message). Only active on Slack quiet channels; inert elsewhere.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(slack): document relevance pre-gate"
```

---

## Task 9: Full verification

- [ ] **Step 1: Run all affected suites**

```bash
scripts/run_tests.sh tests/gateway/test_relevance_gate.py tests/gateway/test_quiet_channels.py tests/gateway/test_slack.py tests/hermes_cli/test_config.py
```
Expected: all PASS.

- [ ] **Step 2: Lint / typecheck the changed files**

```bash
ruff check gateway/run.py gateway/platforms/slack.py gateway/platforms/base.py
ty check gateway/run.py
```
Expected: ruff clean. For `ty`, compare the diagnostic count to baseline — your new helpers must add **0** new diagnostics (the file has pre-existing dynamic-attribute diagnostics; confirm none reference `_relevance_gate_purpose`/`_classify_relevance`/`_relevance_gate_should_skip`).

- [ ] **Step 3: Import sanity**

```bash
python -c "import gateway.run as g; assert hasattr(g,'_relevance_gate_should_skip') and hasattr(g,'_classify_relevance') and hasattr(g,'_relevance_gate_purpose'); print('gate wired OK')"
```
Expected: `gate wired OK`.

---

## Manual verification (post-merge, on the VM)

1. On the VM config, the quiet channel already has `quiet_channels` + `channel_prompts`. Add `slack.relevance_gate_model: 'gpt-5-nano'` (or another cheap model) and optionally a `slack.relevance_gate_purpose` entry.
2. Restart the gateway.
3. Post off-topic chatter not addressed to the bot ("hey johnny, any news?") → expect **no agent run, no reply, no error** (classifier returns ignore).
4. Post / @mention an actual issue → expect the agent to run and act (create/update the tracked item, react).
5. Check `hermes logs` for `relevance gate skip:` lines on the ignored messages (confirms the gate fired and the full agent did not run).
