"""Matrix backend pure-function tests."""
from __future__ import annotations

from claude_hive.matrix import (
    MatrixBackend,
    device_id_for_session,
    freshest_peer,
    localpart_for_agent,
)


def test_localpart_for_agent_strips_prefix_and_sanitizes():
    assert localpart_for_agent("agent_a25759143c6f8332") == "aa25759143c6f8332"
    assert localpart_for_agent("agent_A25-x") == "aa25x"


def test_device_id_keeps_session_id_readable():
    assert device_id_for_session("73974cd0-44f0-4517") == "73974cd0-44f0-4517"
    assert device_id_for_session("thr weird:id") == "thr_weird_id"


def test_freshest_peer_prefers_newest_heartbeat():
    dead = {"name": "claude-hive", "user_id": "@sdead:hive.local", "last_active": 100}
    live = {"name": "claude-hive", "user_id": "@slive:hive.local", "last_active": 900}
    assert freshest_peer([dead, live]) is live
    assert freshest_peer([live, dead]) is live
    assert freshest_peer([]) is None


def test_checkpoint_file_prefers_agent_sync_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    b = MatrixBackend(None, "sess-1")
    assert b.checkpoint_file().name == "sess-1"
    b.sync_key = "aa25759143c6f8332"
    assert b.checkpoint_file().name == "aa25759143c6f8332"


def test_owns_session_state_tracks_by_session_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    b = MatrixBackend(None, "abc-123")
    b.access_token = "tok-old"
    assert b.owns_session_state() is True
    (tmp_path / "by-session").mkdir()
    f = tmp_path / "by-session" / "abc-123.json"
    f.write_text('{"access_token": "tok-old"}')
    assert b.owns_session_state() is True
    f.write_text('{"access_token": "tok-newer-server"}')
    assert b.owns_session_state() is False


def test_owns_delivery_yields_to_newer_session_of_same_agent(tmp_path, monkeypatch):
    import json

    from claude_hive import hook
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    old = MatrixBackend(None, "old-sess")
    old.access_token, old.sync_key = "tok-old", "akey"
    assert old.owns_delivery() is True
    hook.write_atomic(hook.delivery_owner_file("akey"), json.dumps({"session_id": "new-sess"}))
    assert old.owns_session_state() is True
    assert old.owns_delivery() is False


def test_clear_hook_state_releases_only_own_ownership(tmp_path, monkeypatch):
    import json

    from claude_hive import hook
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    monkeypatch.delenv("ZELLIJ", raising=False)
    owner = hook.delivery_owner_file("akey")
    old = MatrixBackend(None, "old-sess")
    old.sync_key = "akey"
    hook.write_atomic(owner, json.dumps({"session_id": "new-sess"}))
    old.clear_hook_state()
    assert owner.exists()
    hook.write_atomic(owner, json.dumps({"session_id": "old-sess"}))
    old.clear_hook_state()
    assert not owner.exists()
