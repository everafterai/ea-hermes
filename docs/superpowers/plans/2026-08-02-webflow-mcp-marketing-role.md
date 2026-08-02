# Webflow MCP + RBAC Role Inheritance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give exactly one Slack user (`U02S08M50S3`, Adi Gorelik) draft-first
Webflow access through Webflow's MCP server, with publishing behind a human
approval gate — and add role inheritance to RBAC so the new role composes
instead of duplicating.

**Architecture:** Two deliverables. First, an `extends` key in the RBAC roles
config (`gateway/tool_access.py`) that lets a role inherit another's toolsets —
a pure function change to `_coerce_roles`, fully unit-testable, no I/O. Second,
config in the VM's `~/.hermes/config.yaml` wiring Webflow's local stdio MCP
server, composing a `marketing_publisher` role from `marketing` +
`webflow_publisher`, and gating publish/delete tools behind
`approvals.require_for_tools`.

**Tech Stack:** Python 3.11, pytest (via `scripts/run_tests.sh`), YAML config,
MCP over stdio (`npx -y webflow-mcp-server@1.0.0`, Node v22.22.3 on the VM).

**Spec:** [docs/superpowers/specs/2026-08-02-webflow-mcp-marketing-role-design.md](../specs/2026-08-02-webflow-mcp-marketing-role-design.md)

## Global Constraints

- **Never run bare `pytest`.** Always `scripts/run_tests.sh` — it unsets creds,
  forces `TZ=UTC` and `C.UTF-8`, and isolates tests in subprocesses.
- **RBAC fail-closed invariants must survive:** activation stays keyed to
  `user_roles` presence (empty `user_roles` = upstream behavior exactly), and
  deny-until-assigned holds for roleless/undefined-role users.
- **Tests must not write to `~/.hermes/`.** An autouse fixture redirects
  `HERMES_HOME`; do not defeat it. Do not write change-detector tests.
- **Both toolset names always travel together:** `webflow` (alias, seen by the
  `enabled_toolsets` filter) and `mcp-webflow` (canonical, seen by the execution
  backstop). Granting one is a bug.
- **MCP tool names dispatch prefixed** as `mcp_webflow_<tool>`. Any
  `approvals.require_for_tools` entry missing the prefix is silently inert.
- **Pin the MCP package** to `webflow-mcp-server@1.0.0`. Never `@latest`.
- **Config edits go in the repo-root `config.yaml`** (the working copy). Shai
  pushes it to the VM; do not attempt to ssh or deploy.
- `ruff check .` is near-disabled (only PLW1514 enforced); `ty check` is the
  type checker.

---

### Task 1: Role inheritance — `extends` resolution

Adds an optional `extends` key to a role definition. A role's effective toolsets
become its own plus the transitive union of every role it extends. Two-pass, so
a role may extend one defined later in the YAML.

**Files:**
- Modify: `gateway/tool_access.py:122-151` (`_coerce_roles`)
- Test: `tests/gateway/test_tool_access.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_coerce_roles(raw: Any) -> Dict[str, FrozenSet[str]]` — unchanged
  signature and return type, now honoring `extends`. A new module-level helper
  `_coerce_extends(raw: Any) -> List[str]` returning lowercased parent role
  names. Task 3's config relies on `extends` accepting a **list**.

- [ ] **Step 1: Write the failing tests**

Append to `tests/gateway/test_tool_access.py`:

```python
class TestRoleInheritance:
    def test_extends_single_parent_unions_toolsets(self):
        p = policy_from_extra({
            "user_roles": {"U_pub": "publisher"},
            "roles": {
                "base": {"toolsets": ["web", "notion"]},
                "publisher": {"extends": "base", "toolsets": ["webflow"]},
            },
        })
        assert p.grant_for("U_pub") == frozenset({"web", "notion", "webflow"})

    def test_extends_accepts_a_list_of_parents(self):
        p = policy_from_extra({
            "user_roles": {"U_pub": "publisher"},
            "roles": {
                "base": {"toolsets": ["web"]},
                "extra": {"toolsets": ["webflow", "mcp-webflow"]},
                "publisher": {"extends": ["base", "extra"]},
            },
        })
        assert p.grant_for("U_pub") == frozenset({"web", "webflow", "mcp-webflow"})

    def test_extends_is_transitive(self):
        p = policy_from_extra({
            "user_roles": {"U_c": "c"},
            "roles": {
                "a": {"toolsets": ["web"]},
                "b": {"extends": "a", "toolsets": ["notion"]},
                "c": {"extends": "b", "toolsets": ["webflow"]},
            },
        })
        assert p.grant_for("U_c") == frozenset({"web", "notion", "webflow"})

    def test_extends_resolves_forward_references(self):
        # 'publisher' is defined BEFORE the role it extends. Dict order in YAML
        # must not affect resolution.
        p = policy_from_extra({
            "user_roles": {"U_pub": "publisher"},
            "roles": {
                "publisher": {"extends": "base", "toolsets": ["webflow"]},
                "base": {"toolsets": ["web"]},
            },
        })
        assert p.grant_for("U_pub") == frozenset({"web", "webflow"})

    def test_extends_with_no_own_toolsets_is_pure_composition(self):
        p = policy_from_extra({
            "user_roles": {"U_pub": "publisher"},
            "roles": {
                "base": {"toolsets": ["web"]},
                "extra": {"toolsets": ["webflow"]},
                "publisher": {"extends": ["base", "extra"]},
            },
        })
        assert p.grant_for("U_pub") == frozenset({"web", "webflow"})

    def test_role_without_extends_is_unchanged(self):
        # Regression guard: the existing flat-list form keeps working.
        p = policy_from_extra({
            "user_roles": {"U_a": "plain"},
            "roles": {"plain": {"toolsets": ["web", "notion"]}},
        })
        assert p.grant_for("U_a") == frozenset({"web", "notion"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `scripts/run_tests.sh tests/gateway/test_tool_access.py::TestRoleInheritance -v`

Expected: FAIL. `test_extends_single_parent_unions_toolsets` fails with
`frozenset({'webflow'}) != frozenset({'web', 'notion', 'webflow'})` — `extends`
is currently ignored, so only the role's own `toolsets` survive.
`test_extends_with_no_own_toolsets_is_pure_composition` fails with `frozenset()`.

- [ ] **Step 3: Add the `_coerce_extends` helper**

Insert immediately above `_coerce_roles` in `gateway/tool_access.py`:

```python
def _coerce_extends(raw: Any) -> List[str]:
    """Normalize a role's ``extends`` value to a list of parent role names.

    Accepts a single name, a comma-separated string, or a sequence. Names are
    lowercased to match the ``roles`` table. Anything else logs and yields no
    parents — an unusable ``extends`` must never silently widen a grant.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [s for s in (p.strip().lower() for p in raw.split(",")) if s]
    if isinstance(raw, (list, tuple, set, frozenset)):
        return [n for n in (_coerce_str(p).lower() for p in raw) if n]
    logger.warning(
        "tool_access: unexpected extends type %s — ignoring",
        type(raw).__name__,
    )
    return []
```

**`List` is not currently imported.** Line 36 of `gateway/tool_access.py` reads:

```python
from typing import Any, Dict, FrozenSet, Mapping, Optional
```

Change it to:

```python
from typing import Any, Dict, FrozenSet, List, Mapping, Optional
```

- [ ] **Step 4: Rewrite `_coerce_roles` as two passes**

Replace the body of `_coerce_roles` (`gateway/tool_access.py:122-151`) with:

```python
def _coerce_roles(raw: Any) -> Dict[str, FrozenSet[str]]:
    """Normalize a ``roles`` block, merged over the built-in defaults.

    Two passes: collect each role's OWN toolsets, then resolve ``extends``
    against the fully-populated map so a role may extend one defined later in
    the YAML.
    """
    own: Dict[str, FrozenSet[str]] = dict(BUILTIN_ROLES)
    parents: Dict[str, List[str]] = {}
    if not isinstance(raw, dict):
        return own

    for name, body in raw.items():
        role_name = _coerce_str(name).lower()
        if not role_name:
            continue
        toolsets: Any = None
        if isinstance(body, dict):
            toolsets = body.get("toolsets")
            parents[role_name] = _coerce_extends(body.get("extends"))
        elif isinstance(body, (list, tuple)):
            toolsets = body
        if isinstance(toolsets, str):
            items = [s for s in (s.strip() for s in toolsets.split(",")) if s]
        elif isinstance(toolsets, (list, tuple, set, frozenset)):
            items = toolsets
        elif toolsets is not None:
            logger.warning(
                "tool_access: unexpected toolsets type %s for role '%s' — ignoring",
                type(toolsets).__name__, role_name,
            )
            items = []
        else:
            items = []
        own[role_name] = frozenset(
            _coerce_str(t).lower() for t in items if _coerce_str(t)
        )

    if not any(parents.values()):
        return own

    return {
        role: _resolve_role_extends(role, own, parents, set())
        for role in own
    }
```

- [ ] **Step 5: Add the resolver**

Insert immediately below `_coerce_extends`:

```python
def _resolve_role_extends(
    role: str,
    own: Dict[str, FrozenSet[str]],
    parents: Dict[str, List[str]],
    resolving: set,
) -> FrozenSet[str]:
    """Union of *role*'s own toolsets and everything it transitively extends.

    ``resolving`` carries the current DFS path so a cycle breaks instead of
    recursing forever. Deliberately unmemoized: role graphs are a handful of
    entries, and a memo populated mid-cycle would depend on which role the
    caller happened to start from.
    """
    if role in resolving:
        logger.warning(
            "tool_access: role inheritance cycle at '%s' — ignoring this edge",
            role,
        )
        return frozenset()
    resolving.add(role)
    try:
        acc = set(own.get(role, frozenset()))
        for parent in parents.get(role, []):
            if parent not in own:
                logger.warning(
                    "tool_access: role '%s' extends undefined role '%s' — ignoring",
                    role, parent,
                )
                continue
            acc |= _resolve_role_extends(parent, own, parents, resolving)
        return frozenset(acc)
    finally:
        resolving.discard(role)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `scripts/run_tests.sh tests/gateway/test_tool_access.py::TestRoleInheritance -v`

Expected: PASS, all 6.

- [ ] **Step 7: Run the full RBAC suite for regressions**

Run: `scripts/run_tests.sh tests/gateway/test_tool_access.py`

Expected: PASS. Every pre-existing test must still pass — the `extends`-free
path returns `own` unchanged, so nothing that omits `extends` can behave
differently.

- [ ] **Step 8: Typecheck**

Run: `ty check gateway/tool_access.py`

Expected: no new errors.

- [ ] **Step 9: Commit**

```bash
git add gateway/tool_access.py tests/gateway/test_tool_access.py
git commit -m "feat(rbac): role inheritance via extends in the roles config

A user holds exactly one role, so composing capabilities previously meant
duplicating a role's toolsets — a silent drift hazard. _coerce_roles now
resolves an optional extends key (string or list) in two passes, so a role
inherits the transitive union of its parents and forward references work."
```

---

### Task 2: Fail-closed edge cases for `extends`

The resolver from Task 1 already handles these paths; this task proves each one
fails closed and locks the behavior in. A reviewer could accept Task 1's happy
path and still reject these semantics, which is why it is its own gate.

**Files:**
- Test: `tests/gateway/test_tool_access.py`
- Modify (only if a test exposes a gap): `gateway/tool_access.py`

**Interfaces:**
- Consumes: `_coerce_extends` and `_resolve_role_extends` from Task 1.
- Produces: no new symbols.

- [ ] **Step 1: Write the failing tests**

Append to `class TestRoleInheritance` in `tests/gateway/test_tool_access.py`:

```python
    def test_extends_undefined_role_contributes_nothing(self):
        # A typo'd parent must not silently widen OR silently erase the grant.
        p = policy_from_extra({
            "user_roles": {"U_pub": "publisher"},
            "roles": {"publisher": {"extends": "ghost", "toolsets": ["webflow"]}},
        })
        assert p.grant_for("U_pub") == frozenset({"webflow"})

    def test_extends_cycle_does_not_hang_and_keeps_own_toolsets(self):
        p = policy_from_extra({
            "user_roles": {"U_a": "a", "U_b": "b"},
            "roles": {
                "a": {"extends": "b", "toolsets": ["web"]},
                "b": {"extends": "a", "toolsets": ["notion"]},
            },
        })
        # The cycle is broken, not fatal: each role keeps its own toolsets and
        # picks up whatever its parent contributes before the edge is cut.
        assert "web" in p.grant_for("U_a")
        assert "notion" in p.grant_for("U_b")

    def test_role_extending_itself_is_not_fatal(self):
        p = policy_from_extra({
            "user_roles": {"U_a": "a"},
            "roles": {"a": {"extends": "a", "toolsets": ["web"]}},
        })
        assert p.grant_for("U_a") == frozenset({"web"})

    def test_extends_a_builtin_role(self):
        p = policy_from_extra({
            "user_roles": {"U_pub": "publisher"},
            "roles": {"publisher": {"extends": "readonly", "toolsets": ["webflow"]}},
        })
        grant = p.grant_for("U_pub")
        assert BUILTIN_ROLES["readonly"] <= grant
        assert "webflow" in grant

    def test_extends_a_config_overridden_builtin(self):
        # Overriding a built-in and extending it must see the OVERRIDE, not the
        # shipped default — both live in the same map.
        p = policy_from_extra({
            "user_roles": {"U_pub": "publisher"},
            "roles": {
                "readonly": {"toolsets": ["web", "custom_thing"]},
                "publisher": {"extends": "readonly"},
            },
        })
        assert p.grant_for("U_pub") == frozenset({"web", "custom_thing"})

    def test_extends_admin_inherits_the_wildcard(self):
        # Legal and deliberate — asserted so it is never discovered by accident.
        p = policy_from_extra({
            "user_roles": {"U_pub": "publisher"},
            "roles": {"publisher": {"extends": "admin"}},
        })
        assert p.can_use_tool("U_pub", "terminal") is True

    def test_extends_wrong_type_is_ignored(self):
        p = policy_from_extra({
            "user_roles": {"U_pub": "publisher"},
            "roles": {"publisher": {"extends": 42, "toolsets": ["webflow"]}},
        })
        assert p.grant_for("U_pub") == frozenset({"webflow"})

    def test_extends_does_not_activate_rbac_on_its_own(self):
        # Activation stays keyed to user_roles presence — the boundary that
        # keeps installs which never opted into RBAC byte-for-byte unchanged.
        p = policy_from_extra({
            "roles": {
                "base": {"toolsets": ["web"]},
                "publisher": {"extends": "base"},
            },
        })
        assert p.enabled is False

    def test_extends_does_not_weaken_deny_until_assigned(self):
        p = policy_from_extra({
            "user_roles": {"U_pub": "publisher"},
            "roles": {
                "base": {"toolsets": ["web"]},
                "publisher": {"extends": "base"},
            },
        })
        assert p.is_authorized("U_nobody") is False
        assert p.grant_for("U_nobody") is None
```

- [ ] **Step 2: Run them**

Run: `scripts/run_tests.sh tests/gateway/test_tool_access.py::TestRoleInheritance -v`

Expected: PASS, all 15 (6 from Task 1 + 9 here). Task 1's implementation
already covers these paths. **If any fail, fix `gateway/tool_access.py` — do not
weaken the test.** The likely culprits: an unguarded recursion (cycle test hangs
or raises `RecursionError`), or `_coerce_extends` not returning `[]` for a
non-string non-sequence (wrong-type test).

- [ ] **Step 3: Confirm the cycle test cannot hang the suite**

Run: `scripts/run_tests.sh tests/gateway/test_tool_access.py::TestRoleInheritance::test_extends_cycle_does_not_hang_and_keeps_own_toolsets -v`

Expected: PASS in well under a second. A hang here means `resolving` is not
being consulted before recursing.

- [ ] **Step 4: Run the whole gateway suite**

Run: `scripts/run_tests.sh tests/gateway/`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/gateway/test_tool_access.py gateway/tool_access.py
git commit -m "test(rbac): fail-closed coverage for role inheritance

Undefined parents, cycles, self-reference, built-in and overridden-builtin
parents, admin wildcard inheritance, and wrong-typed extends. Also asserts
extends does not activate RBAC and does not weaken deny-until-assigned."
```

---

### Task 3: Webflow MCP server + role composition + approval gate (config)

All four config edits, in the repo-root `config.yaml` working copy. No code.

**Files:**
- Modify: `config.yaml` (repo root, untracked working copy of the VM's `~/.hermes/config.yaml`)

**Interfaces:**
- Consumes: `extends` from Task 1.
- Produces: MCP server named `webflow` → toolsets `webflow` (alias) and
  `mcp-webflow` (canonical); roles `webflow_publisher` and `marketing_publisher`.

- [ ] **Step 1: Add the MCP server block**

In `config.yaml`, under the existing `mcp_servers:` key (which currently holds
only `github:`), add a sibling entry. Keep `github:` untouched:

```yaml
mcp_servers:
  github:
    # ...unchanged...
  webflow:
    # Local stdio server. Webflow's HOSTED MCP (mcp.webflow.com) is OAuth-only
    # — verified 401 + www-authenticate: Bearer realm="OAuth" — and an OAuth
    # handshake would hang the headless gateway, so we run the npm package
    # with a static token instead.
    command: npx
    args: ["-y", "webflow-mcp-server@1.0.0"]
    env:
      # The package reads WEBFLOW_TOKEN; ~/.hermes/.env holds
      # WEBFLOW_API_TOKEN. This remap is REQUIRED: _build_safe_env passes only
      # PATH/HOME/USER/LANG/... plus vars named here, so an unlisted .env var
      # never reaches the subprocess.
      WEBFLOW_TOKEN: "${WEBFLOW_API_TOKEN}"
    timeout: 120
    connect_timeout: 60
    enabled: true
    tools:
      # Trimmed from the package's 42 tools. Designer-session tools (de_*,
      # element_*, style_tool, variable_tool) need the Bridge App and are dead
      # weight headless; site-script tools run arbitrary JS on the live site;
      # the *_live write variants are omitted so publishing is exactly one
      # explicit, gated step.
      include:
        - sites_list
        - sites_get
        - collections_list
        - collections_get
        - collections_items_list_items
        - pages_list
        - pages_get_content
        - pages_get_metadata
        - components_list
        - asset_tool
        - collections_items_create_item
        - collections_items_update_items
        - collections_items_publish_items
        - sites_publish
        - collections_items_delete_item
```

- [ ] **Step 2: Add the two roles under `slack.roles`**

Place `webflow_publisher` and `marketing_publisher` immediately after the
existing `marketing:` role so the composition reads top-to-bottom. **Leave the
`marketing:` role's own `toolsets` list exactly as it is** — the whole point of
`extends` is that it is never edited here.

```yaml
    # (existing `marketing:` role above, unchanged)
    webflow_publisher:
      # Small, reusable capability role. Both names are required: the
      # enabled_toolsets filter sees the bare alias `webflow`, while the
      # execution backstop reads the canonical `mcp-webflow` off
      # ToolEntry.toolset without resolving aliases. Granting one is a bug.
      toolsets:
      - webflow
      - mcp-webflow
    marketing_publisher:
      # marketing + Webflow. Composed via `extends` so a future edit to
      # `marketing` is inherited automatically and the two cannot drift.
      extends:
      - marketing
      - webflow_publisher
```

- [ ] **Step 3: Move the target user onto the new role**

In `config.yaml` under `slack.user_roles:`, change the one line:

```yaml
    U02S08M50S3: marketing_publisher   # Adi Gorelik — was: marketing
```

Leave `channel_roles.C0BCX83K82V: marketing` **unchanged** — that is what keeps
every other poster in the content channel off Webflow.

- [ ] **Step 4: Add the approval gate**

Add `require_for_tools` to the existing top-level `approvals:` block, preserving
its current keys (`mode: manual`, `timeout: 60`, `cron_mode: deny`,
`mcp_reload_confirm: true`, `destructive_slash_confirm: true`):

```yaml
approvals:
  # ...existing keys unchanged...
  require_for_tools:
  # MCP tools dispatch PREFIXED as mcp_{server}_{tool}. A bare `sites_publish`
  # matches nothing and the gate is SILENTLY INERT — configured, visible, and
  # not gating. Confirm against `hermes mcp test webflow` output before trusting.
  - "mcp_webflow_sites_publish"
  - "mcp_webflow_collections_items_publish_items"
  - "mcp_webflow_collections_items_delete_item"
```

- [ ] **Step 5: Validate the YAML parses**

Run:

```bash
source .venv/bin/activate 2>/dev/null || source venv/bin/activate
python -c "
import yaml
c = yaml.safe_load(open('config.yaml'))
wf = c['mcp_servers']['webflow']
print('command      :', wf['command'], wf['args'])
print('env          :', wf['env'])
print('tools.include:', len(wf['tools']['include']), 'tools')
print('user_role    :', c['slack']['user_roles']['U02S08M50S3'])
print('channel_role :', c['slack']['channel_roles']['C0BCX83K82V'])
print('gated        :', c['approvals']['require_for_tools'])
"
```

Expected: `command: npx ['-y', 'webflow-mcp-server@1.0.0']`, `env` showing the
literal `${WEBFLOW_API_TOKEN}` (interpolation happens at load, not in the file),
`15 tools`, `marketing_publisher`, `marketing`, and the three prefixed globs.

- [ ] **Step 6: Prove the composition resolves correctly**

This is the step that catches a mis-wired `extends` before it ever reaches the
VM. Run:

```bash
python -c "
import yaml
from gateway.tool_access import policy_from_extra
slack = yaml.safe_load(open('config.yaml'))['slack']
p = policy_from_extra({k: slack[k] for k in ('user_roles','roles','channel_roles') if k in slack})
adi   = p.grant_for('U02S08M50S3', 'C0BCX83K82V')
other = p.grant_for('U_teammate',  'C0BCX83K82V')
print('adi   webflow    :', {'webflow','mcp-webflow'} <= adi)
print('other webflow    :', bool({'webflow','mcp-webflow'} & other))
print('adi   superset   :', (other - {'webflow','mcp-webflow'}) <= adi)
print('adi   count      :', len(adi), '| other count:', len(other))
"
```

Expected:
```
adi   webflow    : True
other webflow    : False
adi   superset   : True
adi   count      : 17 | other count: 15
```

`adi superset: True` is the inheritance assertion — everything a plain
`marketing` poster gets, Adi also gets. If it prints `False`, `extends` did not
resolve and the roles have drifted apart, which is exactly the failure this
design exists to prevent.

- [ ] **Step 7: Commit**

`config.yaml` is untracked by design (it holds live Slack IDs and channel
prompts) and **must not be committed**. Verify it stays out of the index, then
commit nothing for this task:

```bash
git status --short config.yaml   # expect: ?? config.yaml
```

If a previous step staged it, unstage with `git restore --staged config.yaml`.
Report the diff to Shai rather than committing it.

---

### Task 4: Deploy and verify on the VM

Shai pushes `config.yaml` to the VM. These are the checks that must pass before
the change is considered done, in order — each one can only be trusted if the
one above it passed.

**Files:** none (verification only).

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: confirmation, or a concrete failure to feed back into Task 3.

- [ ] **Step 1: Hand off the config**

Report to Shai: the four edited regions of `config.yaml` (`mcp_servers.webflow`,
`slack.roles`, `slack.user_roles`, `approvals.require_for_tools`), and that the
code change in Tasks 1-2 must be deployed **together with** it — a config using
`extends` against a gateway without Task 1 resolves `marketing_publisher` to an
empty toolset, silently stripping Adi of every non-floor tool.

- [ ] **Step 2: Confirm the server connects**

On the VM: `hermes mcp list` shows `webflow` enabled, then
`hermes mcp test webflow`.

Expected: connects with no OAuth prompt, lists exactly 15 tools. If it hangs,
the config resolved to the hosted OAuth server rather than stdio — recheck that
`command:`, not `url:`, is set.

- [ ] **Step 3: Cross-check the approval globs against reality**

Compare the dispatched names in the `hermes mcp test webflow` output against the
three entries in `approvals.require_for_tools`. They must match exactly.

This is the single step most worth doing carefully: a mismatch produces a config
that looks like it gates publishing but does not, and nothing at runtime will
say so.

- [ ] **Step 4: Confirm the role assignment**

On the VM: `hermes users list`.

Expected: `U02S08M50S3` / Adi Gorelik shows `marketing_publisher`; every other
user unchanged.

- [ ] **Step 5: Functional check as the target user**

In Slack channel `C0BCX83K82V`, as Adi: ask the agent to list Webflow sites, then
to create a draft CMS item.

Expected: both succeed. Then ask it to publish — expected: an in-thread approval
prompt offering *Allow once* / *Allow session*, never *always*.

- [ ] **Step 6: The single-user check**

In `C0BCX83K82V`, as a **different** teammate (who receives `marketing` via
`channel_roles`): ask the agent to list Webflow sites.

Expected: it cannot — the Webflow tools are absent from its tool list, and a
forced invocation is refused by the execution backstop.

**If this succeeds for anyone other than Adi, the grant landed on the wrong
role. Stop and revert.** This is the requirement the whole design turns on.

- [ ] **Step 7: Confirm no regression for other roles**

As Itamar Amit (`U01SN6Y7V8A`, `readonly`): Webflow tools absent. As a poster in
either issue-tracking channel (`operator` via `channel_roles`): Webflow tools
absent, and their existing Notion issue-tracking still works.

- [ ] **Step 8: Report**

State plainly which checks passed and which did not. If any step failed, say so
with the actual output rather than describing the intent.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §1 MCP server block (stdio, pinned, env remap) | Task 3 Step 1 |
| §2 Tool surface 42 → 15 | Task 3 Step 1 (`tools.include`) |
| §3 Approval gate, prefixed names | Task 3 Step 4; verified Task 4 Step 3 |
| §4 Role inheritance `extends` + fail-closed cases | Tasks 1 and 2 |
| §5 Single-user grant, both toolset names | Task 3 Steps 2-3; verified Task 4 Step 6 |
| §6 CLI exposure (accepted, no action) | No task — documented as accepted |
| Verification 1 (Node ≥22.3) | Already cleared 2026-08-02 |
| Verification 2-10 | Task 3 Steps 5-6, Task 4 Steps 2-7 |

**Placeholder scan:** no TBD/TODO; every code step carries real code; every run
step names an exact command and its expected output.

**Type consistency:** `_coerce_extends` returns `List[str]` and is consumed by
`parents: Dict[str, List[str]]`; `_resolve_role_extends` returns `FrozenSet[str]`
matching `_coerce_roles`'s unchanged `Dict[str, FrozenSet[str]]` return type.
`grant_for` returns `Optional[FrozenSet[str]]` — Task 2's
`test_extends_does_not_weaken_deny_until_assigned` asserts the `None` case,
every other test asserts against a non-None grant.

**Known gap, deliberate:** §6 (CLI sessions on the VM also receive the Webflow
tools, since RBAC is a messaging-platform control) has no task. The spec accepts
this rather than fixing it; the optional mitigation is recorded there if it is
ever wanted.
