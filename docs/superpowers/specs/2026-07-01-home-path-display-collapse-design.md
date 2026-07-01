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

Traced through the code, user-facing text reaches a platform via five distinct
boundaries. The helper is applied at each:

| # | Spot | File | Covers |
|---|------|------|--------|
| 1 | `build_tool_preview` | [agent/display.py](../../../agent/display.py) | The tool-call preview line (e.g. `read_file <path>`) — CLI/TUI display and the source for gateway progress lines |
| 2 | tool-progress **enqueue source** + `_progress_text` | [gateway/run.py](../../../gateway/run.py) | The Slack/etc. tool-progress bubble. The message is collapsed where it is put on the queue (verbose path + normal path before dedup), so **every** downstream render/send (the direct `"\n".join(progress_lines)` sends, the `content=msg` flood fallback, the dedup counter, and `_progress_text`) ships `~`. This closes **verbose mode**, whose raw `json.dumps(args)` is not pre-collapsed by spot 1. `_progress_text` keeps a collapse too as an idempotent backstop. |
| 3 | `_prepare_gateway_status_message` | [gateway/run.py](../../../gateway/run.py) | Status / context-pressure messages — generalized from Telegram-only to collapse for **all** platforms |
| 4 | `_sanitize_gateway_final_response` | [gateway/run.py](../../../gateway/run.py) | Final replies — generalized from Telegram-only to collapse for **all** platforms |
| 5 | `_approval_notify_sync` (dangerous-command approval prompt) | [gateway/run.py](../../../gateway/run.py) | The approval prompt shown before a dangerous `terminal` command runs — the **display copy** of the command (both the interactive `send_exec_approval` button prompt and the plain-text `/approve` fallback). Collapsed at the single caller so it covers every adapter. The **real** command used for execution and `/approve session` pattern matching is untouched — matching is keyed by `session_key` + a server-side `pattern_key`, never the displayed string. |

Spots 3 and 4 currently no-op for non-Telegram platforms (they return the text
unchanged). We keep their Telegram-specific provider-error mapping exactly as-is
and only add the universal home-collapse so it runs for every platform.

Spots 1 and 2 overlap on previews; because the helper is idempotent, the overlap
is harmless. Each spot covers a surface the others do not (local CLI/TUI vs. the
gateway progress bubble vs. status vs. final reply vs. the approval prompt).

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

## Verified residuals / known limitations

Tracing the delivery paths confirmed the four spots above fully cover **Slack**
(the target platform) and cover **tool-call previews on every platform** (the
stated concern). Two narrow gaps remain, deliberately out of scope for this
change:

- **Streamed prose on streaming platforms (e.g. Telegram).** When per-platform
  streaming is enabled, `GatewayStreamConsumer`
  ([gateway/stream_consumer.py](../../../gateway/stream_consumer.py)) delivers
  the model's prose incrementally *before* `_sanitize_gateway_final_response`
  (line ~9896) runs, through ~6 buffered `adapter.send` sites with no single
  chokepoint. So a **path mentioned in the model's prose** could leak there.
  Slack does not stream (Bolt posts can't be edited like the CLI), so this does
  not affect the target platform, and tool-call previews are unaffected (the
  progress bubble is separate from streamed content). Covering it would mean
  routing every consumer send through a shared text hook — a larger, riskier
  change left as a follow-up.
- **Reasoning prepend.** When `show_reasoning` is enabled (off by default), the
  reasoning block is prepended to the reply *after* line 9896, so a path inside
  reasoning text would bypass the collapse. Edge case; left as a follow-up.
- **Final-reply fallback transcript persist.** `_sanitize_gateway_final_response`
  reassigns `response` in place, and one rare fallback path (taken only when a
  turn produced no distinguishable new messages) writes that collapsed
  `response` to the JSONL transcript, which can re-enter model context on
  JSONL-based resume. This is pre-existing behavior (Telegram's provider-error
  remapping already flowed through the same fallback), doubly narrow, and benign
  (`~` is model-interpretable; the simple fallback carries no tool-call args to
  corrupt). Noted for honesty, not fixed.
- **`.bak`-sibling over-collapse.** Because the right boundary treats `.` as a
  non-username char, a sibling dir like `/home/<user>.bak/x` collapses to
  `~.bak/x`. Display-only, extremely rare, and `[A-Za-z0-9_-]` is a defensible
  username charset. Noted only.

Neither gap affects the tool-call display the user reported, nor Slack.
