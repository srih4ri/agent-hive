#!/usr/bin/env python3
"""Claude Code adapter: wire claude-hive into Claude's config.

Most people should run `python3 scripts/setup.py`, which calls into this
module after bringing up the homeserver. Run it directly to redo just the
Claude wiring:
    python3 scripts/install_claude.py

Idempotent. Backs up each file to <name>.bak-hive before writing. Does:

1. ~/.claude/settings.json — add UserPromptSubmit / PostToolUse / Stop hooks
   running hook.py, so hive messages are injected without channels.
2. ~/.claude/skills/ — symlink each shared skill directory so the skills are
   available as slash commands in every project.
3. reports whether the claude-hive MCP server is registered, and prints the
   `claude mcp add` command when it is not.

Claude Code cannot do this itself (hook installation is permission-gated),
which is why it is a script for the human to run. Codex setup lives in
install_codex.py.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_CMD = f"python3 {REPO_ROOT / 'src' / 'claude_hive' / 'hook.py'}"
HOOK_EVENTS = ("UserPromptSubmit", "PostToolUse", "Stop")
SETTINGS = Path.home() / ".claude" / "settings.json"
USER_SKILLS = Path.home() / ".claude" / "skills"
CLAUDE_JSON = Path.home() / ".claude.json"


def _load(path: Path) -> dict:
    """Config as a dict — an absent or empty file reads as {}, so this works
    on a machine where Claude Code has never written settings."""
    try:
        return json.loads(path.read_text()) or {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-hive"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _hook_entry(event: str) -> dict:
    entry: dict = {"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 5}]}
    if event == "PostToolUse":
        entry["matcher"] = "*"
    return entry


def _has_hook(settings: dict, event: str) -> bool:
    return any(h.get("command") == HOOK_CMD
               for e in settings.get("hooks", {}).get(event, [])
               for h in e.get("hooks", []))


def hooks_installed() -> bool:
    settings = _load(SETTINGS)
    return all(_has_hook(settings, event) for event in HOOK_EVENTS)


def install_hooks() -> bool:
    """Add the delivery hooks. True when something changed."""
    settings = _load(SETTINGS)
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in HOOK_EVENTS:
        if _has_hook(settings, event):
            continue
        hooks.setdefault(event, []).append(_hook_entry(event))
        changed = True
    if changed:
        _save(SETTINGS, settings)
    return changed


def set_env(key: str, value: str) -> bool:
    """Set an env var for every Claude session. True when something changed.

    The hook and the MCP server both read the environment, so session-wide
    settings are the only place that reaches both.
    """
    settings = _load(SETTINGS)
    env = settings.setdefault("env", {})
    if env.get(key) == value:
        return False
    env[key] = value
    _save(SETTINGS, settings)
    return True


def _skill_dirs() -> list[Path]:
    return [d for d in sorted((REPO_ROOT / "skills").iterdir())
            if (d / "SKILL.md").exists()]


def skills_linked() -> int:
    """How many of this repo's skills are currently linked user-wide."""
    return sum(1 for d in _skill_dirs()
               if (USER_SKILLS / d.name).is_symlink()
               and (USER_SKILLS / d.name).resolve() == d.resolve())


def install_skills() -> list[str]:
    USER_SKILLS.mkdir(parents=True, exist_ok=True)
    linked = []
    for skill_dir in _skill_dirs():
        target = USER_SKILLS / skill_dir.name
        if target.is_symlink() and target.resolve() == skill_dir.resolve():
            continue
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(skill_dir)
        linked.append(skill_dir.name)
    return linked


def mcp_add_command() -> list[str]:
    return ["claude", "mcp", "add", "--scope", "user", "--transport", "stdio",
            "claude-hive", "--", "uv", "run", "--project", str(REPO_ROOT),
            "claude-hive-server"]


def mcp_add_hint() -> str:
    return "   " + " ".join(mcp_add_command())


def mcp_status() -> str:
    """"registered" or "not registered", read from Claude's own config."""
    cfg = _load(CLAUDE_JSON)
    if cfg.get("mcpServers", {}).get("claude-hive") is not None:
        return "registered"
    return "not registered"


def main() -> None:
    print("hooks in settings.json:", "installed" if install_hooks() else "already present")
    linked = install_skills()
    print("skills linked into ~/.claude/skills:",
          ", ".join(linked) if linked else "already current")
    status = mcp_status()
    print("claude-hive MCP server:", status)
    if status != "registered":
        print("  register it with:\n" + mcp_add_hint())
    print("restart Claude sessions (or /mcp reconnect) to pick up changes")


if __name__ == "__main__":
    sys.exit(main())
