"""Claude/Codex hooks: protect questions and route peer sends through guard.py.

These are cooperative guardrails, not a sandbox. Shell indirection and tools
which opt out of hooks cannot be comprehensively intercepted here.
"""
import fcntl
import hashlib
import json
import os
import re
import sys

import guard

QUESTIONS = {"AskUserQuestion", "request_user_input"}
LEGACY = {"tmux_send_keys", "tmux_send_text", "tmux_run"}
CLI = str(__file__).replace("guard_hook.py", "guard.py")
GUIDANCE = (
    "Peer input is protected. Use tmux_message after reconnecting the tmux MCP. "
    "Until then write your message to a temporary UTF-8 file, then run: "
    f"/usr/bin/python3 '{CLI}' TARGET --file /tmp/message.txt. "
    "The guard automatically waits for typing/drafts/questions to clear. "
    "NOT SENT means nothing was queued. Never answer a human question or "
    "bypass this guard with raw tmux commands."
)


def is_agent(target):
    return bool(guard.snapshot(target).kind)


def question_state(token, pending):
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return  # Desktop/non-tmux clients have no pane to mark.
    s = guard.snapshot(pane)
    guard.STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256((s.socket + s.pane).encode()).hexdigest()
    with (guard.STATE_DIR / (key + ".lock")).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = guard.snapshot(pane).question
        # A completion for one question must not clear a newer question.
        if pending or current == token:
            guard.tmux("set-option", "-p", "-t", pane, "@mcp_human_question", token if pending else "")


def handle(payload):
    event = payload.get("hook_event_name", "")
    tool = payload.get("tool_name", "").split(".")[-1]
    if event == "SessionStart" and os.environ.get("TMUX_PANE"):
        pane = os.environ["TMUX_PANE"]
        current = guard.snapshot(pane).question
        if current:
            question_state(current, False)
    if tool in QUESTIONS:
        token = tool + ":" + payload.get("tool_use_id", "unknown")
        if event == "PreToolUse":
            question_state(token, True)
        elif event in {"PostToolUse", "PostToolUseFailure"}:
            question_state(token, False)
    if event != "PreToolUse":
        return {}
    arguments = payload.get("tool_input", {})
    if not isinstance(arguments, dict):
        arguments = {"code": str(arguments)}
    denied = False
    if tool.split("__")[-1] in LEGACY:
        target = arguments.get("target", "")
        denied = not target or is_agent(target)
    elif tool in {"Bash", "exec", "exec_command", "shell", "shell_command"}:
        code = json.dumps(arguments)
        # Catch direct CLI writes (including subprocess argv), plus legacy MCP
        # writes embedded in code mode. Read-only tmux commands remain usable.
        raw_tmux = re.search(r"\btmux(?:\s|[\\\"'])", code)
        mutation = re.search(r"\b(send-keys|send-prefix|paste-buffer|pipe-pane|send|pasteb)\b", code)
        nested = any("tmux__" + name in code for name in LEGACY)
        denied = bool((raw_tmux and mutation) or nested)
    if denied:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                "permissionDecision": "deny", "permissionDecisionReason": GUIDANCE}}
    return {}


if __name__ == "__main__":
    payload = {}
    try:
        payload = json.load(sys.stdin)
        result = handle(payload)
    except Exception as exc:
        # Fail closed for a pre-tool routing error; never emit malformed JSON.
        result = {"systemMessage": f"tmux input guard could not update state: {exc}"}
        if payload.get("hook_event_name") == "PreToolUse":
            result = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                  "permissionDecision": "deny",
                  "permissionDecisionReason": f"tmux input guard could not check state: {exc}"}}
    print(json.dumps(result))
