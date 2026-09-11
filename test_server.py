"""Behavioural tests for tmux-mcp, run against a throwaway tmux server.

Every test talks to a private tmux socket (``-L tmux-mcp-test``) so nothing
here can reach the user's real sessions.
"""

import inspect
import os
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
