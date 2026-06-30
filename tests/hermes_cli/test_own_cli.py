"""hermes own: claim / transfer / collaborators / bundle init."""
import agent.automation_ownership as ao
from hermes_cli.own import run_own
from hermes_constants import get_hermes_home


def test_claim_then_list(capsys):
    assert run_own(["claim", "script:foo.sh", "--user", "U_ALICE", "--name", "Alice"]) == 0
    assert ao.get_record("script:foo.sh")["owner"]["user_id"] == "U_ALICE"
    assert run_own(["list", "--user", "U_ALICE"]) == 0
    assert "script:foo.sh" in capsys.readouterr().out


def test_transfer_by_admin():
    run_own(["claim", "cron:j1", "--user", "U_ALICE", "--name", "Alice"])
    assert run_own(["transfer", "cron:j1", "--to", "U_BOB", "--to-name", "Bob", "--admin"]) == 0
    assert ao.get_record("cron:j1")["owner"]["user_id"] == "U_BOB"


def test_collab_add_remove():
    run_own(["claim", "cron:j2", "--user", "U_ALICE", "--name", "Alice"])
    run_own(["collab", "add", "cron:j2", "--user", "U_BOB", "--name", "Bob"])
    assert any(c["user_id"] == "U_BOB" for c in ao.get_record("cron:j2")["collaborators"])
    run_own(["collab", "remove", "cron:j2", "--user", "U_BOB"])
    assert all(c["user_id"] != "U_BOB" for c in ao.get_record("cron:j2")["collaborators"])


def test_init_scaffolds_bundle_and_registers_owner():
    assert run_own(["init", "weekly-report", "--user", "U_ALICE", "--name", "Alice"]) == 0
    base = get_hermes_home() / "automations" / "weekly-report"
    assert (base / "automation.yaml").exists()
    assert (base / "workflow.md").exists()
    assert (base / "scripts").is_dir() and (base / "assets").is_dir()
    assert ao.get_record("automation:weekly-report")["owner"]["user_id"] == "U_ALICE"
