"""
tmux-mcp - MCP server for driving tmux panes from an agent.

Design rules (they exist because agents kept tripping on the old version):

* Anything that types into a pane EXECUTES immediately when Enter is
  pressed, so every send returns evidence: a status header plus the pane
  tail, never a bare "Keys sent.".
* ``tmux_run`` is the primary tool. It types a command, presses Enter, and
  waits until the shell is idle again (or a timeout), so one call replaces
  the send / sleep / read dance.
* Waits are real waits. They poll and return as soon as the pane is idle
  or the expected text shows up, and the header says which happened.
* Re-sending the same text to a pane that is still busy is refused unless
  ``force=True``; the usual cause is an agent thinking a command did not
  run because the output had not arrived yet.
* ``target`` is required on every tool that types. tmux's "current pane"
  may be the agent's own pane.
"""

import os
import subprocess
import time

import guard
import delivery
import worker

from mcp.server.fastmcp import FastMCP

INSTRUCTIONS = """\
tmux protocol for agents:
- Sending keys EXECUTES immediately when Enter is pressed. One send = one run.
- To run a shell command, call tmux_run. It types the command, presses Enter,
  waits until the shell is idle or the timeout passes, and returns the output.
  Do not follow it with a separate Enter or a read; the result is already there.
- Every send returns a header like
  [target | foreground: bash (idle) | sent + Enter | finished in 0.6s]
  followed by the pane tail. Read the header before deciding anything.
- If the header says "still running", the command has NOT failed. Call
  tmux_wait_and_read on the same target. NEVER resend a command because its
  output looked empty or the prompt had not come back yet.
- "foreground: X (busy)" where X is not a shell means the pane is running a
  program; typed text goes to that program's stdin, not to a shell.
- tmux_send_keys is for control keys (C-c, Escape, Up, Tab). It does not press
  Enter unless enter=True. tmux_send_text is for typing into an interactive
  program (a REPL, a prompt).
- target is required on anything that types. Find one with tmux_list_panes.
- Peer Claude/Codex messages MUST use tmux_message. It queues automatically
  while human typing, drafts or questions block delivery. QUEUED means keep
  working; do not resend. Use tmux_message_status to check delivery. Never
  bypass the guard with shell send-keys.
- Use worker_submit for bounded, context-heavy read-only Qwen tasks. Claude,
  Codex and their subagents keep ownership of decisions and verification.
  worker_status returns lifecycle, OpenCode session/title, and report path;
  completed means a report was produced, not that its evidence was accepted.
  Do not resubmit a queued/running job or read raw progress into your context.
"""

mcp = FastMCP("tmux", instructions=INSTRUCTIONS)

# Set TMUX_MCP_SOCKET to talk to a private tmux server (tests use this).
_SOCKET = os.environ.get("TMUX_MCP_SOCKET", "")

_SHELLS = {"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh"}
_POLL = 0.25          # seconds between polls while waiting
_SETTLE = 0.4         # seconds to let a send land before capturing
_MIN_WAIT = 0.3       # let the shell fork before judging it idle
_DUPLICATE_WINDOW = 15.0  # seconds within which an identical send is suspect

# pane_id -> (text, monotonic time) of the last executed send
_send_history: dict[str, tuple[str, float]] = {}


# --- tmux plumbing -----------------------------------------------------------

def _run(cmd: list[str], timeout: int = 10) -> str:
    """Run a tmux command and return stdout, or an ERROR: line."""
    base = ["tmux", "-L", _SOCKET] if _SOCKET else ["tmux"]
    result = subprocess.run(
        base + cmd, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0 and result.stderr:
        return "ERROR: {}".format(result.stderr.strip())
    return result.stdout


def _display(target: str, fmt: str) -> str:
    cmd = ["display-message", "-p"]
    if target:
        cmd += ["-t", target]
    return _run(cmd + [fmt]).strip()


def _pane_id(target: str) -> str:
    return _display(target, "#{pane_id}")


def _foreground(target: str) -> str:
    """Name of the process in the foreground of the pane (bash, node, ...)."""
    return _display(target, "#{pane_current_command}")


def _is_idle(target: str) -> bool:
    return _foreground(target) in _SHELLS


def _capture(target: str, lines: int) -> str:
    cmd = ["capture-pane", "-p", "-S", str(-lines)]
    if target:
        cmd += ["-t", target]
    out = _run(cmd).rstrip("\n")
    if out.startswith("ERROR"):
        return out
    # capture-pane returns the visible screen plus `lines` of history;
    # keep only the last `lines` lines so the tail is what the name says.
    return "\n".join(out.split("\n")[-lines:])


_NAMED_KEYS = {
    "enter", "escape", "space", "tab", "btab", "bspace", "up", "down", "left",
    "right", "home", "end", "pageup", "pagedown", "ppage", "npage", "ic", "dc",
    *{f"f{i}" for i in range(1, 13)},
}


def _is_key_name(token: str) -> bool:
    lowered = token.lower()
    if lowered in _NAMED_KEYS:
        return True
    # Modifier chords: C-c, M-x, C-M-Left, S-Tab, ...
    if "-" in token and len(token) > 2:
        *mods, base = token.split("-")
        return all(m in ("C", "M", "S") for m in mods) and (
            len(base) == 1 or base.lower() in _NAMED_KEYS)
    return False


def _split_keys(keys: str) -> list[str]:
    """Split "Up Up Enter" into separate key args; leave plain text intact."""
    tokens = keys.split(" ")
    if len(tokens) > 1 and all(_is_key_name(t) for t in tokens):
        return tokens
    return [keys]


def _send(target: str, *keys: str, literal: bool = False) -> str:
    cmd = ["send-keys", "-t", target]
    if literal:
        cmd.append("-l")
    return _run(cmd + list(keys))


# --- result formatting -------------------------------------------------------

def _header(target: str, *parts: str) -> str:
    fg = _foreground(target)
    state = "idle" if fg in _SHELLS else "busy"
    label = target or _pane_id("")
    return "[{}]".format(" | ".join([label, f"foreground: {fg} ({state})", *parts]))


def _busy_note(fg_before: str) -> str:
    """Warn when text was typed into a pane whose foreground was not a shell."""
    if not fg_before or fg_before in _SHELLS:
        return ""
    return (
        f"NOTE: foreground was '{fg_before}', not a shell - the text went to "
        "that program's stdin. Use tmux_send_keys C-c to interrupt it if that "
        "was not intended."
    )


def _report(target: str, lines: int, *parts: str, fg_before: str = "") -> str:
    """Header + optional stdin warning + pane tail.

    fg_before is the foreground command as it was before a send; pass it
    from any tool that typed something so the warning reflects where the
    text actually went, not what the shell launched afterwards.
    """
    chunks = [_header(target, *parts)]
    note = _busy_note(fg_before)
    if note:
        chunks.append(note)
    chunks.append(_capture(target, lines))
    return "\n".join(chunks)


def _wait(target: str, timeout: float, until_text: str = "") -> tuple[str, float]:
    """Poll until the pane is idle+quiet, until_text appears, or timeout.

    Returns (outcome, elapsed) where outcome is 'idle', 'matched', or 'timeout'.
    """
    start = time.monotonic()
    time.sleep(min(_MIN_WAIT, timeout))
    previous = None
    while True:
        content = _capture(target, 200)
        elapsed = time.monotonic() - start
        if until_text and until_text in content:
            return "matched", elapsed
        if _is_idle(target) and content == previous:
            return "idle", elapsed
        if elapsed >= timeout:
            return "timeout", elapsed
        previous = content
        time.sleep(min(_POLL, max(timeout - elapsed, 0.01)))


# --- duplicate guard -----------------------------------------------------------

def _reset_send_history() -> None:
    _send_history.clear()


def _duplicate_refusal(target: str, text: str, lines: int) -> str:
    """Return a refusal message if this exact text was just sent to a busy pane."""
    pane = _pane_id(target)
    last = _send_history.get(pane)
    if not last:
        return ""
    last_text, last_at = last
    age = time.monotonic() - last_at
    if last_text != text or age > _DUPLICATE_WINDOW or _is_idle(target):
        return ""
    return "\n".join([
        _header(target, "REFUSED duplicate send"),
        f"The identical text was sent to this pane {age:.1f}s ago and the pane "
        "is still busy, so it was NOT sent again. The earlier command is "
        "probably still running: call tmux_wait_and_read on this target. "
        "If you really do want it sent twice, pass force=True.",
        _capture(target, lines),
    ])


def _record_send(target: str, text: str) -> None:
    _send_history[_pane_id(target)] = (text, time.monotonic())


# --- tools: discovery and reading --------------------------------------------

@mcp.tool()
def tmux_list_sessions() -> str:
    """List all tmux sessions with window/pane counts."""
    return _run(["list-sessions"]) or "No tmux sessions found."


@mcp.tool()
def tmux_list_panes(session: str = "") -> str:
    """List panes (all sessions if session is empty). Use this to pick a target.

    Each line: target  size  foreground-command  pane-id. Pass the target
    (e.g. "setup:0.0") or the pane id (e.g. "%3") to the other tools.
    """
    fmt = ("#{session_name}:#{window_index}.#{pane_index}  "
           "#{pane_width}x#{pane_height}  #{pane_current_command}  #{pane_id}")
    if session:
        return _run(["list-panes", "-t", session, "-F", fmt])
    return _run(["list-panes", "-a", "-F", fmt])


@mcp.tool()
def tmux_read_pane(target: str = "", lines: int = 50) -> str:
    """Read a pane right now, without waiting. Read-only.

    Prefer tmux_wait_and_read when a command may still be running: this
    tool returns whatever is on screen this instant, which is often just
    the echoed command line.

    Args:
        target: Pane target (e.g. "setup:0.0"). Empty = tmux's current pane.
        lines: Lines to capture from the bottom (default 50).
    """
    return _report(target, lines, "snapshot")


# --- tools: running and typing -----------------------------------------------

@mcp.tool()
def tmux_run(command: str, target: str, timeout: int = 30, lines: int = 50,
             force: bool = False) -> str:
    """Run a shell command in a pane and return its output. USE THIS FIRST.

    Types the command literally, presses Enter, then waits until the shell
    prompt is back and the screen has stopped changing, or until `timeout`
    seconds pass. One call = one execution; do not follow it with Enter or
    a separate read.

    The first line of the result is a status header ending in either
    "finished in Ns" or "still running after Ns". "still running" is not a
    failure: call tmux_wait_and_read on the same target to keep waiting.

    Refuses to send the identical command to a pane that is still busy from
    the previous send unless force=True.

    Args:
        command: Shell command, taken literally (no tmux key-name parsing).
        target: Pane target (e.g. "setup:0.0" or "%3"). Required.
        timeout: Max seconds to wait for the prompt to return (default 30, max 600).
        lines: Lines of pane tail to return (default 50).
        force: Send even if it duplicates a send still in flight.
    """
    if not force:
        refusal = _duplicate_refusal(target, command, lines)
        if refusal:
            return refusal
    timeout = min(max(timeout, 1), 600)
    fg_before = _foreground(target)
    err = guard.send(target, [command], literal=True, enter=True)
    if err:
        return err
    _record_send(target, command)
    outcome, elapsed = _wait(target, timeout)
    status = (f"finished in {elapsed:.1f}s" if outcome == "idle"
              else f"still running after {elapsed:.1f}s (timeout)")
    return _report(target, lines, "sent + Enter", status, fg_before=fg_before)


@mcp.tool()
def tmux_send_text(text: str, target: str, enter: bool = True, lines: int = 30,
                   force: bool = False) -> str:
    """Type literal text into a pane, for interactive programs (REPL, prompt).

    Enter is pressed by default, so the text is SUBMITTED immediately. For
    shell commands use tmux_run instead; it also waits for the result.
    Returns a status header and the pane tail after a short settle. When the
    header says the foreground is not a shell, the text went to that program.

    Refuses to resubmit identical text to a pane that is still busy from the
    previous send unless force=True.

    Args:
        text: Literal text; tmux key names are NOT interpreted.
        target: Pane target (e.g. "setup:0.0" or "%3"). Required.
        enter: Press Enter after the text (default True).
        lines: Lines of pane tail to return (default 30).
        force: Send even if it duplicates a send still in flight.
    """
    if enter and not force:
        refusal = _duplicate_refusal(target, text, lines)
        if refusal:
            return refusal
    fg_before = _foreground(target)
    err = guard.send(target, [text], literal=True, enter=enter)
    if err:
        return err
    if enter:
        _record_send(target, text)
    time.sleep(_SETTLE)
    return _report(target, lines, "sent + Enter" if enter else "typed, not submitted",
                   fg_before=fg_before)


@mcp.tool()
def tmux_send_keys(keys: str, target: str, enter: bool = False, lines: int = 30) -> str:
    """Send tmux key names to a pane: C-c, Escape, Up, Tab, Enter, and so on.

    Does NOT press Enter unless enter=True. For shell commands use tmux_run;
    for typing text into a program use tmux_send_text.
    Returns a status header and the pane tail after a short settle.

    Args:
        keys: Space-separated tmux key names or text (e.g. "C-c", "Escape", "Up Up").
        target: Pane target (e.g. "setup:0.0" or "%3"). Required.
        enter: Also press Enter afterwards (default False).
        lines: Lines of pane tail to return (default 30).
    """
    err = guard.send(target, _split_keys(keys), enter=enter)
    if err:
        return err
    if enter:
        _record_send(target, keys)
    time.sleep(_SETTLE)
    what = f"sent: {keys}" + (" + Enter" if enter else "")
    return _report(target, lines, what)


@mcp.tool()
def tmux_message(text: str, target: str, wait_seconds: int = 25) -> str:
    """Deliver a peer message only when the Claude/Codex input is safe.

    Waits for typing, unfinished drafts, questions and dialogs. If the initial
    wait ends, returns QUEUED with an ID; delivery continues automatically for
    up to one hour. Use tmux_message_status, not a resend. Never answer a
    question meant for the human. No force override exists.
    Prefix the text with your identity (for example 'From Codex: ...').
    """
    return delivery.enqueue(target, text, wait_seconds)


@mcp.tool()
def tmux_message_status(message_id: str) -> str:
    """Read a queued peer message's delivery status. Does not send input."""
    return delivery.status(message_id)


@mcp.tool()
def tmux_cancel_message(message_id: str) -> str:
    """Request cancellation of a queued message; check status for the outcome."""
    return delivery.cancel(message_id)


@mcp.tool()
def tmux_input_status(target: str) -> str:
    """Read why an agent pane is protected, without sending any input."""
    state = guard.snapshot(target)
    return f"[{state.pane} | {state.kind or 'other'} | {guard.block_reason(state) or 'ready'}]"


@mcp.tool()
def worker_submit(task: str, cwd: str, scope: list[str], owner: str,
                  timeout_seconds: int = 600) -> dict:
    """Delegate one bounded read-only task to the shared local Qwen scout.

    Specify an absolute working directory, explicit search paths in scope,
    an evidence-oriented task and the caller's owner label. No edits or test
    execution. Best for broad discovery, tracing, review or log analysis;
    avoid trivial lookups and work whose context you already have.
    Returns a job ID immediately; check worker_status, never submit copies.
    Local inference waits for the shared GPU lock. Job state is separate from
    tmux message delivery. Orchestrators must verify the final evidence.
    """
    return worker.submit(task, cwd, scope, owner, timeout_seconds)


@mcp.tool()
def worker_status(job_id: str) -> dict:
    """Read compact job state, observability metadata and final report path.

    Does not paste progress into a pane or return bulk tool output. Read the
    final report once completed; inspect cited evidence before accepting it.
    """
    return worker.status(job_id)


@mcp.tool()
def worker_cancel(job_id: str) -> dict:
    """Request cancellation of a local worker job; check status for outcome.

    A request is not proof that server-side inference has stopped. The worker
    owns cleanup and keeps resource protection until termination is verified.
    """
    return worker.cancel(job_id)


@mcp.tool()
def tmux_wait_and_read(target: str = "", seconds: int = 30, lines: int = 50,
                       until_text: str = "") -> str:
    """Wait for a pane to finish, then read it. Returns as soon as it can.

    Polls the pane and returns when the shell prompt is back and the screen
    has stopped changing ("idle"), when `until_text` appears ("matched"), or
    when `seconds` have passed ("timed out"). The header says which.
    Use this after tmux_run reports "still running", or after typing into a
    program that takes a while. Never resend a command instead of waiting.

    Args:
        target: Pane target (e.g. "setup:0.0"). Empty = tmux's current pane.
        seconds: Upper bound on the wait (default 30, max 600).
        lines: Lines of pane tail to return (default 50).
        until_text: Return early once this text is visible in the pane.
    """
    seconds = min(max(seconds, 1), 600)
    outcome, elapsed = _wait(target, seconds, until_text)
    if outcome == "matched":
        status = f"matched {until_text!r} after {elapsed:.1f}s"
    elif outcome == "idle":
        status = f"idle after {elapsed:.1f}s"
    else:
        status = f"timed out after {elapsed:.1f}s, still busy"
    return _report(target, lines, status)


# --- tools: pane management ------------------------------------------------------

@mcp.tool()
def tmux_new_pane(target: str = "", vertical: bool = False, command: str = "") -> str:
    """Split a pane to create a new one. Returns the new pane's target and id.

    Args:
        target: Pane to split (e.g. "setup:0.0"). Empty = current pane.
        vertical: If True, split vertically. Default is horizontal.
        command: Optional command to run in the new pane.
    """
    cmd = ["split-window", "-v" if vertical else "-h", "-P",
           "-F", "#{session_name}:#{window_index}.#{pane_index}  #{pane_id}"]
    if target:
        cmd += ["-t", target]
    if command:
        cmd.append(command)
    out = _run(cmd).strip()
    return f"Pane created: {out}" if out and not out.startswith("ERROR") else out


@mcp.tool()
def tmux_kill_pane(target: str) -> str:
    """Close a tmux pane.

    Args:
        target: Pane to kill (e.g. "setup:0.1").
    """
    return _run(["kill-pane", "-t", target]) or "Pane closed."


@mcp.tool()
def tmux_select_pane(target: str) -> str:
    """Focus a specific pane.

    Args:
        target: Pane to focus (e.g. "setup:0.1").
    """
    return _run(["select-pane", "-t", target]) or "Pane selected."


@mcp.tool()
def tmux_resize_pane(target: str, width: int = 0, height: int = 0) -> str:
    """Resize a tmux pane.

    Args:
        target: Pane to resize (e.g. "setup:0.0").
        width: Set absolute width in columns (0 = don't change).
        height: Set absolute height in rows (0 = don't change).
    """
    results = []
    if width > 0:
        results.append(_run(["resize-pane", "-t", target, "-x", str(width)]))
    if height > 0:
        results.append(_run(["resize-pane", "-t", target, "-y", str(height)]))
    return "\n".join(r for r in results if r) or "Pane resized."


if __name__ == "__main__":
    mcp.run(transport="stdio")
