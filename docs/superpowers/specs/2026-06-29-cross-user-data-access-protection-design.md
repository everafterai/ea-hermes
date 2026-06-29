# Cross-User Session/Memory Data Access Protection — Design

**Date:** 2026-06-29
**Status:** Approved design, pre-implementation
**Topic:** Close the file-tool read path to other users' session/memory data, and
audit all tool access to those data stores

## Problem

The fork added per-user **session isolation** (`hermes_state.build_visibility_where`,
`session_row_visible`) and **per-scope memory** so that `session_search` / `/resume`
and the holographic memory store scope to the requesting identity. But that isolation
is **purely application-layer SQL/Python scoping** — it never touches the bytes on
disk. The underlying storage has no per-user partitioning:

- **One shared `state.db`** (`hermes_state.py:34`, `DEFAULT_DB_PATH = get_hermes_home() / "state.db"`)
  holds every user's `sessions` + `messages` rows. `content`/`title` are plaintext
  TEXT columns.
- **Memory** lives in `${HERMES_HOME}/memory_store.db` (shared holographic store) or,
  under `scope_isolation=true`, one SQLite file per scope at
  `${HERMES_HOME}/memories/holographic/<scope>.db` (the scope id — a user_id/chat_id —
  is in the filename). `memories/MEMORY.md` and `USER.md` are intentionally *global/shared*.
- The gateway is **one asyncio process running as one OS uid** serving all platform
  users (`gateway/session_context.py`); identity is carried in task-local contextvars,
  not separate processes — so POSIX file permissions cannot separate users.

Any tool that can read files off the host therefore bypasses all app-layer scoping —
but the size of each tool's hole differs, and an earlier read of this codebase
over-stated the `file`-tool hole. The verified picture:

- **`terminal`** (`tools/terminal_tool.py`) defaults to the `local` backend — a real
  host shell with full filesystem access (`sqlite3 ~/.hermes/state.db`, `strings`,
  copy-then-read, …). Binary guards do not apply to a shell. Granted only to the
  **admin** role. **This is the wide-open, primary exposure**, and the only lever
  against it is detection (auditing), since blocking a shell is futile.
- **`file`** tool (`tools/file_tools.py`) — the `.db` stores are **already effectively
  closed** by pre-existing guards, *not* by the credential read-block:
  - `read_file` blocks any binary-extension file at `tools/file_tools.py:712`
    (`.db`/`.sqlite`/`.sqlite3` ∈ `tools/binary_extensions.py`), *before* the
    credential read-block at line 728. So `read_file ~/.hermes/state.db` already
    fails today — but with a misleading message that says *"Use … terminal to
    inspect binary files,"* i.e. it points at the bypass.
  - `search_files` runs `rg`/`grep` **without** `-a`/`--text`
    (`tools/file_operations.py:2072`), so they skip binary `.db` files by default —
    grep cannot dump rows either.
  - **The one genuine `file`-tool hole is plaintext session snapshots**, written under
    `~/.hermes/sessions/` as `session_<sid>.json`, `<sid>.jsonl`, and
    `request_dump_<sid>_*.json` (`agent/agent_init.py:1030`; opt-in
    `sessions.write_json_snapshots`, default off; request dumps appear during
    debugging). These are plaintext JSON — `read_file`/`search_files` read them
    fine, for **any** role holding the `file` toolset (operator included).

So the non-admin (`operator`) tier is, in practice, **already unable to exfiltrate the
session/memory DBs** — the earlier "operator can `read_file state.db`" claim was wrong
(it missed the binary guard). The residual exposures are: (1) the admin `terminal`
(detection only), and (2) plaintext session snapshots via the `file` tool when that
opt-in feature or request-dumping is active.

## Goals

- **Detect the admin path (primary):** create an auditable trail whenever any tool
  reads or references the protected data stores, turning silent cross-user access via
  `terminal`/`code_execution` into a logged event. This is the main value, because a
  shell can't be blocked.
- **Close the one real `file`-tool hole:** deny `read_file`/`search_files`/`patch` on
  the plaintext session snapshots under `~/.hermes/sessions/`
  (`session_*.json` / `*.jsonl` / `request_dump_*.json`). For roles without a shell
  (`operator`, and anything short of admin), this is a *real* boundary.
- **Fix the misleading `.db` message + harden:** run the protected-path check *before*
  the binary-extension guard so a `read_file` on `state.db`/memory DBs returns a
  "this is other users' data" message instead of "use terminal," and add an explicit
  policy deny (defense-in-depth, not reliant on the binary heuristic).
- **Role-independent:** the controls live in the tool execution path and apply whether
  or not RBAC is active (when `user_roles` is empty, everyone has every toolset — these
  controls still apply).
- **No functional regression:** the app's own access to these DBs (via direct `sqlite3`
  connections in `SessionDB` and the cross-profile sidebar) is untouched.

## Non-goals (YAGNI / explicit threat-model decisions)

- **Not** stopping a determined admin. `terminal`'s `local` backend is a real shell;
  command-string scanning is evadable by obfuscation (base64, indirect paths, a helper
  script). We are not sandboxing `terminal`, adding per-user OS accounts, or encrypting
  data at rest. This reflects the decision that admins are a trusted tier.
- **Not** tamper-proof auditing. The audit log is a **local append-only file** written
  by the same uid that runs the gateway, so a malicious admin could edit it. It is
  designed to catch *accidental* and *operator-tier* access, and to make casual admin
  access visible — not to survive an adversary who owns the box.
- **Not** blocking `terminal`/`code_execution` — those emit-on-match only, never deny.
- **Not** touching `MEMORY.md` / `USER.md` — those are intentionally shared global
  context; blocking them would break shared behavior.

## Approach

Two small, independent additions wired into the existing tool execution chokepoints:

1. A single **protected-path matcher** in `agent/file_safety.py` so "close" and "detect"
   agree on exactly which files are protected.
2. Extend the existing `file` read-block with that matcher (the "close"), and add a
   tiny **local append-only audit module** invoked from the `file`, `terminal`, and
   `code_execution` tools (the "detect").

Chosen over the heavier alternatives (terminal sandboxing, per-user OS separation,
encryption-at-rest) because the user accepted the determined-admin bypass and wants a
proportionate change. The read-block reuses the proven `agent/file_safety.py`
mechanism rather than inventing a new one.

## Design

### 1. Shared protected-path matcher — `agent/file_safety.py`

New helper, pure and unit-testable:

```python
def is_protected_data_path(path: str | Path) -> str | None:
    """Return a human-readable reason if `path` is a cross-user data store
    (session or per-scope/shared memory), else None."""
```

Matches the **resolved** path (so `..`/symlink/relative forms normalize) against
patterns, covering cross-profile locations. Two tiers:

**Plaintext session snapshots — the real `file`-tool hole:**
- `**/sessions/session_*.json`     — per-session JSON snapshot
- `**/sessions/*.jsonl`            — per-session JSONL transcript
- `**/sessions/request_dump_*.json` — gateway request dumps

**Data DBs — already binary-blocked; matcher gives a correct message + defense-in-depth:**
- `**/state.db`              — session DB, incl. `profiles/*/state.db`
- `**/memory_store.db`       — shared holographic store
- `**/memories/holographic/*.db` — per-scope memory DBs

Explicitly **excludes** `memories/MEMORY.md` and `memories/USER.md` (shared global
memory — must stay readable). Matching is on filename + parent-dir shape, not anchored
to the current `HERMES_HOME`, so other profiles' files are also recognized.

### 2. Close — wire the matcher into the `file` tool

Wire `is_protected_data_path` into `get_read_block_error` (applied at
`tools/file_tools.py:728`) **and call it before the binary-extension guard** at
`tools/file_tools.py:712`, so a protected `.db` returns the protected-data message
rather than the misleading "use terminal" binary message. Then:

- `read_file` on a protected path returns the protected-data read-block error (reusing
  the honest "defense-in-depth — the terminal tool can still bypass" framing).
- `search_files` skips/denies protected files (defense-in-depth beyond ripgrep's
  binary heuristic, and the actual close for the plaintext snapshots, which `rg` would
  otherwise happily grep).
- `patch` refuses protected targets (never legitimately edited via the file tool).

The plaintext-snapshot patterns are where this is a *new* boundary; the `.db` patterns
are message-correction + an explicit policy block that no longer relies on the binary
heuristic.

**No functional breakage:** `SessionDB.__init__` and the read-only cross-profile
aggregation open the DBs through direct `sqlite3` connections, not the file tool;
snapshot writing uses its own writer — so blocking the *file tool* does not affect
session search, `/resume`, the sidebar, memory, or snapshot generation.

### 3. Detect — local append-only audit log

New module **`agent/data_access_audit.py`**:

```python
def record_access(*, tool: str, action: str, target: str) -> None:
    """Append one JSONL audit event for sensitive-data access. Best-effort,
    never raises into the tool path."""
```

- **Sink:** JSONL, one object per line, opened in append mode (`'a'`) at
  `${HERMES_HOME}/audit/data-access.log` (configurable; directory created on first
  write). Best-effort: any I/O error is swallowed/logged, never propagated into tool
  execution.
- **Event fields:** UTC ISO timestamp, `platform`, `user_id`, `chat_id`, `role` (if
  resolvable), `tool`, `action` (`read` | `blocked-read` | `exec`), and `target`
  (the matched path, or a truncated command/script snippet). Identity is read from the
  session contextvars already used by the RBAC backstop
  (`HERMES_SESSION_USER_ID` / `_CHAT_ID` / `_PLATFORM`, via `gateway/session_context.py`
  helpers).
- **Emit points:**
  - **`file` tool** — on every protected-path hit: `blocked-read` for the denied
    `read_file`/`search_files`/`patch`, so the close-path is itself audited.
  - **`terminal`** (`tools/terminal_tool.py`) and **`code_execution`**
    (`tools/code_execution_tool.py`) — before execution, scan the command string /
    script source for references to the protected filenames/paths
    (`state.db`, `memory_store.db`, `memories/holographic`, …). On match,
    `record_access(action="exec", ...)`. **Log-only; never blocks.**

### 4. Configuration

Default-on, with a couple of knobs in core config:

```yaml
data_access_audit:
  enabled: true                                   # default true
  path: "${HERMES_HOME}/audit/data-access.log"    # default
```

When `enabled: false`, `record_access` is a no-op; the read-block (the "close" half)
still applies regardless, since it is a safety control, not telemetry.

## Testing (via `scripts/run_tests.sh`)

- **Matcher** (`is_protected_data_path`): positives — `state.db`,
  `profiles/p1/state.db`, `memory_store.db`, `memories/holographic/U123.db`,
  `sessions/session_abc.json`, `sessions/abc.jsonl`, `sessions/request_dump_abc_1.json`;
  negatives — `memories/MEMORY.md`, `memories/USER.md`, an arbitrary `notes.db` outside
  the memory dir, a source file, a `sessions/` markdown file.
- **Close:** `read_file` / `search_files` / `patch` denied on each protected path; the
  **plaintext snapshot** path returns the protected-data message (the key new boundary,
  since it is not binary-blocked); a protected `.db` returns the protected-data message
  rather than the "use terminal" binary message (ordering before the binary guard);
  `MEMORY.md` / `USER.md` still readable; cross-profile `profiles/*/state.db` denied.
- **Detect:** a file-tool block and a terminal/code-exec command-scan hit each write one
  well-formed JSONL line carrying the identity fields; `enabled: false` writes nothing;
  audit failures never raise into the tool path.
- Honors the autouse `HERMES_HOME` redirect — **no writes to the real `~/.hermes`**;
  no change-detector tests.

## Files touched

- `agent/file_safety.py` — add `is_protected_data_path`; wire into `get_read_block_error`.
- `agent/data_access_audit.py` — **new**, the audit module.
- `tools/file_tools.py` — call the matcher **before** the binary-extension guard
  (line 712) in `read_file_tool`; apply it across `search_files` / `patch`; emit
  `blocked-read` audit events.
- `tools/terminal_tool.py`, `tools/code_execution_tool.py` — pre-exec command/script
  scan + `exec` audit events.
- config loader — surface the `data_access_audit` block.
- `tests/` — matcher, close, and detect tests per above.
