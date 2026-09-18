"""Local agent identity registry for claude-hive.

Identity is split into three concepts (AGENT_GUIDE.md):
  agent_id      immutable stable identity, generated once
  agent_name    human-friendly stable handle shown in peer lists
  connection_id ephemeral client session currently attached to the agent,
                e.g. "claude:<session_id>" or "codex:<thread_id>"

The registry persists agents and the connection->agent mapping in
agents.json under the hive state dir, so an agent keeps its identity across
reconnects, restarts, and host-tool changes. Session titles are display
hints only — never identity.

Stdlib-only, like hook.py: no third-party imports.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def registry_path() -> Path:
    base = Path(os.environ.get("CLAUDE_HIVE_MATRIX_DIR",
                               Path.home() / ".claude-hive" / "matrix"))
    return base / "agents.json"


def load() -> dict:
    try:
        data = json.loads(registry_path().read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("agents", {})
    data.setdefault("connections", {})
    return data


def save(reg: dict) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=1) + "\n")
    os.replace(tmp, path)


def new_agent_id() -> str:
    return "agent_" + os.urandom(8).hex()


def agent_by_name(reg: dict, name: str) -> dict | None:
    return reg["agents"].get(name)


def agent_by_id(reg: dict, agent_id: str) -> dict | None:
    for agent in reg["agents"].values():
        if agent.get("agent_id") == agent_id:
            return agent
    return None


def agent_for_connection(reg: dict, connection_id: str) -> dict | None:
    conn = reg["connections"].get(connection_id)
    if not conn:
        return None
    return agent_by_id(reg, conn.get("agent_id", ""))


def connections_for_agent(reg: dict, agent_id: str) -> list[str]:
    return [cid for cid, c in reg["connections"].items()
            if c.get("agent_id") == agent_id and c.get("status") == "active"]


def ensure_agent(reg: dict, name: str, cwd: str = "") -> dict:
    agent = reg["agents"].get(name)
    if agent is None:
        agent = {
            "agent_id": new_agent_id(),
            "name": name,
            "created_at": int(time.time()),
        }
        reg["agents"][name] = agent
    agent["last_cwd"] = cwd or agent.get("last_cwd", "")
    agent["last_seen"] = int(time.time())
    return agent


def bind_connection(reg: dict, connection_id: str, agent: dict, client: str) -> None:
    reg["connections"][connection_id] = {
        "agent_id": agent["agent_id"],
        "client": client,
        "status": "active",
    }
    agent["last_seen"] = int(time.time())


def release_connection(reg: dict, connection_id: str) -> None:
    conn = reg["connections"].get(connection_id)
    if conn:
        conn["status"] = "inactive"
    agent = agent_for_connection(reg, connection_id)
    if agent:
        agent["last_seen"] = int(time.time())


def candidates(reg: dict) -> list[dict]:
    """Known agents, most recently seen first — the identity-selection menu."""
    return sorted(reg["agents"].values(),
                  key=lambda a: a.get("last_seen", 0), reverse=True)


def suggest_name(reg: dict, hint: str | None = None) -> str:
    """A new-agent name that does not collide with the registry: the display
    hint (e.g. the session title) when free, else hive-agent-N."""
    if hint:
        hint = hint.strip()
    if hint and hint not in reg["agents"]:
        return hint
    n = 1
    while f"hive-agent-{n}" in reg["agents"]:
        n += 1
    return f"hive-agent-{n}"
