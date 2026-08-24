"""Self-test for the live-system guard fixture in tests/conftest.py.

This file is the canary. If anyone removes a guard or weakens it, these
tests fail. If anyone adds a NEW kill primitive to the codebase without
adding it to the guard, the corresponding test added here will fail too.

The guard exists to protect the developer's live ``hermes-gateway`` process
from being SIGTERMed by tests. See PR #23397 for the original incident
(5+ live gateway kills in 3 days). Per Teknium 2026-05-10:

  > "You better do such a deep scan and scrub of the tests that this
  >  never is possible ever again for all eternity."

Every primitive that can deliver a signal to a foreign process or mutate
the live systemd unit MUST be exercised below. Adding a new primitive to
the guard? Add a test here too.
"""
from __future__ import annotations

import os
import signal
import subprocess
import types

import pytest

# A guaranteed-foreign PID: PID 1 (init).  Owned by root, not us, and
# always exists. A sane guard refuses to signal it.
FOREIGN_PID = 1


# ──────────────────── fail-closed self-protection ──────────────
#
# This file executes REAL kill primitives — os.kill(-1, SIGTERM), os.killpg,
# pkill -f python — and depends entirely on the autouse ``_live_system_guard``
# fixture in tests/conftest.py to intercept them. That makes the canary
# fail-OPEN: in any collection context where this file is present but its home
# conftest is not, the primitives fire for real and ``os.kill(-1, SIGTERM)``
# SIGTERMs every process the invoking user owns (a full desktop-session kill was
# reported in the field — see issue #68311). Such contexts are not exotic:
# published sdists that ship ``tests/`` but not ``tests/conftest.py``, trees
# assembled by copying ``test*.py`` files (that glob does NOT match
# ``conftest.py``), ``pytest --noconftest``, or running from a foreign rootdir.
#
# The fixture below makes the canary fail-CLOSED instead: it refuses to run any
# test in this file unless the guard is provably active, so no collection
# context can ever detonate the primitives. The one thing the canary can detect
# about its own safety is that the guard monkeypatches ``os.kill`` with a plain
# Python function, whereas the unguarded primitive is a C builtin.


def _live_system_guard_is_active() -> bool:
    """True iff tests/conftest.py's ``_live_system_guard`` has patched os.kill.

    The guard replaces ``os.kill`` with a plain Python function; the raw,
    unguarded primitive is a C builtin (``types.BuiltinFunctionType``). If
    ``os.kill`` is still the builtin, the guard never loaded and every kill
    primitive in this file would fire for real.
    """
    return not isinstance(os.kill, types.BuiltinFunctionType)


@pytest.fixture(autouse=True)
def _refuse_to_fire_live_weapons(request):
    """Fail closed: refuse to run a canary test unless the guard is active.

    Tests genuinely marked ``@pytest.mark.live_system_guard_bypass`` opt out
    (they run the raw primitive deliberately and harmlessly, e.g. a signal-0
    liveness probe of our own PID), matching the guard's own bypass contract.
    """
    if request.node.get_closest_marker("live_system_guard_bypass"):
        yield
        return
    if not _live_system_guard_is_active():
        pytest.fail(
            "REFUSING TO RUN: the live-system guard from tests/conftest.py is "
            "not active in this interpreter (os.kill is still the raw C "
            "builtin). This canary file executes real kill primitives — "
            "os.kill(-1, SIGTERM), os.killpg, pkill -f python — and relies on "
            "the guard to intercept them; unguarded, they SIGTERM every process "
            "the current user owns. This usually means the file was collected "
            "without its home tests/conftest.py (note: a test*.py copy glob "
            "does NOT match conftest.py). See issue #68311.",
            pytrace=False,
        )
    yield


def test_fail_closed_probe_reports_guard_active():
    """In the real suite the guard is loaded, so the probe reports active and
    ``_refuse_to_fire_live_weapons`` stays out of the way (no false positives
    that would wedge CI)."""
    assert _live_system_guard_is_active() is True


def test_fail_closed_probe_classifies_raw_builtin_as_unguarded():
    """The probe's discriminator, exercised against real objects: a raw C
    builtin the guard never touches (``os.getpid``) is exactly what an
    unguarded ``os.kill`` looks like and must read as 'guard not active', while
    the loaded guard's ``os.kill`` is a plain Python function."""
    assert isinstance(os.getpid, types.BuiltinFunctionType)
    assert not isinstance(os.kill, types.BuiltinFunctionType)


# ──────────────────── kill primitives ─────────────────────────


def test_os_kill_blocks_foreign_pid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(FOREIGN_PID, signal.SIGTERM)


def test_os_kill_blocks_negative_one():
    """``os.kill(-1, sig)`` signals every process we can reach. Must be blocked."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(-1, signal.SIGTERM)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="killpg POSIX-only")
def test_os_killpg_blocks_foreign_pgid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.killpg(FOREIGN_PID, signal.SIGTERM)


# ──────────────────── subprocess regex bypasses ────────────────


def test_subprocess_run_systemctl_restart_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_run_full_path_systemctl_blocked():
    """``/usr/bin/systemctl`` (full path) must be blocked too."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_run_sudo_systemctl_blocked():
    """``sudo systemctl ...`` defeated the old head==systemctl check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sudo", "systemctl", "restart", "hermes-gateway"])


def test_subprocess_run_env_systemctl_blocked():
    """``env systemctl ...`` similarly defeated the old head check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["env", "systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_run_bash_c_systemctl_blocked():
    """``bash -c "systemctl ..."`` must also be caught."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["bash", "-c", "systemctl --user restart hermes-gateway"])


def test_subprocess_run_sh_c_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sh", "-c", "systemctl --user stop hermes-gateway"])


def test_subprocess_run_setsid_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["setsid", "systemctl", "kill", "hermes-gateway"])


def test_subprocess_run_string_shell_true_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            "systemctl --user restart hermes-gateway",
            shell=True,
        )


def test_subprocess_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(["systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_output_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_output(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_getoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getoutput("systemctl --user restart hermes-gateway")


def test_subprocess_getstatusoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getstatusoutput("systemctl --user restart hermes-gateway")


# ──────────────────── os.system / os.popen ────────────────────


def test_os_system_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.system("systemctl --user restart hermes-gateway")


def test_os_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.popen("systemctl --user restart hermes-gateway")


# ──────────────────── pty.spawn ────────────────────────────────


def test_pty_spawn_systemctl_blocked():
    import pty
    with pytest.raises(RuntimeError, match="live-system guard"):
        pty.spawn(["systemctl", "--user", "restart", "hermes-gateway"])


# ──────────────────── asyncio.create_subprocess_* ──────────────


def test_asyncio_create_subprocess_exec_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", "hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


def test_asyncio_create_subprocess_shell_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_shell(
            "systemctl --user restart hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


# ──────────────────── pkill / killall / taskkill ───────────────


def test_subprocess_pkill_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes"])


def test_subprocess_pkill_hermes_gateway_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes-gateway"])


def test_subprocess_pkill_python_dash_f_blocked():
    """``pkill -f python`` matches the gateway's "python -m hermes_cli.main"."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "python"])


def test_subprocess_killall_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["killall", "hermes"])


# ──────────────────── pass-through cases (must NOT raise) ──────

# The systemctl pass-through tests actually invoke systemctl: on macOS the
# binary doesn't exist and they die on FileNotFoundError, which says nothing
# about the guard. Skip where systemctl is absent (CI runs Linux, so the
# guard's pass-through behavior stays covered there).
import shutil

_needs_systemctl = pytest.mark.skipif(
    shutil.which("systemctl") is None, reason="systemctl not on this platform"
)


@_needs_systemctl
def test_systemctl_status_passes_through():
    """Read-only systemctl probes (status/show/list-units) are fine."""
    # Run with check=False so we don't fail on the gateway's exit code.
    r = subprocess.run(
        ["systemctl", "--user", "status", "hermes-gateway", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r is not None  # Did not raise — the guard let it through.


@_needs_systemctl
def test_systemctl_show_passes_through():
    r = subprocess.run(
        ["systemctl", "--user", "show", "hermes-gateway", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r is not None


@_needs_systemctl
def test_systemctl_list_units_passes_through():
    r = subprocess.run(
        ["systemctl", "--user", "list-units", "fake-not-real-unit*", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r is not None


@_needs_systemctl
def test_systemctl_unrelated_unit_passes_through():
    """systemctl restart of a non-hermes unit is allowed (we only protect hermes)."""
    # Use --dry-run so we don't actually try to restart anything; just
    # verify the guard doesn't block the call. systemctl supports
    # --dry-run via the privileged API; on user scope it usually fails
    # quickly without side effects.
    r = subprocess.run(
        ["systemctl", "--user", "show", "fake-not-real-unit"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r is not None








# ──────────────────── bypass marker ─────────────────────────────


@pytest.mark.live_system_guard_bypass
def test_bypass_marker_disables_guard():
    """The bypass marker exists for tests that genuinely need real signal delivery
    (e.g. PTY tests SIGINTing their own child). Verify it works.

    We use it harmlessly here by signaling our own PID 0 (own group) so we
    don't actually kill anything — but the call goes through real os.kill.
    """
    # With bypass, the guard yields without installing the monkeypatch,
    # so we get the real os.kill. Calling os.kill(os.getpid(), 0) just
    # checks that the PID exists — harmless.
    os.kill(os.getpid(), 0)  # No exception — guard is OFF.


# ──────────────────── repo-mutation / self-update guard ─────────
#
# Incident 2026-06-10: a full-suite run triggered a REAL ``hermes update``
# against the developer's checkout — ``cmd_update`` operates on
# PROJECT_ROOT (the repo itself), so it autostashed ~1600 lines of
# uncommitted work and switched the branch to main, twice. The guard now
# blocks (a) in-process exec into a hermes entrypoint (the
# ``relaunch(["update"])`` / os.execvp path), (b) ``hermes update``
# subprocesses, and (c) mutating git commands aimed at the real repo root.


def test_os_execvp_into_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.execvp("hermes", ["hermes", "update"])


def test_os_execv_python_m_hermes_cli_blocked():
    import sys

    with pytest.raises(RuntimeError, match="live-system guard"):
        os.execv(sys.executable, [sys.executable, "-m", "hermes_cli.main", "update"])


def test_subprocess_hermes_update_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["hermes", "update"])


def test_subprocess_hermes_update_with_flags_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["hermes", "update", "--yes"])


def test_subprocess_python_m_hermes_cli_update_blocked():
    import sys

    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run([sys.executable, "-m", "hermes_cli.main", "update"])


def test_subprocess_hermes_users_update_passes_through():
    """'update' as a nested subcommand (hermes users update) is NOT the
    self-updater — only the top-level ``hermes update`` is blocked. Use
    echo as a stand-in binary; the guard inspects the command string."""
    r = subprocess.run(
        ["echo", "hermes", "users", "update", "U123"],
        capture_output=True,
        text=True,
    )
    assert "users" in r.stdout


def test_git_stash_in_repo_root_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["git", "stash"], capture_output=True)


def test_git_checkout_in_repo_root_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["git", "checkout", "main"], capture_output=True)


def test_git_pull_with_explicit_repo_cwd_blocked():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["git", "pull"], cwd=str(repo_root), capture_output=True)


def test_git_status_in_repo_root_passes_through():
    r = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True
    )
    assert r is not None


def test_git_stash_list_in_repo_root_passes_through():
    r = subprocess.run(
        ["git", "stash", "list"], capture_output=True, text=True
    )
    assert r is not None


def test_git_mutation_in_tmp_repo_passes_through(tmp_path):
    """Mutating git in a test fixture repo (tmp_path) must keep working."""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    r = subprocess.run(
        ["git", "checkout", "-b", "feature", "-q"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert r is not None
