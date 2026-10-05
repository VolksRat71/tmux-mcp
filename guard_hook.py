"""Claude/Codex hooks: protect questions and route peer sends through guard.py.

These are cooperative guardrails, not a sandbox. Shell indirection and tools
which opt out of hooks cannot be comprehensively intercepted here.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

import guard

QUESTIONS = {"AskUserQuestion", "request_user_input"}
LEGACY = {"tmux_send_keys", "tmux_send_text", "tmux_run"}
CLI = str(__file__).replace("guard_hook.py", "guard.py")
GUIDANCE = (
    "Peer input is protected. Use tmux_message after reconnecting the tmux MCP. "
    "Until then write your message to a temporary UTF-8 file, then run: "
    f"/usr/bin/python3 '{CLI}' TARGET --file /tmp/message.txt --queue. "
    "The guard automatically waits for typing/drafts/questions to clear. "
    "QUEUED means delivery will continue automatically; do not resend. Never answer a human question or "
    "bypass this guard with raw tmux commands."
)


def is_agent(target):
    return bool(guard.snapshot(target).kind)


class UnverifiedSessionIdentity(ValueError):
    """The pane cannot be bound to its currently selected conversation."""


def pane_session(pane):
    """Identify the resumed TUI under this pane, not the shared app-server.

    A managed Codex daemon can export the same TMUX_PANE to unrelated
    sessions. Never use that variable alone as evidence of session ownership.
    Unidentifiable/fresh sessions retain screen protection, without a latch.
    """
    if guard.snapshot(pane).kind == "opencode":
        # OpenCode --session is only an initial selection. Its TUI can switch
        # tabs in the same process; neither argv nor displayed titles bind a
        # queued message to its intended conversation. Refuse queue creation.
        raise UnverifiedSessionIdentity(guard.OPENCODE_IDENTITY_REASON)
    root = int(guard.tmux("display-message", "-p", "-t", pane, "#{pane_pid}").strip())
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,command="],
                            capture_output=True, text=True, timeout=3, check=True)
    return session_from_processes(result.stdout, root)


def session_from_processes(table, root):
    processes = {}
    for line in table.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            processes[int(parts[0])] = (int(parts[1]), parts[2])
    descendants = {root}
    while True:
        expanded = descendants | {pid for pid, (parent, _) in processes.items() if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    sessions = set()
    for pid in descendants:
        command = processes.get(pid, (0, ""))[1]
        try:
            args = shlex.split(command)
        except ValueError:
            continue
        if not args or Path(args[0]).name not in {"codex", "claude"}:
            continue
        for i, arg in enumerate(args[:-1]):
            if arg in {"resume", "--resume", "-r"} and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", args[i+1]):
                sessions.add(args[i+1])
    return next(iter(sessions)) if len(sessions) == 1 else ""


def owns_pane(payload):
    pane = os.environ.get("TMUX_PANE")
    session = payload.get("session_id")
    try:
        return bool(pane and session and pane_session(pane) == session)
    except UnverifiedSessionIdentity:
        return False  # An inherited OpenCode pane is not this hook's session.


def is_question_result(line, call_id):
    try:
        item = json.loads(line)
        payload = item.get("payload", {})
        return (item.get("type") == "response_item" and isinstance(payload, dict)
                and payload.get("type") == "function_call_output"
                and payload.get("call_id") == call_id)
    except (ValueError, AttributeError):
        return False


def watch_result(pane, token, transcript, offset):
    """Clear only on the exact terminal tool result, including error results.

    No inactivity timeout releases a question. A replaced/cleared marker
    ends this observer. Unknown transcript records do not release input.
    """
    call_id = token.split(":", 1)[1]
    with open(transcript, encoding="utf-8") as stream:
        stream.seek(offset)
        pending = ""
        while guard.snapshot(pane).question == token:
            # Codex history files may be truncated during history maintenance.
            if os.fstat(stream.fileno()).st_size < stream.tell():
                stream.seek(0)
                pending = ""
            chunk = stream.read()
            pending += chunk
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                if is_question_result(line, call_id):
                    question_state(token, False)
                    return
            time.sleep(.25)


def start_result_watch(token, transcript, offset):
    guard.STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Keep errors visible without keeping the hook's stdout pipe open. This
    # records diagnostics only, never question/answer content.
    with (guard.STATE_DIR / "question-observer.log").open("a") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watch",
                          os.environ["TMUX_PANE"], token, transcript, str(offset)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)


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
    owned = owns_pane(payload) if event == "SessionStart" or tool in QUESTIONS else False
    if event == "SessionStart" and owned:
        pane = os.environ["TMUX_PANE"]
        current = guard.snapshot(pane).question
        if current:
            question_state(current, False)
    if tool in QUESTIONS and owned:
        token = tool + ":" + payload.get("tool_use_id", "unknown")
        if event == "PreToolUse":
            transcript = payload.get("transcript_path")
            # Capture the offset before returning to the tool: its result may
            # arrive before the detached observer has started.
            if tool == "request_user_input" and not (transcript and Path(transcript).is_file()):
                return {"systemMessage": "tmux question latch skipped: no Codex transcript; screen checks still apply."}
            offset = Path(transcript).stat().st_size if tool == "request_user_input" else 0
            question_state(token, True)
            if tool == "request_user_input":
                try:
                    start_result_watch(token, transcript, offset)
                except OSError:
                    question_state(token, False)  # The hook will deny this tool call.
                    raise
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
    if len(sys.argv) > 1 and sys.argv[1] == "--watch":
        pane, token, transcript, offset = sys.argv[2:]
        os.environ["TMUX_PANE"] = pane
        watch_result(pane, token, transcript, int(offset))
        raise SystemExit(0)
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
