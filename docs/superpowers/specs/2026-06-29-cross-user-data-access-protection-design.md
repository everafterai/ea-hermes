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

Any tool that can read files off the host therefore bypasses all app-layer scoping:

- **`terminal`** (`tools/terminal_tool.py`) defaults to the `local` backend — a real
  host shell with full filesystem access (`sqlite3 ~/.hermes/state.db`, `strings`, …).
  Granted only to the **admin** role.
- **`file`** / `read_file` (`tools/file_tools.py`) accepts absolute paths and is **not**
  blocked from reading `state.db` or the memory DBs. The existing read-block
  (`agent/file_safety.py`) covers credentials (`.env`, `auth.json`, …) but **not** the
  data stores. The `file` toolset is granted to **operator** *and* admin — so an
  **operator (a non-shell role) can already read other users' messages** via
  `read_file ~/.hermes/state.db`.

The user framed this as "mainly an admin capability," but the operator/`file` path is a
genuine non-admin leak.

## Goals

- **Close the non-admin path:** make the `file` tool deny reads of the session DB and
  the per-scope/shared memory DBs. For roles without a shell (`operator`, and anything
  short of admin), this is a *real* boundary.
- **Detect the admin path:** create an auditable trail whenever any tool reads or
  references those protected data stores, turning silent cross-user access into a
  logged event.
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
patterns, covering cross-profile locations:

- `**/state.db`              — session DB, incl. `profiles/*/state.db`
- `**/memory_store.db`       — shared holographic store
- `**/memories/holographic/*.db` — per-scope memory DBs

Explicitly **excludes** `memories/MEMORY.md` and `memories/USER.md` (shared global
memory — must stay readable). Matching is on filename + parent-dir shape, not anchored
to the current `HERMES_HOME`, so other profiles' DBs are also recognized.

### 2. Close — extend the `file` read-block

Wire `is_protected_data_path` into the existing read-block path
(`get_read_block_error`, applied at `tools/file_tools.py:728`) so:

- `read_file` on a protected path returns a read-block error (reusing the existing
  honest "defense-in-depth — the terminal tool can still bypass" framing).
- `search_files` skips/denies protected files so grep can't dump rows out of the
  binary DBs.
- `patch` refuses protected targets (these files are never legitimately edited via the
  file tool).

**No functional breakage:** `SessionDB.__init__` and the read-only cross-profile
aggregation open these DBs through direct `sqlite3` connections, not the file tool, so
blocking the *file tool* does not affect session search, `/resume`, the sidebar, or
memory.

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
  `profiles/p1/state.db`, `memory_store.db`, `memories/holographic/U123.db`; negatives —
  `memories/MEMORY.md`, `memories/USER.md`, an arbitrary `notes.db` outside the memory
  dir, a source file.
- **Close:** `read_file` / `search_files` / `patch` denied on each protected path;
  `MEMORY.md` / `USER.md` still readable; cross-profile `profiles/*/state.db` denied.
- **Detect:** a file-tool block and a terminal/code-exec command-scan hit each write one
  well-formed JSONL line carrying the identity fields; `enabled: false` writes nothing;
  audit failures never raise into the tool path.
- Honors the autouse `HERMES_HOME` redirect — **no writes to the real `~/.hermes`**;
  no change-detector tests.

## Files touched

- `agent/file_safety.py` — add `is_protected_data_path`; wire into the read-block.
- `agent/data_access_audit.py` — **new**, the audit module.
- `tools/file_tools.py` — apply the matcher across `read_file` / `search_files` /
  `patch`; emit `blocked-read` audit events.
- `tools/terminal_tool.py`, `tools/code_execution_tool.py` — pre-exec command/script
  scan + `exec` audit events.
- config loader — surface the `data_access_audit` block.
- `tests/` — matcher, close, and detect tests per above.
