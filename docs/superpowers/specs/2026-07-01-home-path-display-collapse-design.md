# Collapse OS home directory to `~` in user-facing display

**Date:** 2026-07-01
**Status:** Approved (design)

## Problem

The gateway serves many messaging-platform users (primarily Slack) from a
single Google Cloud VM running as one OS user (`shaidiamant`). Every file-system
tool call the agent makes surfaces an absolute path — e.g.

```
read_file  /home/shaidiamant/.hermes/config.yaml
```

That path leaks the OS username to everyone in the channel. We want users to see
`~/.hermes/config.yaml` instead, so the home directory (and the username inside
it) is never exposed in chat.

## Goal

Hide the OS home-directory prefix from **all user-facing text** by collapsing it
to `~`. This is display-only privacy: it must not change what tools receive,
what is written to logs, or what is fed back into model context.

- `/home/shaidiamant/.hermes/x` → `~/.hermes/x`
- `/home/shaidiamant/repos/y` → `~/repos/y`

## Non-goals

- Not a security boundary. RBAC ([gateway/tool_access.py](../../../gateway/tool_access.py))
  and the secret redactor remain the real controls. A determined user with a
  shell can still learn the home path; this closes casual/accidental exposure in
  the chat UI.
- Not collapsing a custom `HERMES_HOME` that lives **outside** the OS home
  (e.g. `/opt/hermes`) — such a path carries no username, and rewriting it to
  `~` would be misleading. The default `~/.hermes` and profile dirs live under
  the OS home and are collapsed transitively.
- Not touching logs, the model's own context, or paths passed to tools.

## The helper

A single display-only function in [agent/redact.py](../../../agent/redact.py)
(the existing "scrub before it reaches a user" module):

```python
def collapse_home_path(text: str) -> str:
    """Replace the OS home-dir prefix with ~ in user-facing display text.

    Display-only: hides the OS username (e.g. /home/shaidiamant/.hermes ->
    ~/.hermes) from people the gateway serves. Never mutates paths passed to
    tools, written to logs, or fed into model context.
    """
```

Behavior:

- Resolves `str(Path.home())` **at call time** (so tests can monkeypatch `HOME`;
  the gateway process's home is the OS user's home = exactly what we hide).
- Replaces the home prefix with `~` **only when the following character is not a
  username-continuation char** (`[A-Za-z0-9_-]`). So:
  - `/home/shaidiamant/x`, `/home/shaidiamant"`, and a bare `/home/shaidiamant`
    at a token boundary collapse.
  - `/home/shaidiamant2/x` and `/home/shaidiamantfoo` (a *different* user whose
    name shares the prefix) are left untouched.
- **Guards:** returns the input unchanged when it is empty/None, or when
  `Path.home()` resolves to `/` or a too-short prefix (never collapse
  everything).
- **Idempotent** — collapsing already-collapsed text is a no-op.
- Compiled regex **memoized per home string** (cheap on the hot progress path).

## Application points

Traced through the code, user-facing text reaches a platform via four distinct
boundaries. The helper is applied at each:

| # | Spot | File | Covers |
|---|------|------|--------|
| 1 | `build_tool_preview` | [agent/display.py](../../../agent/display.py) | The tool-call preview line (e.g. `read_file <path>`) — CLI/TUI display and the source for gateway progress lines |
| 2 | `_progress_text` | [gateway/run.py](../../../gateway/run.py) (~17899) | The Slack/etc. tool-progress bubble (previews **and** any inlined tool output), sent raw via `_send_progress_text` / `edit_message` — this path bypasses the sanitizers below, so it needs its own hook |
| 3 | `_prepare_gateway_status_message` | [gateway/run.py](../../../gateway/run.py) (line 306) | Status / context-pressure messages — generalized from Telegram-only to collapse for **all** platforms |
| 4 | `_sanitize_gateway_final_response` | [gateway/run.py](../../../gateway/run.py) (line 288, called at ~9896) | Final replies — generalized from Telegram-only to collapse for **all** platforms |

Spots 3 and 4 currently no-op for non-Telegram platforms (they return the text
unchanged). We keep their Telegram-specific provider-error mapping exactly as-is
and only add the universal home-collapse so it runs for every platform.

Spots 1 and 2 overlap on previews; because the helper is idempotent, the overlap
is harmless. Each spot covers a surface the others do not (local CLI/TUI vs. the
gateway progress bubble vs. status vs. final reply).

## Decisions

- **Always-on, no new config block.** Purely display-side and privacy-positive;
  matches the default-on secret-redaction philosophy. A Slack user who is not the
  OS user cannot `cd` into the home dir anyway, so `~` is the correct
  abstraction, not a loss of information.
- **Only the OS home is collapsed** (`Path.home()`), for the reasons in
  Non-goals.

## Testing

Via `scripts/run_tests.sh`, monkeypatching `HOME` for a deterministic home:

- Unit tests for `collapse_home_path`:
  - `/home/testuser/.hermes/x` → `~/.hermes/x`
  - bare `/home/testuser` at a boundary → `~`
  - `/home/testuser2/x` and `/home/testuserfoo` → unchanged (different user)
  - a non-home absolute path (e.g. `/opt/foo`) → unchanged
  - path embedded mid-sentence / in quotes → collapsed
  - `Path.home() == "/"` guard → input returned unchanged
  - empty / `None` → safe
  - idempotence
- `build_tool_preview` collapses a `read_file` path argument.
- `_prepare_gateway_status_message` collapses for a non-Telegram platform (slack)
  while preserving Telegram provider-error behavior.
- `_sanitize_gateway_final_response` collapses for a non-Telegram platform (slack).

## Residual risk to verify during implementation

Confirm that `_sanitize_gateway_final_response` (line ~9896) is the **sole**
final-reply send path for Slack — i.e. no streaming or split-message path
delivers final text while bypassing it. If another final-send path exists, hook
the helper there too.
