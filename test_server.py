"""Behavioural tests for tmux-mcp, run against a throwaway tmux server.

Every test talks to a private tmux socket (``-L tmux-mcp-test``) so nothing
here can reach the user's real sessions.
"""

import inspect
import dataclasses
import json
import os
import shlex
import subprocess
import time

import pytest

SOCKET = "tmux-mcp-test"
os.environ["TMUX_MCP_SOCKET"] = SOCKET

import server  # noqa: E402  (must come after the env var is set)

SESSION = "t"
TARGET = f"{SESSION}:0.0"


def tmux(*args: str) -> str:
    return subprocess.run(
        ["tmux", "-L", SOCKET, *args], capture_output=True, text=True
    ).stdout


@pytest.fixture(autouse=True)
def fresh_session():
    tmux("kill-server")
    subprocess.run(
        ["tmux", "-L", SOCKET, "new-session", "-d", "-s", SESSION,
         "-x", "120", "-y", "40", "bash --norc --noprofile"],
        check=True,
    )
    # Give bash a moment to print its prompt, then give it a boring one.
    time.sleep(0.3)
    tmux("send-keys", "-t", TARGET, "-l", "PS1='$ '")
    tmux("send-keys", "-t", TARGET, "Enter")
    time.sleep(0.2)
    server._reset_send_history()
    yield
    tmux("kill-server")


# --- tmux_run: the one-call happy path -------------------------------------

def test_run_executes_and_returns_output_when_finished():
    out = server.tmux_run("echo run-marker-$((40+2))", TARGET)
    assert "run-marker-42" in out
    assert "finished" in out.splitlines()[0]


def test_run_reports_still_running_when_command_outlives_timeout():
    start = time.monotonic()
    out = server.tmux_run("sleep 30", TARGET, timeout=1)
    elapsed = time.monotonic() - start
    assert elapsed < 3
    assert "still running" in out.splitlines()[0]
    assert "foreground: sleep" in out.splitlines()[0]


def test_run_returns_before_timeout_when_command_finishes_early():
    start = time.monotonic()
    server.tmux_run("echo quick", TARGET, timeout=20)
    assert time.monotonic() - start < 5


# --- status header on every send --------------------------------------------

def test_send_text_returns_header_and_pane_tail():
    out = server.tmux_send_text("echo text-marker", TARGET)
    header = out.splitlines()[0]
    assert header.startswith(f"[{TARGET}")
    assert "sent + Enter" in header
    assert "text-marker" in out


def test_send_warns_when_keys_go_to_a_running_program():
    server.tmux_send_text("cat", TARGET)  # cat now owns stdin
    time.sleep(0.3)
    out = server.tmux_send_text("hello", TARGET)
    header = out.splitlines()[0]
    assert "foreground: cat" in header
    assert "busy" in header
    assert "not a shell" in out


# --- send_keys is for control keys and does not press Enter by default ------

def test_send_keys_default_does_not_execute():
    out = server.tmux_send_keys("echo keys-marker", TARGET)
    assert "sent" in out.splitlines()[0]
    assert "Enter" not in out.splitlines()[0]
    time.sleep(0.3)
    pane = tmux("capture-pane", "-p", "-t", TARGET)
    # The command is typed on the prompt line but has not produced output.
    assert "$ echo keys-marker" in pane
    assert pane.count("keys-marker") == 1


def test_send_keys_can_deliver_a_control_key():
    server.tmux_send_text("sleep 30", TARGET)
    time.sleep(0.3)
    out = server.tmux_send_keys("C-c", TARGET)
    time.sleep(0.5)
    assert server._foreground(TARGET) == "bash"
    assert "C-c" in out.splitlines()[0]


# --- duplicate guard ----------------------------------------------------------

def test_duplicate_send_to_busy_pane_is_refused():
    server.tmux_send_text("sleep 30", TARGET)
    time.sleep(0.3)
    out = server.tmux_send_text("sleep 30", TARGET)
    assert "REFUSED" in out
    assert "force" in out
    # Only one sleep process is running in that pane.
    pane_pid = tmux("display", "-p", "-t", TARGET, "#{pane_pid}").strip()
    children = subprocess.run(
        ["pgrep", "-P", pane_pid, "sleep"], capture_output=True, text=True
    ).stdout.split()
    assert len(children) == 1


def test_duplicate_send_to_idle_pane_is_allowed():
    first = server.tmux_run("echo twice", TARGET)
    second = server.tmux_run("echo twice", TARGET)
    assert "REFUSED" not in first
    assert "REFUSED" not in second
    assert second.count("twice") >= 2


def test_duplicate_send_with_force_is_allowed():
    server.tmux_send_text("sleep 30", TARGET)
    time.sleep(0.3)
    out = server.tmux_send_text("sleep 30", TARGET, force=True)
    assert "REFUSED" not in out


# --- wait_and_read is a real wait, not a sleep ---------------------------------

def test_wait_and_read_returns_early_when_pane_is_idle():
    start = time.monotonic()
    out = server.tmux_wait_and_read(TARGET, seconds=20)
    assert time.monotonic() - start < 5
    assert "idle" in out.splitlines()[0]


def test_wait_and_read_returns_when_until_text_appears():
    server.tmux_send_text("sleep 1; echo waited-marker", TARGET)
    start = time.monotonic()
    out = server.tmux_wait_and_read(TARGET, seconds=20, until_text="waited-marker")
    assert time.monotonic() - start < 6
    assert "waited-marker" in out
    assert "matched" in out.splitlines()[0]


def test_wait_and_read_reports_timeout_when_still_busy():
    server.tmux_send_text("sleep 30", TARGET)
    out = server.tmux_wait_and_read(TARGET, seconds=1)
    assert "timed out" in out.splitlines()[0]


# --- contract: target required on anything that types ----------------------

@pytest.mark.parametrize("fn", [
    server.tmux_run, server.tmux_send_keys, server.tmux_send_text,
])
def test_target_is_required_on_sending_tools(fn):
    param = inspect.signature(fn).parameters["target"]
    assert param.default is inspect.Parameter.empty


def test_read_pane_keeps_optional_target():
    param = inspect.signature(server.tmux_read_pane).parameters["target"]
    assert param.default is not inspect.Parameter.empty


def test_server_instructions_state_the_protocol():
    text = server.mcp.instructions
    assert "tmux_run" in text
    assert "immediately" in text
    assert "resend" in text.lower() or "re-send" in text.lower()


@pytest.mark.parametrize("keys,expected", [
    ("Up Up Enter", ["Up", "Up", "Enter"]),
    ("C-c", ["C-c"]),
    ("C-M-Left S-Tab", ["C-M-Left", "S-Tab"]),
    ("echo keys-marker", ["echo keys-marker"]),
    ("git status", ["git status"]),
])
def test_split_keys_only_splits_sequences_of_key_names(keys, expected):
    assert server._split_keys(keys) == expected


# --- regressions found in live smoke testing -----------------------------------

def test_lines_limits_the_pane_tail():
    for i in range(6):
        server.tmux_run(f"echo line-{i}", TARGET)
    out = server.tmux_read_pane(TARGET, lines=2)
    body = out.splitlines()[1:]
    assert len(body) == 2
    assert "line-5" in out
    assert "line-3" not in out


def test_run_from_idle_shell_does_not_warn_about_stdin():
    out = server.tmux_run("sleep 30", TARGET, timeout=1)
    assert "still running" in out.splitlines()[0]
    assert "not a shell" not in out


def fake_agent(monkeypatch, tmp_path, draft, clear_after=0):
    """Real PTY input, with only the fake process's identity adapted."""
    receipt = tmp_path / "received"
    script = tmp_path / "fake_tui.py"
    script.write_text(
        "import os, sys, time, tty\n"
        "tty.setraw(0)\n"
        "def render(text):\n"
        "    sys.stdout.write('\\x1b[2J\\x1b[H• Done.\\r\\n› ' + text + '\\x1b[2;3H')\n"
        "    sys.stdout.flush()\n"
        f"render({draft!r})\n"
        + (f"time.sleep({clear_after}); render('Ask Codex to do anything')\n" if clear_after else "")
        + f"open({str(receipt)!r}, 'wb').write(os.read(0, 4096))\n"
        "time.sleep(10)\n"
    )
    tmux("set-option", "-p", "-t", TARGET, "@mcp_agent_kind", "codex")
    tmux("send-keys", "-t", TARGET, "-l", f"{shlex.quote(os.sys.executable)} {shlex.quote(str(script))}")
    tmux("send-keys", "-t", TARGET, "Enter")
    time.sleep(.3)
    real_snapshot = server.guard.snapshot
    monkeypatch.setattr(server.guard, "snapshot", lambda target:
                        dataclasses.replace(real_snapshot(target), foreground="codex"))
    monkeypatch.setattr(server.guard, "STATE_DIR", tmp_path / "guard-state")
    return receipt


def test_live_draft_never_receives_peer_input(monkeypatch, tmp_path):
    receipt = fake_agent(monkeypatch, tmp_path, "Re")
    result = server.guard.send(TARGET, ["From Codex: hello"], literal=True, enter=True, wait_seconds=1)
    assert "NOT SENT" in result and "draft" in result
    assert not receipt.exists() or receipt.read_bytes() == b""


def test_live_send_waits_until_human_draft_clears(monkeypatch, tmp_path):
    receipt = fake_agent(monkeypatch, tmp_path, "Nate's answer", clear_after=1.2)
    start = time.monotonic()
    result = server.guard.send(TARGET, ["From Claude: hello"], literal=True, enter=True, wait_seconds=5)
    assert result == ""
    assert time.monotonic() - start >= 1
    time.sleep(.1)
    assert b"From Claude: hello" in receipt.read_bytes()


@pytest.mark.parametrize("tool", ["text", "keys", "run"])
def test_all_legacy_send_paths_respect_guard_even_force(monkeypatch, tmp_path, tool):
    receipt = fake_agent(monkeypatch, tmp_path, "Re")
    real_send = server.guard.send
    monkeypatch.setattr(server.guard, "send", lambda *args, **kw: real_send(*args, **kw, wait_seconds=0))
    if tool == "text":
        result = server.tmux_send_text("peer", TARGET, force=True)
    elif tool == "keys":
        result = server.tmux_send_keys("Enter", TARGET)
    else:
        result = server.tmux_run("peer", TARGET, force=True)
    assert "NOT SENT" in result
    assert not receipt.exists() or receipt.read_bytes() == b""


def test_real_question_hook_latches_and_matches_completion(monkeypatch, tmp_path):
    import guard_hook
    monkeypatch.setattr(guard_hook, "owns_pane", lambda payload: True)
    monkeypatch.setenv("TMUX_PANE", tmux("display-message", "-p", "-t", TARGET, "#{pane_id}").strip())
    monkeypatch.setattr(server.guard, "STATE_DIR", tmp_path)
    guard_hook.handle({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q1"})
    assert server.guard.snapshot(TARGET).question == "AskUserQuestion:q1"
    guard_hook.handle({"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "old"})
    assert server.guard.snapshot(TARGET).question == "AskUserQuestion:q1"
    guard_hook.handle({"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "q1"})
    assert server.guard.snapshot(TARGET).question == ""


def test_detached_observer_clears_failed_question_on_private_pane(monkeypatch, tmp_path):
    import guard_hook
    pane = tmux("display-message", "-p", "-t", TARGET, "#{pane_id}").strip()
    monkeypatch.setenv("TMUX_PANE", pane)
    transcript = tmp_path / "observer.jsonl"
    transcript.touch()
    token = "request_user_input:failed-call"
    guard_hook.question_state(token, True)
    guard_hook.start_result_watch(token, str(transcript), 0)
    transcript.write_text(json.dumps({"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "failed-call",
        "output": "request_user_input is unavailable in Default mode"}}) + "\n")
    deadline = time.monotonic() + 3
    while server.guard.snapshot(TARGET).question and time.monotonic() < deadline:
        time.sleep(.05)
    assert server.guard.snapshot(TARGET).question == ""


def test_detached_queue_delivers_after_draft_clears_despite_prose_question(monkeypatch, tmp_path):
    """An actual process named codex, real tmux socket, and detached worker."""
    source = tmp_path / "fake_codex.c"
    binary = tmp_path / "codex"
    receipt = tmp_path / "receipt"
    source.write_text(r'''
#include <termios.h>
#include <unistd.h>
#include <stdio.h>
int main(int argc, char **argv) {
    struct termios raw; tcgetattr(0, &raw); cfmakeraw(&raw); tcsetattr(0, TCSANOW, &raw);
    printf("\033[2J\033[H• Done.\r\n› Nate draft\033[2;3H"); fflush(stdout);
    usleep(1800000);
    printf("\033[2J\033[H• The decision still waiting on you is in the other pane.\r\n› Ask Codex to do anything\033[2;3H"); fflush(stdout);
    char buffer[4096]; int n=read(0, buffer, sizeof(buffer));
    FILE *f=fopen(argv[3], "wb"); if (n>0) fwrite(buffer, 1, n, f); fclose(f);
    sleep(10); return 0;
}
''')
    subprocess.run(["cc", str(source), "-o", str(binary)], check=True, capture_output=True)
    session = "12345678-1234-1234-1234-123456789abc"
    command = f"{shlex.quote(str(binary))} resume {session} {shlex.quote(str(receipt))}"
    tmux("send-keys", "-t", TARGET, "-l", command)
    tmux("send-keys", "-t", TARGET, "Enter")
    time.sleep(.3)
    monkeypatch.setattr(server.delivery, "QUEUE_DIR", tmp_path / "messages")
    result = server.tmux_message("From Claude: queued test", TARGET, wait_seconds=0)
    assert result.startswith("QUEUED ")
    assert not receipt.exists()
    message_id = result.split()[1].rstrip(":")
    deadline = time.monotonic() + 7
    while not server.tmux_message_status(message_id).startswith("DELIVERED") and time.monotonic() < deadline:
        time.sleep(.1)
    assert server.tmux_message_status(message_id).startswith("DELIVERED")
    assert b"From Claude: queued test" in receipt.read_bytes()
