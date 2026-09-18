"""Shared test isolation.

The suite itself often runs inside a Claude Code session in a zellij pane;
connection/identity resolution reads ~/.claude/sessions and ZELLIJ_* env, so
every test gets a blank sessions dir and no zellij environment unless it
sets them explicitly.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _host_env_isolation(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSIONS_DIR",
                       str(tmp_path_factory.mktemp("sessions")))
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    monkeypatch.delenv("ZELLIJ_PANE_ID", raising=False)
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_TOKEN", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path_factory.mktemp("runtime")))
    for var in ("HIVE_HUMAN_MXID", "HIVE_MATRIX_URL", "HIVE_MATRIX_SERVER_NAME",
                "CLAUDE_HIVE_MATRIX_DIR", "HIVE_CODEX_SOCKET", "CODEX_THREAD_ID",
                "CODEX_SESSION_ID", "CODEX_CONVERSATION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
