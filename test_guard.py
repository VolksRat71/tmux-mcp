"""Input protection: drafts and questions must never receive peer keystrokes."""
import pytest
import hashlib
import subprocess
import sys

import guard


def screen(body, kind="codex", activity=0, hook=""):
    lines = body.splitlines()
    row = next(i for i, line in reversed(list(enumerate(lines)))
               if line.startswith(("›", "❯")))
    return guard.Snapshot("%7", kind, kind, body, row, 2, activity, hook)


@pytest.mark.parametrize("body, reason", [
    ("• Done.\n› Re\n  GPT-6", "draft"),
    ("• Done.\n› first line\n  second line\n  GPT-6", "draft"),
    ("• Done.\n› \n  draft below the cursor\n  GPT-6", "draft"),
    ("• Which export path should I use?\n› Ask Codex to do anything\n  GPT-6", "question"),
    ("  1. Yes\n› 2. No\n  Enter to select", "dialog"),
    ("• Working (esc to interrupt)\n› Ask Codex to do anything", "working"),
])
def test_occupied_inputs_are_blocked(body, reason):
    assert reason in guard.block_reason(screen(body), now=100)


def test_recent_human_activity_blocks_empty_prompt():
    assert "typing" in guard.block_reason(screen("› Ask Codex to do anything", activity=99), now=100)


def test_old_question_does_not_block_after_new_answer():
    s = screen("• Which one?\n› Option one\n• Done.\n› Ask Codex to do anything")
    assert guard.block_reason(s, now=100) == ""


def test_question_hook_overrides_empty_prompt():
    assert "question" in guard.block_reason(screen("› Ask Codex to do anything", hook="AskUserQuestion"), now=100)


def test_empty_claude_prompt_is_ready():
    assert guard.block_reason(screen("⏺ Done.\n────\n❯ \n────", "claude"), now=100) == ""


def test_unknown_agent_screen_is_blocked():
    s = guard.Snapshot("%7", "codex", "codex", "Something changed", 0, 0, 0, "")
    assert "unrecognized" in guard.block_reason(s, now=100)


def test_agent_exit_does_not_deliver_message_to_shell():
    s = guard.Snapshot("%7", "codex", "zsh", "$ ", 0, 2, 0, "")
    assert "foreground" in guard.block_reason(s, now=100)


def test_timeout_never_sends(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path)
    monkeypatch.setattr(guard, "snapshot", lambda target: screen("› Re"))
    writes = []
    monkeypatch.setattr(guard, "deliver", lambda *args: writes.append(args))
    result = guard.send("%7", ["hello"], literal=True, enter=True, wait_seconds=0)
    assert "NOT SENT" in result
    assert not writes


def test_ready_prompt_sends_once(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path)
    monkeypatch.setattr(guard, "STABLE_SECONDS", 0)
    monkeypatch.setattr(guard, "snapshot", lambda target: screen("› Ask Codex to do anything"))
    writes = []
    monkeypatch.setattr(guard, "deliver", lambda *args: writes.append(args) or "")
    assert guard.send("%7", ["hello"], literal=True, enter=True, wait_seconds=1) == ""
    assert len(writes) == 1
    assert "duplicate" in guard.send("%7", ["hello"], literal=True, enter=True, wait_seconds=0)
    assert len(writes) == 1


def test_human_starts_typing_at_final_check(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path)
    monkeypatch.setattr(guard, "STABLE_SECONDS", 0)
    samples = iter([screen("› Ask Codex to do anything"), screen("› Ask Codex to do anything"), screen("› Nate draft")])
    last = screen("› Nate draft")
    monkeypatch.setattr(guard, "snapshot", lambda target: next(samples, last))
    writes = []
    monkeypatch.setattr(guard, "deliver", lambda *args: writes.append(args))
    assert "NOT SENT" in guard.send("%7", ["hello"], literal=True, enter=True, wait_seconds=.2)
    assert not writes


def test_another_process_owns_delivery_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "STATE_DIR", tmp_path)
    monkeypatch.setattr(guard, "snapshot", lambda target: screen("› Ask Codex to do anything"))
    key = hashlib.sha256(b"%7").hexdigest()
    lock = tmp_path / (key + ".lock")
    code = ("import fcntl, time; "
            f"f=open({str(lock)!r}, 'a+'); "
            "fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(10)")
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        writes = []
        monkeypatch.setattr(guard, "deliver", lambda *args: writes.append(args))
        assert "another sender" in guard.send("%7", ["peer"], literal=True, enter=True, wait_seconds=0)
        assert not writes
    finally:
        process.terminate()
        process.wait(timeout=3)
