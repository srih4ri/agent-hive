"""hive_connect connection/identity resolution tests."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from claude_hive import mcp_server, registry
from claude_hive.mcp_server import (
    Connection,
    ConnState,
    _selection_text,
    _tool_connect,
    resolve_connection,
    session_id_for_pid,
    session_name_for_pid,
    session_name_for_session_id,
    session_pid_for,
)


class StubBackend:
    """MatrixBackend stand-in: records calls, no homeserver."""

    peers_state: list[dict] = []

    def __init__(self, http, session_id):
        self.http = self
        self.session_id = session_id
        self.user_id = f"@s{session_id}:hive.local"
        self.name = self.version = self.agent_id = None
        self.client_name = self.connection_id = None
        self.sync_key = None

    async def ensure_reader_account(self): pass

    async def ensure_agent_account(self, agent_id):
        self.user_id = f"@a{agent_id.removeprefix('agent_')}:hive.local"
        self.sync_key = f"a{agent_id.removeprefix('agent_')}"

    async def ensure_directory_room(self): pass
    async def ensure_bulletin_room(self): pass
    async def peers(self): return list(self.peers_state)
    async def set_displayname(self, name): pass
    async def publish_peer(self, *a, **k): pass
    async def announce(self, text): pass
    async def init_hook_state(self, name): pass
    def set_delivery_mode(self, mode): pass
    async def aclose(self): pass


@pytest.fixture
def hive(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.setattr(mcp_server.hive_matrix, "MatrixBackend", StubBackend)
    monkeypatch.setattr(mcp_server, "_state", ConnState())
    StubBackend.peers_state = []
    return tmp_path


def _connect(**args) -> str:
    args.setdefault("summary", "testing")
    return asyncio.run(_tool_connect(args))[0].text


def test_connect_unmapped_connection_returns_candidates(hive):
    reg = registry.load()
    registry.ensure_agent(reg, "old-reviewer")
    registry.save(reg)
    out = _connect()
    assert "identity selection required" in out
    assert "old-reviewer" in out


def test_connect_with_name_creates_agent_and_binds_connection(hive):
    out = _connect(name="cookie-fixer")
    assert "connected to hive as 'cookie-fixer'" in out
    reg = registry.load()
    agent = registry.agent_by_name(reg, "cookie-fixer")
    assert reg["connections"]["claude:sess-1"]["agent_id"] == agent["agent_id"]


def test_connect_reconnects_mapped_connection_without_name(hive):
    reg = registry.load()
    agent = registry.ensure_agent(reg, "cookie-fixer")
    registry.bind_connection(reg, "claude:sess-1", agent, "claude")
    registry.save(reg)
    out = _connect()
    assert "connected to hive as 'cookie-fixer'" in out


def test_connect_refuses_active_name_without_attach(hive):
    StubBackend.peers_state = [{"name": "cookie-fixer", "user_id": "@sother:hive.local",
                                "last_active": int(time.time())}]
    out = _connect(name="cookie-fixer")
    assert "already ACTIVE" in out
    assert "attach=true" in out

    mcp_server._state = ConnState()
    out = _connect(name="cookie-fixer", attach=True)
    assert "connected to hive as 'cookie-fixer'" in out


def test_resolve_connection_claude_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    conn = resolve_connection()
    assert conn == Connection("claude", "abc-123")
    assert conn.connection_id == "claude:abc-123"


def test_resolve_connection_codex_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thr_123")
    assert resolve_connection() == Connection("codex", "thr_123")


def test_resolve_connection_explicit_beats_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    assert resolve_connection("codex:thr_9") == Connection("codex", "thr_9")


def test_resolve_connection_bare_explicit_uses_client_hint():
    assert resolve_connection("raw-id", "codex") == Connection("codex", "raw-id")


def test_resolve_connection_none_without_any_source(monkeypatch):
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
                "CODEX_SESSION_ID", "CODEX_CONVERSATION_ID"):
        monkeypatch.delenv(var, raising=False)
    assert resolve_connection() is None


def test_selection_text_lists_candidates_and_suggestion(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    reg = registry.load()
    registry.ensure_agent(reg, "backend", cwd="/repo")
    peers = [{"name": "backend", "user_id": "@s1:hive.local",
              "last_active": 10**12}]
    text = _selection_text(reg, Connection("codex", "thr_1"), peers)
    assert "identity selection required" in text
    assert "1. backend" in text
    assert "ACTIVE" in text
    assert "new: hive-agent-1" in text


def test_session_id_lookup_matches_sessions_file(tmp_path):
    (tmp_path / "4242.json").write_text('{"sessionId": "abc-123", "name": "aws-costs"}')
    (tmp_path / "1111.json").write_text('{"sessionId": "other", "name": "p90Goal"}')
    assert session_name_for_session_id("abc-123", tmp_path) == "aws-costs"


def test_session_id_lookup_none_when_unmatched(tmp_path):
    (tmp_path / "1111.json").write_text('{"sessionId": "other", "name": "p90Goal"}')
    (tmp_path / "junk.json").write_text("not json")
    assert session_name_for_session_id("abc-123", tmp_path) is None


def test_session_pid_prefers_nearest_sessions_file_owner(tmp_path):
    (tmp_path / "4242.json").write_text('{"name": "aws-costs"}')
    (tmp_path / "1111.json").write_text('{"name": "fork-parent"}')
    assert session_pid_for([9999, 4242, 1111, 1], tmp_path) == 4242


def test_session_pid_none_when_no_ancestor_owns_a_session(tmp_path):
    assert session_pid_for([9999, 8888], tmp_path) is None


def test_reads_rename_title_from_sessions_file(tmp_path):
    (tmp_path / "4242.json").write_text('{"pid": 4242, "name": "p90Goal"}')
    assert session_name_for_pid(4242, tmp_path) == "p90Goal"


def test_missing_file_returns_none(tmp_path):
    assert session_name_for_pid(4242, tmp_path) is None


def test_malformed_json_returns_none(tmp_path):
    (tmp_path / "4242.json").write_text("not json")
    assert session_name_for_pid(4242, tmp_path) is None


def test_empty_name_returns_none(tmp_path):
    (tmp_path / "4242.json").write_text('{"pid": 4242, "name": ""}')
    assert session_name_for_pid(4242, tmp_path) is None


def test_resolve_connection_prefers_sessions_file_over_env(monkeypatch):
    sdir = mcp_server._sessions_dir()
    (sdir / f"{os.getppid()}.json").write_text(
        '{"sessionId": "live-conversation-id", "name": "p90Goal"}')
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "stale-spawn-id")
    assert resolve_connection() == Connection("claude", "live-conversation-id")


def test_resolve_connection_env_when_sessions_file_lacks_id(monkeypatch):
    sdir = mcp_server._sessions_dir()
    (sdir / f"{os.getppid()}.json").write_text('{"name": "p90Goal"}')
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-id")
    assert resolve_connection() == Connection("claude", "env-id")


def test_session_id_for_pid_reads_sessions_file(tmp_path):
    (tmp_path / "4242.json").write_text('{"sessionId": "abc-123"}')
    assert session_id_for_pid(4242, tmp_path) == "abc-123"
    assert session_id_for_pid(1111, tmp_path) is None


def test_title_hint_prefers_zellij_tab_name(monkeypatch):
    monkeypatch.setattr(mcp_server, "zellij_tab_name", lambda: "p90Goal")
    assert mcp_server._title_hint(Connection("claude", "sid-x")) == "p90Goal"
    assert mcp_server._title_hint(Connection("codex", "thr_1")) == "p90Goal"


def test_title_hint_falls_back_to_claude_session_title(monkeypatch):
    monkeypatch.setattr(mcp_server, "zellij_tab_name", lambda: None)
    sdir = mcp_server._sessions_dir()
    (sdir / "4242.json").write_text('{"sessionId": "sid-x", "name": "renamed-title"}')
    assert mcp_server._title_hint(Connection("claude", "sid-x")) == "renamed-title"


def test_selection_text_marks_candidate_matching_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    monkeypatch.setattr(mcp_server, "zellij_tab_name", lambda: "p90Goal")
    reg = registry.load()
    registry.ensure_agent(reg, "p90Goal", cwd="/repo")
    registry.ensure_agent(reg, "dashboard", cwd="/repo2")
    text = _selection_text(reg, Connection("claude", "sid-x"), [])
    marked = [ln for ln in text.splitlines() if "matches this pane's tab/title" in ln]
    assert len(marked) == 1
    assert "p90Goal" in marked[0]


def test_codex_connection_never_discovers_claude_socket(hive, monkeypatch):
    def wrong_host(*args):
        raise AssertionError("Codex attempted Claude socket discovery")
    monkeypatch.setattr(mcp_server.socket_delivery, "discover", wrong_host)
    out = _connect(name="codex-agent", connection_id="codex:thread-1")
    assert "codex:thread-1" in out


def test_explicit_connection_replaces_ephemeral_selection(hive, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert "identity selection required" in _connect()
    assert mcp_server._state.connection.session_id.startswith("eph")
    out = _connect(name="codex-agent", connection_id="codex:thread-1")
    assert "codex:thread-1" in out
    assert "codex:thread-1" in registry.load()["connections"]


def test_codex_probe_failure_does_not_register_identity(hive, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(mcp_server.codex_delivery, "probe", AsyncMock(side_effect=OSError("wrong server")))
    out = _connect(name="codex-agent", connection_id="codex:thread-1", codex_socket="/tmp/c.sock")
    assert "setup failed" in out
    assert registry.agent_by_name(registry.load(), "codex-agent") is None
    assert mcp_server._state.matrix is None


def test_codex_push_reports_transport(hive, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(mcp_server.codex_delivery, "probe", AsyncMock())
    monkeypatch.setattr(mcp_server, "_delivery_loop", AsyncMock())
    out = _connect(name="codex-agent", connection_id="codex:thread-1", codex_socket="/tmp/c.sock")
    assert "Delivery: codex-app-server" in out
    assert mcp_server._state.matrix.client_name == "codex"
    assert mcp_server._state.matrix.delivery_mode == "codex-app-server"


def test_claude_rejects_codex_transport(hive):
    out = _connect(name="claude-agent", codex_socket="/tmp/c.sock")
    assert "requires connection_id=codex:" in out
