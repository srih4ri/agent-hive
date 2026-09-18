"""Tests for the setup entry point and the Claude wiring it drives.

These touch the user's real ~/.claude config in production, so every test
here redirects the module-level paths at a tmp_path first.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

install_claude = importlib.import_module("install_claude")
setup = importlib.import_module("setup")


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """Point the Claude adapter at a throwaway home."""
    settings = tmp_path / "claude" / "settings.json"
    monkeypatch.setattr(install_claude, "SETTINGS", settings)
    monkeypatch.setattr(install_claude, "USER_SKILLS", tmp_path / "claude" / "skills")
    monkeypatch.setattr(install_claude, "CLAUDE_JSON", tmp_path / "claude.json")
    return tmp_path


@pytest.fixture
def matrix_dir(tmp_path, monkeypatch):
    d = tmp_path / "matrix"
    (d / "accounts").mkdir(parents=True)
    monkeypatch.setattr(setup, "MATRIX_DIR", d)
    return d


def test_load_tolerates_missing_and_corrupt_files(tmp_path):
    """A machine where Claude Code has never written settings must still
    install cleanly — the old version crashed here."""
    assert install_claude._load(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert install_claude._load(bad) == {}
    empty = tmp_path / "empty.json"
    empty.write_text("")
    assert install_claude._load(empty) == {}


def test_install_hooks_is_idempotent_from_nothing(claude_home):
    assert install_claude.hooks_installed() is False
    assert install_claude.install_hooks() is True
    assert install_claude.hooks_installed() is True
    assert install_claude.install_hooks() is False

    settings = json.loads(install_claude.SETTINGS.read_text())
    assert set(settings["hooks"]) == set(install_claude.HOOK_EVENTS)
    post = settings["hooks"]["PostToolUse"][0]
    assert post["matcher"] == "*"
    assert post["hooks"][0]["command"] == install_claude.HOOK_CMD


def test_install_hooks_preserves_unrelated_config(claude_home):
    install_claude.SETTINGS.parent.mkdir(parents=True)
    install_claude.SETTINGS.write_text(json.dumps({
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-tool"}]}]},
    }))
    install_claude.install_hooks()
    settings = json.loads(install_claude.SETTINGS.read_text())
    assert settings["model"] == "opus"
    commands = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]]
    assert "other-tool" in commands and install_claude.HOOK_CMD in commands


def test_set_env_writes_once_and_backs_up(claude_home):
    assert install_claude.set_env("HIVE_HUMAN_MXID", "@me:hive.local") is True
    assert install_claude.set_env("HIVE_HUMAN_MXID", "@me:hive.local") is False
    settings = json.loads(install_claude.SETTINGS.read_text())
    assert settings["env"]["HIVE_HUMAN_MXID"] == "@me:hive.local"
    assert install_claude.set_env("HIVE_HUMAN_MXID", "@other:hive.local") is True
    assert install_claude.SETTINGS.with_name("settings.json.bak-hive").exists()


def test_install_skills_links_every_skill(claude_home):
    linked = install_claude.install_skills()
    assert "hive-connect" in linked
    assert install_claude.skills_linked() == len(linked)
    assert install_claude.install_skills() == []
    target = install_claude.USER_SKILLS / "hive-connect"
    assert target.is_symlink() and (target / "SKILL.md").exists()


def test_install_skills_replaces_a_stale_link(claude_home, tmp_path):
    install_claude.USER_SKILLS.mkdir(parents=True)
    stale = install_claude.USER_SKILLS / "hive-connect"
    stale.symlink_to(tmp_path / "somewhere-else")
    assert install_claude.skills_linked() < 7
    assert "hive-connect" in install_claude.install_skills()
    assert stale.resolve() == (install_claude.REPO_ROOT / "skills" / "hive-connect")


def test_mcp_status_reads_claude_config(claude_home):
    assert install_claude.mcp_status() == "not registered"
    install_claude.CLAUDE_JSON.write_text(json.dumps({"mcpServers": {"claude-hive": {}}}))
    assert install_claude.mcp_status() == "registered"


def test_mcp_add_command_targets_this_repo():
    cmd = install_claude.mcp_add_command()
    assert cmd[:4] == ["claude", "mcp", "add", "--scope"]
    assert str(install_claude.REPO_ROOT) in cmd
    assert cmd[-1] == "claude-hive-server"


def test_observer_discovered_from_existing_accounts(matrix_dir):
    accounts = matrix_dir / "accounts"
    (accounts / "aa25759143c6f8332.json").write_text("{}")
    (accounts / "s72e576d4034f.json").write_text("{}")
    (accounts / "lookout-nb.json").write_text("{}")
    (accounts / "priya.json").write_text("{}")
    assert setup.existing_observer_localpart() == "priya"
    assert setup._default_observer_localpart() == "priya"


def test_observer_falls_back_to_username(matrix_dir, monkeypatch):
    monkeypatch.setenv("USER", "Ada.Lovelace!")
    assert setup.existing_observer_localpart() is None
    assert setup._default_observer_localpart() == "ada.lovelace"


def test_observer_mxid_prefers_env_then_settings(claude_home, monkeypatch):
    monkeypatch.setenv("HIVE_HUMAN_MXID", "@fromenv:hive.local")
    assert setup.observer_mxid() == "@fromenv:hive.local"
    monkeypatch.setenv("HIVE_HUMAN_MXID", "")
    assert setup.observer_mxid() is None
    monkeypatch.delenv("HIVE_HUMAN_MXID")
    assert setup.observer_mxid() is None
    install_claude.set_env("HIVE_HUMAN_MXID", "@fromsettings:hive.local")
    assert setup.observer_mxid() == "@fromsettings:hive.local"


def test_legacy_artifacts_spares_agent_and_human_accounts(matrix_dir):
    accounts = matrix_dir / "accounts"
    for name in ("s72e576d4034f", "sabc123", "aa25759143c6f8332", "observer", "lookout-nb"):
        (accounts / f"{name}.json").write_text("{}")
    (matrix_dir / "dm-rooms.json").write_text("{}")

    stale, dm_rooms = setup.legacy_artifacts()
    assert {f.stem for f in stale} == {"s72e576d4034f", "sabc123"}
    assert dm_rooms is not None

    setup.step_legacy(check_only=False, prune=True)
    assert not (accounts / "s72e576d4034f.json").exists()
    assert (accounts / "aa25759143c6f8332.json").exists()
    assert (accounts / "observer.json").exists()
    assert not (matrix_dir / "dm-rooms.json").exists()


def test_legacy_prune_is_a_noop_on_a_fresh_install(matrix_dir):
    assert setup.legacy_artifacts() == ([], None)
    setup.step_legacy(check_only=False, prune=True)


def test_generated_homeserver_config_is_closed_by_default():
    config = setup.CONFIG_TEMPLATE.format(
        server_name="hive.local", port=8008, token="deadbeef")
    assert 'server_name = "hive.local"' in config
    assert "allow_federation = false" in config
    assert 'registration_token = "deadbeef"' in config
