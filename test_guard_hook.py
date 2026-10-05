import guard_hook
import json
import pytest
from types import SimpleNamespace


def test_raw_shell_send_is_denied():
    result = guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                "tool_input": {"command": "tmux send-keys -t %7 Enter"}})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_readonly_tmux_is_allowed():
    assert guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": "tmux capture-pane -p -t %7"}}) == {}


def test_guard_cli_is_allowed():
    assert guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": "python3 /x/guard.py %7 --file /tmp/message.txt"}}) == {}


def test_legacy_mcp_send_to_agent_is_denied(monkeypatch):
    monkeypatch.setattr(guard_hook, "is_agent", lambda target: True)
    result = guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "mcp__tmux__tmux_send_keys",
                               "tool_input": {"keys": "Enter", "target": "%7"}})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_shell_pane_control_is_not_blocked(monkeypatch):
    monkeypatch.setattr(guard_hook, "is_agent", lambda target: False)
    assert guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "mcp__tmux__tmux_send_keys",
                              "tool_input": {"keys": "C-c", "target": "%3"}}) == {}


def test_question_lifecycle_uses_matching_tool_id(monkeypatch, tmp_path):
    changes = []
    transcript = tmp_path / "rollout.jsonl"
    transcript.touch()
    monkeypatch.setattr(guard_hook, "owns_pane", lambda payload: True)
    monkeypatch.setattr(guard_hook, "start_result_watch", lambda *args: None)
    monkeypatch.setattr(guard_hook, "question_state", lambda *args: changes.append(args))
    for event in ("PreToolUse", "PostToolUse"):
        assert guard_hook.handle({"hook_event_name": event, "tool_name": "request_user_input",
                                  "tool_use_id": "call-1", "transcript_path": str(transcript)}) == {}
    assert changes == [("request_user_input:call-1", True), ("request_user_input:call-1", False)]


def test_code_mode_raw_send_is_denied():
    result = guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "exec",
                               "tool_input": {"code": 'await tools.exec_command({cmd:"tmux send-keys -t %7 Enter"})'}})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_foreign_session_cannot_mark_inherited_tmux_pane(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(guard_hook, "pane_session", lambda pane: "sam-session", raising=False)
    changes = []
    monkeypatch.setattr(guard_hook, "question_state", lambda *args: changes.append(args))
    guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "request_user_input",
                       "tool_use_id": "foreign", "session_id": "vault-session"})
    assert not changes


def test_foreign_session_start_cannot_clear_another_question(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(guard_hook, "pane_session", lambda pane: "sam-session", raising=False)
    monkeypatch.setattr(guard_hook.guard, "snapshot", lambda pane: SimpleNamespace(question="request_user_input:active"))
    changes = []
    monkeypatch.setattr(guard_hook, "question_state", lambda *args: changes.append(args))
    guard_hook.handle({"hook_event_name": "SessionStart", "session_id": "vault-session"})
    assert not changes


@pytest.mark.parametrize("output", [
    "request_user_input is unavailable in Default mode",
    {"answers": {"choice": {"answers": ["Original timing"]}}},
])
def test_exact_codex_result_is_completion_even_on_failure(output):
    line = json.dumps({"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "call-q", "output": output}})
    assert guard_hook.is_question_result(line, "call-q")
    assert not guard_hook.is_question_result(line, "newer-call")


def test_question_call_itself_is_not_completion():
    line = json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "call_id": "call-q", "name": "request_user_input"}})
    assert not guard_hook.is_question_result(line, "call-q")


def test_failed_call_clears_without_post_tool_hook(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps({"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "failed", "output": "request_user_input is unavailable in Default mode"}}) + "\n")
    monkeypatch.setattr(guard_hook.guard, "snapshot", lambda pane: SimpleNamespace(question="request_user_input:failed"))
    changes = []
    monkeypatch.setattr(guard_hook, "question_state", lambda *args: changes.append(args))
    guard_hook.watch_result("%7", "request_user_input:failed", str(transcript), 0)
    assert changes == [("request_user_input:failed", False)]


def test_session_binding_ignores_shared_daemon_and_other_terminals():
    own = "12345678-1234-1234-1234-123456789abc"
    other = "00000000-0000-0000-0000-000000000001"
    table = f"100 1 zsh\n101 100 codex resume {own}\n200 1 codex app-server --managed-daemon\n201 200 python3 guard_hook.py\n300 1 codex resume {other}\n"
    assert guard_hook.session_from_processes(table, 100) == own
    assert guard_hook.session_from_processes(table, 200) == ""


def test_unidentified_fresh_tui_does_not_claim_a_session():
    assert guard_hook.session_from_processes("100 1 zsh\n101 100 codex\n", 100) == ""


@pytest.mark.parametrize("tool", ["tmux_send_keys", "tmux_send_text", "tmux_run"])
def test_legacy_opencode_writes_are_denied(monkeypatch, tool):
    def tmux(*args):
        if args[0] == "display-message":
            return "%7|qwen-agent|opencode|0|5|||0|/tmp/test-socket\n"
        if args[0] == "list-clients":
            return ""
        return "Unknown OpenCode UI"
    monkeypatch.setattr(guard_hook.guard, "tmux", tmux)
    result = guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "mcp__tmux__" + tool,
                               "tool_input": {"target": "%7", "keys": "Enter"}})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_opencode_launch_session_does_not_claim_active_conversation():
    # OpenCode can change sessions without changing process argv. Neither its
    # ses_ identifier nor a UUID-shaped argument proves the selected TUI tab.
    for session in ("ses_example", "12345678-1234-1234-1234-123456789abc"):
        table = f"100 1 zsh\n101 100 opencode --session {session}\n"
        assert guard_hook.session_from_processes(table, 100) == ""


def test_opencode_queue_refuses_unverified_or_changed_conversation(monkeypatch, tmp_path):
    import delivery
    from test_guard import opencode_screen
    from dataclasses import replace
    monkeypatch.setattr(delivery, "QUEUE_DIR", tmp_path)
    launched = []
    monkeypatch.setattr(delivery, "launch", lambda path: launched.append(path))
    def tmux(*args):
        if "#{pid}|#{pane_pid}" in args:
            return "10|100\n"
        raise AssertionError("No launch-argv inference can establish the active OpenCode tab")
    monkeypatch.setattr(guard_hook.guard, "tmux", tmux)
    for title in ("Conversation A", "Conversation B"):
        state = replace(opencode_screen(), screen=title + "\n" + opencode_screen().screen)
        monkeypatch.setattr(guard_hook.guard, "snapshot", lambda target: state)
        with pytest.raises(ValueError, match="identity"):
            delivery.enqueue("%7", "peer", wait_seconds=0)
    assert not launched
    assert not list(tmp_path.glob("*.json"))


def test_foreign_hook_inheriting_opencode_pane_does_not_latch_or_deny_question(monkeypatch):
    from test_guard import opencode_screen
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(guard_hook.guard, "snapshot", lambda target: opencode_screen())
    changes = []
    monkeypatch.setattr(guard_hook, "question_state", lambda *args: changes.append(args))
    assert guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                              "tool_use_id": "foreign", "session_id": "claude-session"}) == {}
    assert not changes
