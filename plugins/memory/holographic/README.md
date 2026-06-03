# Holographic Memory Provider

Local SQLite fact store with FTS5 search, trust scoring, entity resolution, and HRR-based compositional retrieval.

## Requirements

None — uses SQLite (always available). NumPy optional for HRR algebra.

## Setup

```bash
hermes memory setup    # select "holographic"
```

Or manually:
```bash
hermes config set memory.provider holographic
```

## Config

Config in `config.yaml` under `plugins.hermes-memory-store`:

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite database path |
| `auto_extract` | `false` | Auto-extract facts at session end |
| `default_trust` | `0.5` | Default trust score for new facts |
| `hrr_dim` | `1024` | HRR vector dimensions |

## Per-scope isolation (multi-user gateway)

Set `scope_isolation: true` under `plugins.hermes-memory-store` to give each
DM user and each channel its own fact store:

- DM  -> `db_dir/user_<user_id>.db`  (per-user silo)
- channel/group/thread -> `db_dir/chat_<chat_id>.db`  (shared by participants)
- CLI / cron -> `db_dir/default_default.db`

Default `db_dir` is `$HERMES_HOME/memories/holographic`. When
`scope_isolation` is false (default) the plugin uses the single shared
`db_path`, exactly as before. Scope is resolved per operation from session
contextvars, so a single process safely serves many concurrent users.

## Tools

| Tool | Description |
|------|-------------|
| `fact_store` | 9 actions: add, search, probe, related, reason, contradict, update, remove, list |
| `fact_feedback` | Rate facts as helpful/unhelpful (trains trust scores) |
