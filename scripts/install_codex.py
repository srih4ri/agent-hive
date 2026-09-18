#!/usr/bin/env python3
"""Codex adapter: wire claude-hive into a Codex-driven project. Run by hand:
    python3 scripts/install_codex.py [target-project-dir]

Idempotent. For the target project (default: this repo) it does:

1. .agents/skills — symlink to this repo's shared skills/ directory, which
   is Codex's repository skill discovery path.
2. .codex/hooks.json — UserPromptSubmit / PostToolUse / Stop hooks running
   hook.py, the same pull-delivery model as the Claude adapter. Codex hooks
   only fire on lifecycle events; an idle session stays quiet until the
   hive-check heartbeat or the next turn.
3. Prints the `codex mcp add` registration command (global, one-time).

Codex app-server delivery can wake idle sessions; see
docs/codex-delivery.md for setup. Claude setup lives in
install_claude.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_CMD = f"python3 {REPO_ROOT / 'src' / 'claude_hive' / 'hook.py'}"


def _hook_entry(event: str) -> dict:
    entry: dict = {"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 5}]}
    if event == "PostToolUse":
        entry["matcher"] = "*"
    return entry


def install_skills_link(target: Path) -> str:
    agents = target / ".agents"
    agents.mkdir(exist_ok=True)
    link = agents / "skills"
    skills = REPO_ROOT / "skills"
    if link.resolve() == skills.resolve():
        return "already current"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(skills if target.resolve() != REPO_ROOT else Path("..") / "skills")
    return f"{link} -> {skills}"


def install_hooks(target: Path) -> str:
    hooks_file = target / ".codex" / "hooks.json"
    config = {
        "description": "claude-hive message delivery hooks",
        "hooks": {event: [_hook_entry(event)]
                  for event in ("UserPromptSubmit", "PostToolUse", "Stop")},
    }
    if hooks_file.exists() and json.loads(hooks_file.read_text()) == config:
        return "already current"
    hooks_file.parent.mkdir(exist_ok=True)
    hooks_file.write_text(json.dumps(config, indent=2) + "\n")
    return f"written to {hooks_file}"


def main() -> None:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else REPO_ROOT
    if not target.is_dir():
        sys.exit(f"not a directory: {target}")
    print("codex skills link:", install_skills_link(target))
    print("codex hooks:", install_hooks(target))
    print("register the MCP server once (global):")
    print(f"  codex mcp add claude-hive -- uv run --project {REPO_ROOT} claude-hive-server")
    print(f"For push delivery and idle wakeup: {REPO_ROOT / 'docs/codex-delivery.md'}")


if __name__ == "__main__":
    main()
