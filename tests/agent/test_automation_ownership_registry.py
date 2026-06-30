"""Registry data layer: config, identity, key scheme, path classification."""
import json

import agent.automation_ownership as ao
from hermes_constants import get_hermes_home


def _touch(p, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_artifact_key_format():
    assert ao.artifact_key("skill", "weekly-report") == "skill:weekly-report"
    assert ao.artifact_key("cron", "9f3a1c2b7e10") == "cron:9f3a1c2b7e10"


def test_path_to_artifact_key_script():
    p = get_hermes_home() / "scripts" / "reports" / "weekly.py"
    assert ao.path_to_artifact_key(str(p)) == ("script:reports/weekly.py", "script")


def test_path_to_artifact_key_skill_file():
    p = get_hermes_home() / "skills" / "weekly-report" / "SKILL.md"
    assert ao.path_to_artifact_key(str(p)) == ("skill:weekly-report", "skill")


def test_path_to_artifact_key_categorized_skill():
    p = get_hermes_home() / "skills" / "mlops" / "weekly-report" / "references" / "x.md"
    # The skill NAME is the leaf dir that directly contains SKILL.md; here we
    # only have the path, so classification keys on the dir holding SKILL.md.
    _touch(get_hermes_home() / "skills" / "mlops" / "weekly-report" / "SKILL.md")
    assert ao.path_to_artifact_key(str(p)) == ("skill:weekly-report", "skill")


def test_path_to_artifact_key_automation_bundle():
    p = get_hermes_home() / "automations" / "weekly-report" / "scripts" / "run.sh"
    assert ao.path_to_artifact_key(str(p)) == ("automation:weekly-report", "automation")


def test_path_to_artifact_key_outside_returns_none(tmp_path):
    assert ao.path_to_artifact_key(str(tmp_path / "scripts" / "x.sh")) is None
    # A file directly under HERMES_HOME but not in a managed dir:
    assert ao.path_to_artifact_key(str(get_hermes_home() / "config.yaml")) is None


def test_registry_round_trip():
    rec = {"kind": "cron", "owner": {"platform": "slack", "user_id": "U1", "display_name": "Alice"},
           "collaborators": [], "source": "creator"}
    ao._put_record("cron:abc", rec)
    assert ao.get_record("cron:abc")["owner"]["user_id"] == "U1"
    # Persisted to disk as JSON under ownership/registry.json
    reg = json.loads((get_hermes_home() / "ownership" / "registry.json").read_text(encoding="utf-8"))
    assert reg["automations"]["cron:abc"]["owner"]["user_id"] == "U1"


def test_corrupt_registry_degrades_to_empty():
    path = get_hermes_home() / "ownership" / "registry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert ao._load_registry() == {"version": 1, "automations": {}}
    assert ao.get_record("anything") is None  # must not raise


def test_is_enabled_default_true(monkeypatch):
    monkeypatch.setattr(ao, "_config", lambda: {})
    assert ao.is_enabled() is True
    monkeypatch.setattr(ao, "_config", lambda: {"enabled": False})
    assert ao.is_enabled() is False


def test_current_identity_none_without_user(monkeypatch):
    monkeypatch.setattr(ao, "get_session_env", lambda name, default="": "")
    assert ao.current_identity() is None


def test_current_identity_from_session(monkeypatch):
    vals = {"HERMES_SESSION_USER_ID": "U7", "HERMES_SESSION_USER_NAME": "Bob",
            "HERMES_SESSION_PLATFORM": "slack"}
    monkeypatch.setattr(ao, "get_session_env", lambda name, default="": vals.get(name, default))
    ident = ao.current_identity()
    assert ident.user_id == "U7" and ident.display_name == "Bob" and ident.platform == "slack"
