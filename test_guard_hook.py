import guard_hook


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


def test_question_lifecycle_uses_matching_tool_id(monkeypatch):
    changes = []
    monkeypatch.setattr(guard_hook, "question_state", lambda *args: changes.append(args))
    for event in ("PreToolUse", "PostToolUse"):
        assert guard_hook.handle({"hook_event_name": event, "tool_name": "request_user_input",
                                  "tool_use_id": "call-1"}) == {}
    assert changes == [("request_user_input:call-1", True), ("request_user_input:call-1", False)]


def test_code_mode_raw_send_is_denied():
    result = guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "exec",
                               "tool_input": {"code": 'await tools.exec_command({cmd:"tmux send-keys -t %7 Enter"})'}})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
