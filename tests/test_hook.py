"""Pull-delivery hook tests: matrix account lookup and per-event output shapes."""
from __future__ import annotations

import json
import os

from claude_hive import hook

PID = os.getpid()

MESSAGES = [
    {"id": 7, "from": "dashboards", "body": "p90 is on panel 3", "sent_at": 1.0},
]


def test_matrix_account_reads_by_session_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    (tmp_path / "by-session").mkdir()
    (tmp_path / "by-session" / "abc-123.json").write_text(
        '{"user_id": "@sabc:hive.local", "access_token": "tok", "name": "system-design"}'
    )
    account = hook.matrix_account("abc-123")
    assert account["user_id"] == "@sabc:hive.local"
    assert account["name"] == "system-design"


def test_matrix_account_none_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    assert hook.matrix_account("abc-123") is None
    assert hook.matrix_account(None) is None


def test_session_id_from_payload_key_variants(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert hook.session_id_from({"session_id": "a"}) == "a"
    assert hook.session_id_from({"sessionId": "b"}) == "b"
    assert hook.session_id_from({"thread_id": "thr_1"}) == "thr_1"


def test_session_id_from_env_fallbacks(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thr_env")
    assert hook.session_id_from({}) == "thr_env"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-env")
    assert hook.session_id_from({}) == "claude-env"
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert hook.session_id_from({}) is None


def test_ancestor_pids_walks_to_init():
    pids = hook.ancestor_pids(PID)
    assert pids[0] == PID
    assert os.getppid() in pids
    assert 1 not in pids


def test_user_prompt_submit_outputs_plain_context():
    out = hook.output_for("UserPromptSubmit", hook.render(MESSAGES))
    assert out is not None
    assert '<hive-message from="dashboards" id="7">' in out
    assert "p90 is on panel 3" in out


def test_post_tool_use_outputs_additional_context_json():
    out = hook.output_for("PostToolUse", hook.render(MESSAGES))
    parsed = json.loads(out)
    inner = parsed["hookSpecificOutput"]
    assert inner["hookEventName"] == "PostToolUse"
    assert "p90 is on panel 3" in inner["additionalContext"]


def test_stop_blocks_with_messages_in_reason():
    out = hook.output_for("Stop", hook.render(MESSAGES))
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert "p90 is on panel 3" in parsed["reason"]


def test_unknown_event_outputs_nothing():
    assert hook.output_for("SessionStart", hook.render(MESSAGES)) is None


SYNC_PAYLOAD = {
    "next_batch": "s99",
    "rooms": {"join": {"!room:hive.local": {"timeline": {"events": [
        {"type": "m.room.message", "sender": "@sabc:hive.local", "event_id": "$evt111222333",
         "content": {"msgtype": "m.text", "body": "hello", "hive_from": "alert-duty"}},
        {"type": "m.room.message", "sender": "@me:hive.local", "event_id": "$mine",
         "content": {"msgtype": "m.text", "body": "my own echo"}},
        {"type": "m.room.member", "sender": "@sabc:hive.local", "event_id": "$member"},
    ]}}}},
}


def test_messages_from_sync_extracts_others_messages_only():
    msgs = hook.messages_from_sync(SYNC_PAYLOAD, "@me:hive.local")
    assert len(msgs) == 1
    assert msgs[0]["from"] == "alert-duty"
    assert msgs[0]["body"] == "hello"


def test_messages_from_sync_skips_presence_notices():
    payload = {"rooms": {"join": {"!hive": {"timeline": {"events": [
        {"type": "m.room.message", "sender": "@sabc:hive.local", "event_id": "$n",
         "content": {"msgtype": "m.notice", "body": "● p90Goal connected"}}]}}}}}
    assert hook.messages_from_sync(payload, "@me:hive.local") == []


def test_messages_from_sync_flags_human_sender(monkeypatch):
    monkeypatch.setattr(hook, "HUMAN_MXID", "@operator:hive.local")
    payload = {"rooms": {"join": {"!b": {"timeline": {"events": [
        {"type": "m.room.message", "sender": "@operator:hive.local", "event_id": "$h",
         "content": {"msgtype": "m.text", "body": "post your daily update"}}]}}}}}
    msgs = hook.messages_from_sync(payload, "@me:hive.local")
    assert msgs[0]["human"] is True
    assert hook.messages_from_sync(SYNC_PAYLOAD, "@me:hive.local")[0]["human"] is False


def test_no_sender_is_human_when_no_operator_configured(monkeypatch):
    """With no operator configured — the shipped default — nobody is tagged
    origin=human, so an unset HIVE_HUMAN_MXID cannot promote a peer to
    speaking as the user."""
    monkeypatch.setattr(hook, "HUMAN_MXID", "")
    payload = {"rooms": {"join": {"!b": {"timeline": {"events": [
        {"type": "m.room.message", "sender": "@anyone:hive.local", "event_id": "$h",
         "content": {"msgtype": "m.text", "body": "hi"}}]}}}}}
    assert hook.messages_from_sync(payload, "@me:hive.local")[0]["human"] is False


def test_render_introduces_human_messages(monkeypatch):
    monkeypatch.setattr(hook, "HUMAN_MXID", "@operator:hive.local")
    out = hook.render([{"id": "$h", "from": "operator", "body": "daily update please",
                        "human": True}])
    assert '<hive-message from="operator" origin=human id="$h">' in out
    assert "human operator" in out
    assert "instructions from your user" in out


def test_render_plain_for_peer_messages():
    out = hook.render(MESSAGES)
    assert "origin=human" not in out
    assert "human operator" not in out


def test_messages_from_sync_carries_room_and_event():
    msg = hook.messages_from_sync(SYNC_PAYLOAD, "@me:hive.local")[0]
    assert msg["room"] == "!room:hive.local"
    assert msg["event"] == "$evt111222333"


def test_render_includes_room_for_reply_routing():
    out = hook.render([{"id": "$e", "from": "p90Goal", "body": "hi",
                        "room": "!pair:hive.local", "human": False}])
    assert '<hive-message from="p90Goal" room="!pair:hive.local" id="$e">' in out
    assert "room=<the block's room attribute>" in out


def test_code_version_reads_branch_ref(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("abc1234def5678\n")
    assert hook.code_version(tmp_path) == "abc1234"


def test_code_version_reads_packed_ref(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled\ncafe1234beef5678 refs/heads/main\n")
    assert hook.code_version(tmp_path) == "cafe123"


def test_code_version_unknown_without_git(tmp_path):
    assert hook.code_version(tmp_path) == "unknown"


def test_messages_from_sync_falls_back_to_sender_localpart():
    payload = {"rooms": {"join": {"!r": {"timeline": {"events": [
        {"type": "m.room.message", "sender": "@sabc:hive.local", "event_id": "$e",
         "content": {"body": "hi"}}]}}}}}
    assert hook.messages_from_sync(payload, "@me:hive.local")[0]["from"] == "sabc"


def test_log_line_appends_timestamped_lines(tmp_path, monkeypatch):
    logf = tmp_path / "hook.log"
    monkeypatch.setenv("HIVE_HOOK_LOG", str(logf))
    hook.log_line("PostToolUse: name=x messages=1")
    hook.log_line("Stop: name=x messages=0")
    lines = logf.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("PostToolUse: name=x messages=1")
    assert f"pid={os.getpid()}" in lines[0]


def test_log_line_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_HOOK_LOG", "off")
    hook.log_line("anything")
    assert list(tmp_path.iterdir()) == []


def test_log_line_rotates_at_size_cap(tmp_path, monkeypatch):
    logf = tmp_path / "hook.log"
    monkeypatch.setenv("HIVE_HOOK_LOG", str(logf))
    monkeypatch.setattr(hook, "LOG_MAX_BYTES", 10)
    hook.log_line("first line, longer than the cap")
    hook.log_line("second")
    assert (tmp_path / "hook.log.1").read_text().endswith("first line, longer than the cap\n")
    assert logf.read_text().endswith("second\n")


class _Run:
    def __init__(self, stdout):
        self.stdout = stdout


def _zellij_env(monkeypatch, session="loyal-crab", pane="4"):
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", session)
    monkeypatch.setenv("ZELLIJ_PANE_ID", pane)


def test_zellij_pane_key_from_env(monkeypatch):
    _zellij_env(monkeypatch)
    assert hook.zellij_pane_key() == "loyal-crab~4"


def test_zellij_pane_key_sanitizes_session_name(monkeypatch):
    _zellij_env(monkeypatch, session="my session/2")
    assert hook.zellij_pane_key() == "my_session_2~4"


def test_zellij_pane_key_none_outside_zellij():
    assert hook.zellij_pane_key() is None


def test_zellij_tab_name_matches_own_pane_not_plugins(monkeypatch):
    _zellij_env(monkeypatch)
    panes = [{"id": 4, "is_plugin": True, "tab_name": "wrong"},
             {"id": 3, "is_plugin": False, "tab_name": "other"},
             {"id": 4, "is_plugin": False, "tab_name": "p90Goal"}]
    monkeypatch.setattr(hook.subprocess, "run",
                        lambda *a, **k: _Run(json.dumps(panes)))
    assert hook.zellij_tab_name() == "p90Goal"


def test_zellij_tab_name_rejects_generic_default(monkeypatch):
    _zellij_env(monkeypatch)
    panes = [{"id": 4, "is_plugin": False, "tab_name": "Tab #6"}]
    monkeypatch.setattr(hook.subprocess, "run",
                        lambda *a, **k: _Run(json.dumps(panes)))
    assert hook.zellij_tab_name() is None


def test_zellij_tab_name_none_outside_zellij(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not shell out without ZELLIJ_PANE_ID")
    monkeypatch.setattr(hook.subprocess, "run", _boom)
    assert hook.zellij_tab_name() is None


def test_zellij_tab_name_none_when_binary_missing(monkeypatch):
    _zellij_env(monkeypatch)
    def _raise(*a, **k):
        raise FileNotFoundError("zellij")
    monkeypatch.setattr(hook.subprocess, "run", _raise)
    assert hook.zellij_tab_name() is None


def _write_account(tmp_path, session_id, user="@sowner:hive.local"):
    (tmp_path / "by-session").mkdir(exist_ok=True)
    (tmp_path / "by-session" / f"{session_id}.json").write_text(json.dumps(
        {"user_id": user, "access_token": "tok", "name": "p90Goal"}))


def test_resolve_account_direct_by_session_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    _write_account(tmp_path, "sid-1")
    sid, account = hook.resolve_account("sid-1")
    assert sid == "sid-1"
    assert account["user_id"] == "@sowner:hive.local"


def test_resolve_account_falls_back_to_pane_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    _zellij_env(monkeypatch)
    _write_account(tmp_path, "registered-id")
    (tmp_path / "by-pane").mkdir()
    (tmp_path / "by-pane" / "loyal-crab~4.json").write_text(
        '{"session_id": "registered-id"}')
    monkeypatch.setenv("HIVE_HOOK_LOG", "off")
    sid, account = hook.resolve_account("resumed-id")
    assert sid == "registered-id"
    assert account["user_id"] == "@sowner:hive.local"


def test_resolve_account_none_on_dangling_pane_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    _zellij_env(monkeypatch)
    (tmp_path / "by-pane").mkdir()
    (tmp_path / "by-pane" / "loyal-crab~4.json").write_text(
        '{"session_id": "gone-id"}')
    sid, account = hook.resolve_account("resumed-id")
    assert sid == "resumed-id"
    assert account is None


def test_resolve_account_none_without_alias_or_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    sid, account = hook.resolve_account("sid-x")
    assert (sid, account) == ("sid-x", None)
