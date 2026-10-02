"""Shared automatic input guard for Claude/Codex panes (stdlib only).

Unknown UI states fail closed. This reduces input races; it is not a keyboard
interceptor. Raw tmux writes bypass this module and must not be used by agents.
"""
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

STATE_DIR = Path(tempfile.gettempdir()) / f"tmux-mcp-guard-{os.getuid()}"
QUIET_SECONDS = 3.0
STABLE_SECONDS = 1.0
POLL_SECONDS = .25


def tmux(*args, input_text=None):
    socket = os.environ.get("TMUX_MCP_SOCKET", "")
    base = ["tmux", "-L", socket] if socket else ["tmux"]
    p = subprocess.run(base + list(args), input=input_text, text=True,
                       capture_output=True, timeout=5)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or "tmux failed")
    return p.stdout


@dataclass(frozen=True)
class Snapshot:
    pane: str
    kind: str
    foreground: str
    screen: str
    cursor_y: int
    cursor_x: int
    activity: float
    question: str
    in_mode: bool = False
    socket: str = ""


def snapshot(target):
    fmt = "|".join("#{" + f + "}" for f in (
        "pane_id", "session_name", "pane_current_command", "cursor_y",
        "cursor_x", "@mcp_agent_kind", "@mcp_human_question", "pane_in_mode", "socket_path"))
    pane, session, fg, cy, cx, registered, question, mode, socket = tmux(
        "display-message", "-p", "-t", target, fmt).strip().split("|")
    kind = registered
    if not kind:
        if "codex" in (session + " " + fg).lower():
            kind = "codex"
        elif "claude" in (session + " " + fg).lower() or re.fullmatch(r"\d+\.\d+\.\d+", fg):
            kind = "claude"
    activity = 0.0
    if kind:
        for line in tmux("list-clients", "-F", "#{pane_id}|#{client_activity}").splitlines():
            client_pane, timestamp = line.split("|")
            if client_pane == pane:
                activity = max(activity, float(timestamp))
    return Snapshot(pane, kind, fg, tmux("capture-pane", "-p", "-t", pane),
                    int(cy), int(cx), activity, question, mode == "1", socket)


def block_reason(s, now=None):
    if not s.kind:
        return ""
    now = time.time() if now is None else now
    if s.in_mode:
        return "pane is in copy mode"
    if s.question:
        return "human question pending: " + s.question
    if now - s.activity < QUIET_SECONDS:
        return "recent human typing/activity"
    if not (s.foreground in {"claude", "codex", "node"} or
            (s.kind == "claude" and re.fullmatch(r"\d+\.\d+\.\d+", s.foreground))):
        return "agent is no longer the foreground application"
    lines = s.screen.splitlines()
    if not 0 <= s.cursor_y < len(lines):
        return "unrecognized input position"
    # Selection prompts are not message composers, even if they use › too.
    tail = "\n".join(lines[max(0, s.cursor_y - 12):]).lower()
    if re.search(r"enter to (select|confirm)|tab to (navigate|select)|esc to cancel|allow once|yes, proceed|1\.\s+yes|would you like to (proceed|run)|submit answer", tail):
        return "question/permission dialog"
    if re.search(r"esc to interrupt|escape to interrupt|ctrl\+c to interrupt", tail):
        return "agent is working"
    marker = "›" if s.kind == "codex" else "❯"
    # The actual cursor must be on the normal composer at column two.
    # Wrapped drafts, multiline inputs and moved cursors remain protected.
    line = lines[s.cursor_y].lstrip()
    if not line.startswith(marker):
        return "unrecognized composer or multiline draft"
    text = line[len(marker):].strip()
    placeholders = {"", "Ask Codex to do anything"} if s.kind == "codex" else {""}
    if text not in placeholders or s.cursor_x != 2:
        return "unfinished human draft or selection dialog"
    # Moving the cursor back to an empty first line must not hide the rest
    # of a multiline draft. Stop only at a recognized composer boundary.
    for below in lines[s.cursor_y + 1:]:
        stripped = below.strip()
        if not stripped:
            continue
        if stripped.startswith(("─", "GPT-", "gpt-", "← for agents", "? for shortcuts")) or re.match(r"\d+% context left", stripped):
            break
        return "multiline draft or unrecognized composer footer"
    # Only the latest visible assistant response counts, not old chat questions.
    preceding = lines[:s.cursor_y]
    starts = [i for i, line in enumerate(preceding)
              if re.match(r"^[•⏺]\s", line)]
    latest = "\n".join(preceding[starts[-1]:] if starts else preceding)
    if "?" in latest or re.search(r"waiting (on|for) (you|nate)|need (your|nate.s) (answer|decision|approval)|questions? (for nate|remain|pending)", latest, re.I):
        return "possible unanswered human question"
    return ""


def deliver(pane, keys, literal, enter):
    if literal:
        # Bracketed paste preserves multiline messages in supporting TUIs.
        name = "mcp-" + uuid.uuid4().hex
        tmux("load-buffer", "-b", name, "-", input_text=keys[0])
        try:
            args = ["paste-buffer", "-p", "-d", "-b", name, "-t", pane]
            if enter:
                args += [";", "send-keys", "-t", pane, "Enter"]
            tmux(*args)
        finally:
            try:
                tmux("delete-buffer", "-b", name)
            except RuntimeError:
                pass  # paste-buffer -d already removed it
    else:
        args = ["send-keys", "-t", pane, *keys]
        if enter:
            args += [";", "send-keys", "-t", pane, "Enter"]
        tmux(*args)
    return ""


def send(target, keys, literal=False, enter=False, wait_seconds=25):
    """Wait for safe input and send once. Empty result means delivered.

    A timeout does not retain a message or send it later. All MCP processes
    use the same per-pane file lock and recent-message fingerprint.
    """
    deadline = time.monotonic() + max(0, min(wait_seconds, 50))
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    ready_since = None
    previous = None
    reason = "checking input"
    while True:
        try:
            s = snapshot(target)
            key = hashlib.sha256((s.socket + s.pane).encode()).hexdigest()
            with (STATE_DIR / (key + ".lock")).open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    reason = "another sender owns this pane"
                    ready_since = None
                else:
                    # Resolve only once: a renamed/reordered pane cannot redirect delivery.
                    current = snapshot(s.pane)
                    if current.socket != s.socket or current.pane != s.pane:
                        return "NOT SENT: pane identity changed."
                    reason = block_reason(current)
                    if current != previous:
                        ready_since = None
                    previous = current
                    if reason:
                        ready_since = None
                    else:
                        ready_since = time.monotonic() if ready_since is None else ready_since
                        if not current.kind or time.monotonic() - ready_since >= STABLE_SECONDS:
                            final = snapshot(current.pane)
                            reason = block_reason(final)
                            if final != current or reason:
                                ready_since = None
                                reason = reason or "input changed before delivery"
                            else:
                                record = STATE_DIR / (key + ".json")
                                digest = hashlib.sha256(json.dumps([keys, literal, enter]).encode()).hexdigest()
                                last = json.loads(record.read_text()) if record.exists() else {}
                                if current.kind and last.get("digest") == digest and time.time() - last.get("at", 0) < 15:
                                    return "NOT SENT: duplicate peer message within 15 seconds; do not resend."
                                # Record the attempt before writing: uncertain errors must not duplicate sends.
                                record.write_text(json.dumps({"digest": digest, "at": time.time()}))
                                return deliver(current.pane, keys, literal, enter)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            return f"NOT SENT or delivery uncertain: {exc}. Inspect the pane before retrying."
        if time.monotonic() >= deadline:
            return f"NOT SENT: {reason}. Nothing queued; wait and retry through the guarded tool."
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--file", help="UTF-8 message file; avoids shell quoting problems")
    parser.add_argument("--wait", type=float, default=25)
    args = parser.parse_args()
    if args.check:
        s = snapshot(args.target)
        print(json.dumps({"pane": s.pane, "kind": s.kind, "blocked": block_reason(s) or None}))
    elif args.file:
        result = send(args.target, [Path(args.file).read_text()], literal=True, enter=True, wait_seconds=args.wait)
        print(result or "DELIVERED")
        raise SystemExit(1 if result else 0)
    else:
        parser.error("use --check or --file")
