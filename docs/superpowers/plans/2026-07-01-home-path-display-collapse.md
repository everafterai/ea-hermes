# Collapse OS Home Directory to `~` in User-Facing Display — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide the OS home-directory prefix (and the username inside it) from all user-facing chat text by collapsing it to `~`, display-only.

**Architecture:** Add one memoized, boundary-safe helper `collapse_home_path(text)` in `agent/redact.py`, then apply it at four traced display boundaries: the tool-call preview builder, the gateway tool-progress render, the gateway status sanitizer, and the gateway final-reply sanitizer. It never touches paths passed to tools, written to logs, or fed into model context.

**Tech Stack:** Python 3.11, `pathlib.Path`, `re`, pytest via `scripts/run_tests.sh`.

## Global Constraints

- Tests must be run with `scripts/run_tests.sh` (CI-parity: unset creds, `TZ=UTC`, xdist, per-test subprocess isolation) — never bare `pytest`.
- Tests must not write to `~/.hermes/` (autouse fixture redirects `HERMES_HOME`); no change-detector tests.
- Display-only: the helper must NOT be added to `redact_sensitive_text`, `RedactingFormatter`, or `context_compressor` (those keep real paths).
- Always-on, no new config block.
- Only the OS home (`Path.home()`) is collapsed; a custom `HERMES_HOME` outside home is left as-is.
- Boundary-safe: never collapse a home prefix that is followed by a username-continuation char (`[A-Za-z0-9_-]`), and never collapse when `Path.home()` is `/`, empty, or shorter than 4 chars.
- Helper must be idempotent.

---

### Task 1: `collapse_home_path` helper in `agent/redact.py`

**Files:**
- Modify: `agent/redact.py` (add `from pathlib import Path` to imports; add helper + memo cache near the other module-level helpers, e.g. after `mask_secret`/`_mask_token`)
- Test: `tests/agent/test_redact.py`

**Interfaces:**
- Consumes: nothing (uses `pathlib.Path`, `re`).
- Produces: `collapse_home_path(text: str) -> str` — replaces the `str(Path.home())` prefix with `~` at a username boundary; returns input unchanged when text is falsy or home is unsafe (`/`, empty, `< 4` chars). Idempotent. Importable as `from agent.redact import collapse_home_path`.

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/agent/test_redact.py`:

```python
class TestCollapseHomePath:
    def test_collapses_hermes_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        from agent.redact import collapse_home_path
        assert collapse_home_path("/home/testuser/.hermes/config.yaml") == "~/.hermes/config.yaml"

    def test_collapses_non_hermes_home_path(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        from agent.redact import collapse_home_path
        assert collapse_home_path("/home/testuser/repos/x") == "~/repos/x"

    def test_collapses_bare_home_at_boundary(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        from agent.redact import collapse_home_path
        assert collapse_home_path("cwd is /home/testuser") == "cwd is ~"

    def test_collapses_home_embedded_in_sentence(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        from agent.redact import collapse_home_path
        assert collapse_home_path('saved to "/home/testuser/.hermes/x"') == 'saved to "~/.hermes/x"'

    def test_different_user_sharing_prefix_untouched(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        from agent.redact import collapse_home_path
        assert collapse_home_path("/home/testuser2/x") == "/home/testuser2/x"
        assert collapse_home_path("/home/testuserfoo") == "/home/testuserfoo"

    def test_non_home_absolute_path_untouched(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        from agent.redact import collapse_home_path
        assert collapse_home_path("/opt/hermes/data") == "/opt/hermes/data"

    def test_idempotent(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        from agent.redact import collapse_home_path
        once = collapse_home_path("/home/testuser/.hermes/x")
        assert collapse_home_path(once) == once == "~/.hermes/x"

    def test_root_home_guard_no_collapse(self, monkeypatch):
        monkeypatch.setenv("HOME", "/")
        from agent.redact import collapse_home_path
        assert collapse_home_path("/etc/passwd") == "/etc/passwd"
        assert collapse_home_path("/home/x") == "/home/x"

    def test_empty_and_none_safe(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        from agent.redact import collapse_home_path
        assert collapse_home_path("") == ""
        assert collapse_home_path(None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/agent/test_redact.py::TestCollapseHomePath -v`
Expected: FAIL with `ImportError: cannot import name 'collapse_home_path'`.

- [ ] **Step 3: Add `from pathlib import Path` to the imports**

At the top of `agent/redact.py`, the current imports are:

```python
import logging
import os
import re
```

Change to:

```python
import logging
import os
import re
from pathlib import Path
```

- [ ] **Step 4: Implement the helper**

Add this to `agent/redact.py` (place it after `_mask_token`, before `_redact_query_string`):

```python
# ── OS home-dir collapse (display-only) ──────────────────────────────────
# Memoized (home_str -> compiled regex | None) so the hot progress/preview
# paths don't recompile. Keyed on the resolved home so a monkeypatched HOME
# in tests, or a profile switch, picks up a fresh pattern.
_HOME_COLLAPSE_CACHE: "dict[str, re.Pattern[str] | None]" = {}


def _home_collapse_pattern(home: str) -> "re.Pattern[str] | None":
    """Compiled regex matching the home prefix at a username boundary.

    Returns None when *home* is unsafe to collapse (empty, ``/``, or shorter
    than 4 chars) so we never rewrite unrelated paths.
    """
    if home not in _HOME_COLLAPSE_CACHE:
        h = home.rstrip("/")
        if not h or h == "/" or len(h) < 4:
            _HOME_COLLAPSE_CACHE[home] = None
        else:
            # Collapse the prefix only when the next char is NOT a
            # username-continuation char, so /home/bob2 (a different user
            # sharing the prefix) is left untouched.
            _HOME_COLLAPSE_CACHE[home] = re.compile(
                re.escape(h) + r"(?![A-Za-z0-9_-])"
            )
    return _HOME_COLLAPSE_CACHE[home]


def collapse_home_path(text: str) -> str:
    """Replace the OS home-dir prefix with ``~`` in user-facing display text.

    Display-only: hides the OS username (e.g. ``/home/shaidiamant/.hermes`` ->
    ``~/.hermes``) from people the gateway serves. Never mutates paths passed
    to tools, written to logs, or fed into model context. Idempotent.
    """
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    try:
        home = str(Path.home())
    except Exception:
        return text
    pattern = _home_collapse_pattern(home)
    if pattern is None:
        return text
    return pattern.sub("~", text)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/agent/test_redact.py::TestCollapseHomePath -v`
Expected: PASS (9 tests).

- [ ] **Step 6: Run the full redact test file to check for regressions**

Run: `scripts/run_tests.sh tests/agent/test_redact.py`
Expected: PASS (all existing + new).

- [ ] **Step 7: Commit**

```bash
git add agent/redact.py tests/agent/test_redact.py
git commit -m "feat(display): add collapse_home_path helper (OS home -> ~)"
```

---

### Task 2: Collapse in `build_tool_preview` (`agent/display.py`)

**Files:**
- Modify: `agent/display.py` (add import; apply collapse in `build_tool_preview` general path, ~line 257)
- Test: `tests/agent/test_display.py`

**Interfaces:**
- Consumes: `collapse_home_path` from `agent.redact` (Task 1).
- Produces: `build_tool_preview` returns previews with the home dir collapsed to `~` for file-path tools.

- [ ] **Step 1: Write the failing test**

Add to the `TestBuildToolPreview` class (or end of file as a new test) in `tests/agent/test_display.py`:

```python
    def test_read_file_preview_collapses_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        result = build_tool_preview(
            "read_file", {"path": "/home/testuser/.hermes/config.yaml"}
        )
        assert result == "~/.hermes/config.yaml"

    def test_terminal_preview_collapses_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        result = build_tool_preview(
            "terminal", {"command": "cat /home/testuser/.hermes/.env"}
        )
        assert "/home/testuser" not in result
        assert "~/.hermes/.env" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/agent/test_display.py -k collapses_home -v`
Expected: FAIL — result still contains `/home/testuser`.

- [ ] **Step 3: Add the import**

At the top of `agent/display.py`, add alongside the existing imports:

```python
from agent.redact import collapse_home_path
```

- [ ] **Step 4: Apply the collapse in the general path**

In `build_tool_preview`, the general path currently reads:

```python
    preview = _oneline(str(value))
    if not preview:
        return None
    if max_len > 0 and len(preview) > max_len:
        preview = preview[:max_len - 3] + "..."
    return preview
```

Change to (collapse before truncation so the shortened form is what gets measured/truncated):

```python
    preview = _oneline(str(value))
    if not preview:
        return None
    preview = collapse_home_path(preview)
    if max_len > 0 and len(preview) > max_len:
        preview = preview[:max_len - 3] + "..."
    return preview
```

- [ ] **Step 5: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/agent/test_display.py -k collapses_home -v`
Expected: PASS.

- [ ] **Step 6: Run the full display test file for regressions**

Run: `scripts/run_tests.sh tests/agent/test_display.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent/display.py tests/agent/test_display.py
git commit -m "feat(display): collapse OS home to ~ in tool-call previews"
```

---

### Task 3: Collapse at the three gateway boundaries (`gateway/run.py`)

**Files:**
- Modify: `gateway/run.py` — add top-level import after line 53; apply collapse in `_sanitize_gateway_final_response` (line 288), `_prepare_gateway_status_message` (line 306), and `_progress_text` (~line 17899)
- Test: `tests/gateway/test_home_path_collapse.py` (new)

**Interfaces:**
- Consumes: `collapse_home_path` from `agent.redact` (Task 1); existing `_gateway_platform_value`, `_redact_gateway_user_facing_secrets`, `_looks_like_gateway_provider_error`, `_gateway_provider_error_reply`, `_TELEGRAM_NOISY_STATUS_RE`.
- Produces: final replies, status messages, and the tool-progress bubble collapse the OS home to `~` on **all** platforms. Telegram-specific provider-error/noise behavior is preserved.

- [ ] **Step 1: Write the failing tests**

Create `tests/gateway/test_home_path_collapse.py`:

```python
"""Home-dir collapse (OS home -> ~) at the gateway display boundaries."""

from gateway.config import Platform
from gateway.run import (
    _prepare_gateway_status_message,
    _sanitize_gateway_final_response,
)


def test_final_response_collapses_home_for_slack(monkeypatch):
    monkeypatch.setenv("HOME", "/home/testuser")
    out = _sanitize_gateway_final_response(
        Platform.SLACK, "I saved it to /home/testuser/.hermes/skills/foo"
    )
    assert out == "I saved it to ~/.hermes/skills/foo"


def test_status_message_collapses_home_for_slack(monkeypatch):
    monkeypatch.setenv("HOME", "/home/testuser")
    out = _prepare_gateway_status_message(
        Platform.SLACK, "lifecycle", "reading /home/testuser/.hermes/state.db"
    )
    assert out == "reading ~/.hermes/state.db"


def test_final_response_keeps_normal_answer(monkeypatch):
    monkeypatch.setenv("HOME", "/home/testuser")
    answer = "Here is the clean summary you asked for."
    assert _sanitize_gateway_final_response(Platform.SLACK, answer) == answer
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/gateway/test_home_path_collapse.py -v`
Expected: FAIL — the two collapse tests still contain `/home/testuser`.

- [ ] **Step 3: Add the top-level import**

In `gateway/run.py`, immediately after line 53 (`from agent.account_usage import fetch_account_usage, render_account_usage_lines`), add:

```python
from agent.redact import collapse_home_path
```

- [ ] **Step 4: Apply collapse in `_sanitize_gateway_final_response`**

Current (lines 288-303):

```python
def _sanitize_gateway_final_response(platform: Any, text: str) -> str:
    """..."""
    if not text:
        return text
    if _gateway_platform_value(platform) != "telegram":
        return text

    redacted = _redact_gateway_user_facing_secrets(str(text))
    if _looks_like_gateway_provider_error(redacted):
        return _gateway_provider_error_reply(redacted)
    return redacted
```

Change the body so the home-collapse runs for every platform, keeping the Telegram-only provider-error mapping:

```python
def _sanitize_gateway_final_response(platform: Any, text: str) -> str:
    """..."""
    if not text:
        return text
    text = collapse_home_path(str(text))
    if _gateway_platform_value(platform) != "telegram":
        return text

    redacted = _redact_gateway_user_facing_secrets(text)
    if _looks_like_gateway_provider_error(redacted):
        return _gateway_provider_error_reply(redacted)
    return redacted
```

(Leave the docstring unchanged.)

- [ ] **Step 5: Apply collapse in `_prepare_gateway_status_message`**

Current (lines 306-319):

```python
def _prepare_gateway_status_message(platform: Any, event_type: str, message: str) -> Optional[str]:
    """Filter/sanitize agent status callbacks before platform delivery."""
    text = str(message or "").strip()
    if not text:
        return None
    if _gateway_platform_value(platform) != "telegram":
        return text

    text = _redact_gateway_user_facing_secrets(text)
    if _TELEGRAM_NOISY_STATUS_RE.search(text):
        return None
    if _looks_like_gateway_provider_error(text):
        return _gateway_provider_error_reply(text)
    return text
```

Change to collapse for every platform before the Telegram branch:

```python
def _prepare_gateway_status_message(platform: Any, event_type: str, message: str) -> Optional[str]:
    """Filter/sanitize agent status callbacks before platform delivery."""
    text = str(message or "").strip()
    if not text:
        return None
    text = collapse_home_path(text)
    if _gateway_platform_value(platform) != "telegram":
        return text

    text = _redact_gateway_user_facing_secrets(text)
    if _TELEGRAM_NOISY_STATUS_RE.search(text):
        return None
    if _looks_like_gateway_provider_error(text):
        return _gateway_provider_error_reply(text)
    return text
```

- [ ] **Step 6: Apply collapse in the tool-progress render `_progress_text`**

This covers the gateway tool-progress bubble, including **verbose mode**, which dumps raw `args` via `json.dumps` (~line 17772) and bypasses `build_tool_preview`. `_progress_text` (~line 17899) is the single render chokepoint every progress send/edit flows through.

Current:

```python
            def _progress_text(lines: list) -> str:
                return "\n".join(str(line) for line in lines)
```

Change to:

```python
            def _progress_text(lines: list) -> str:
                return collapse_home_path("\n".join(str(line) for line in lines))
```

- [ ] **Step 7: Run the new tests to verify they pass**

Run: `scripts/run_tests.sh tests/gateway/test_home_path_collapse.py -v`
Expected: PASS (3 tests).

- [ ] **Step 8: Run the Telegram noise-filter tests for regressions**

The existing suite asserts non-Telegram messages (no home path) pass through unchanged and Telegram provider-error mapping still fires — the collapse is a no-op on those inputs.

Run: `scripts/run_tests.sh tests/gateway/test_telegram_noise_filter.py -v`
Expected: PASS (all existing tests).

- [ ] **Step 9: Commit**

```bash
git add gateway/run.py tests/gateway/test_home_path_collapse.py
git commit -m "feat(gateway): collapse OS home to ~ in replies, status, and progress"
```

---

### Task 4: Full-suite regression check

**Files:** none (verification only).

- [ ] **Step 1: Run the affected suites together**

Run: `scripts/run_tests.sh tests/agent/test_redact.py tests/agent/test_display.py tests/gateway/test_home_path_collapse.py tests/gateway/test_telegram_noise_filter.py`
Expected: PASS.

- [ ] **Step 2: Lint the enforced rule**

Run: `ruff check agent/redact.py agent/display.py gateway/run.py`
Expected: no `PLW1514` violations introduced.

- [ ] **Step 3: Type-check the touched modules**

Run: `ty check agent/redact.py agent/display.py`
Expected: no new errors from the added code. (If `ty` reports pre-existing errors in `gateway/run.py` unrelated to this change, note them but do not fix here.)

---

## Self-Review

**1. Spec coverage:**
- Helper `collapse_home_path` in `agent/redact.py` → Task 1. ✓
- Boundary-safety (username-continuation guard), root/short-home guard, idempotence, memoization → Task 1 helper + tests. ✓
- Spot 1 `build_tool_preview` → Task 2. ✓
- Spot 2 `_progress_text` (incl. verbose-mode raw args) → Task 3 Step 6. ✓
- Spot 3 `_prepare_gateway_status_message` (all platforms) → Task 3 Step 5. ✓
- Spot 4 `_sanitize_gateway_final_response` (all platforms) → Task 3 Step 4. ✓
- Always-on / no config → no config task, nothing added. ✓
- Only OS home collapsed → helper uses `Path.home()` only; non-home path test asserts `/opt/...` untouched. ✓
- Display-only (not in `redact_sensitive_text`/logs/compressor) → helper is a separate function; no edits to those call sites. ✓
- Verified residuals (streaming prose, reasoning prepend) → documented in spec as out of scope; no task. ✓

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N". Every code step shows full code. ✓

**3. Type consistency:** `collapse_home_path(text: str) -> str` is defined in Task 1 and consumed with that exact name/signature in Tasks 2 and 3. `_home_collapse_pattern`/`_HOME_COLLAPSE_CACHE` are internal to Task 1. Gateway helpers reused by name match `gateway/run.py`. ✓
