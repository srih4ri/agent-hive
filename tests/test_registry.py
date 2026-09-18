"""Agent identity registry tests: stable agent_id across connections."""
from __future__ import annotations

import pytest

from claude_hive import registry


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))


def test_load_empty_registry_has_shape():
    reg = registry.load()
    assert reg == {"agents": {}, "connections": {}}


def test_save_and_reload_roundtrip():
    reg = registry.load()
    agent = registry.ensure_agent(reg, "backend", cwd="/repo")
    registry.bind_connection(reg, "claude:abc123", agent, "claude")
    registry.save(reg)

    reloaded = registry.load()
    assert reloaded["agents"]["backend"]["agent_id"] == agent["agent_id"]
    assert reloaded["connections"]["claude:abc123"]["status"] == "active"


def test_ensure_agent_is_idempotent_and_keeps_agent_id():
    reg = registry.load()
    first = registry.ensure_agent(reg, "reviewer", cwd="/a")
    second = registry.ensure_agent(reg, "reviewer", cwd="/b")
    assert first["agent_id"] == second["agent_id"]
    assert second["last_cwd"] == "/b"
    assert len(reg["agents"]) == 1


def test_agent_survives_reconnect_from_new_connection():
    reg = registry.load()
    agent = registry.ensure_agent(reg, "reviewer")
    registry.bind_connection(reg, "claude:old", agent, "claude")
    registry.release_connection(reg, "claude:old")
    registry.bind_connection(reg, "codex:thr_1", agent, "codex")

    assert registry.agent_for_connection(reg, "codex:thr_1")["agent_id"] == agent["agent_id"]
    assert registry.connections_for_agent(reg, agent["agent_id"]) == ["codex:thr_1"]


def test_agent_lookup_by_id_and_name():
    reg = registry.load()
    agent = registry.ensure_agent(reg, "logs")
    assert registry.agent_by_id(reg, agent["agent_id"])["name"] == "logs"
    assert registry.agent_by_name(reg, "logs") is agent
    assert registry.agent_by_id(reg, "agent_nope") is None
    assert registry.agent_by_name(reg, "nope") is None


def test_agent_for_unknown_connection_is_none():
    reg = registry.load()
    assert registry.agent_for_connection(reg, "claude:never-seen") is None


def test_candidates_sorted_by_recency():
    reg = registry.load()
    old = registry.ensure_agent(reg, "old-agent")
    old["last_seen"] = 100
    fresh = registry.ensure_agent(reg, "fresh-agent")
    fresh["last_seen"] = 200
    assert [a["name"] for a in registry.candidates(reg)] == ["fresh-agent", "old-agent"]


def test_suggest_name_prefers_free_hint():
    reg = registry.load()
    assert registry.suggest_name(reg, "api-debugger") == "api-debugger"


def test_suggest_name_avoids_collisions():
    reg = registry.load()
    registry.ensure_agent(reg, "taken")
    registry.ensure_agent(reg, "hive-agent-1")
    assert registry.suggest_name(reg, "taken") == "hive-agent-2"
    assert registry.suggest_name(reg, None) == "hive-agent-2"
