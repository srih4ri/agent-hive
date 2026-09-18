# claude-hive — Codex notes

Read `AGENT_GUIDE.md` for the shared agent guidance (identity model, tools,
delivery, bulletin etiquette). Codex-specific details only below.

- Setup: `python3 scripts/setup.py --codex` (homeserver + observer account +
  skills symlink at `.agents/skills` + hooks in `.codex/hooks.json`), then
  register the MCP server once: `codex mcp add claude-hive -- uv run
  --project <this repo> claude-hive-server`. `scripts/install_codex.py
  [project-dir]` redoes just the Codex wiring for another project.
- Skills are discovered under `.agents/skills` and invoked via `/skills` or
  `$skill-name`.
- Codex exposes no guaranteed session-id env var; `hive_connect` accepts an
  explicit `connection_id` (e.g. `codex:thr_123`) and falls back to
  `CODEX_THREAD_ID`-style vars when present. Hook payload `session_id` /
  `thread_id` keys are both understood.
- Codex push delivery uses the owning app-server's Unix control socket:
  pass `connection_id="codex:<real-thread-id>"` and `codex_socket`, or export
  `HIVE_CODEX_SOCKET` in the MCP server environment. The UI must connect to
  the same app-server. See `docs/codex-delivery.md` for setup. Without this,
  hook delivery needs lifecycle events and cannot wake an idle session.
- The `.agents/skills` symlink is written relative when the target project
  is this repo and absolute otherwise, so a clone stays self-contained.
- Development: `uv run pytest`, `uv run ruff check .`. The code carries no
  inline comments by convention — the rationale (Matrix quirks, delivery
  invariants, host gotchas) lives in the *Implementation notes* section of
  `CLAUDE.md`. Read it before changing delivery, room creation or session
  resolution, and update it alongside the behaviour it describes.
