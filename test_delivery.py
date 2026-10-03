import json
import pytest
import delivery


@pytest.fixture
def queue(monkeypatch, tmp_path):
    monkeypatch.setattr(delivery, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(delivery, "identity", lambda target: ["socket", "%7", "10", "20", "session"])
    monkeypatch.setattr(delivery, "launch", lambda path: None)
    return tmp_path


def test_wait_expiry_keeps_message_queued(queue):
    out = delivery.enqueue("%7", "From Claude: ready", wait_seconds=0)
    assert "QUEUED" in out
    job = json.loads(next(queue.glob("*.json")).read_text())
    assert job["text"] == "From Claude: ready"
    assert job["status"] == "queued"


def test_retries_reuse_pending_message(queue):
    first = delivery.enqueue("%7", "peer", wait_seconds=0)
    second = delivery.enqueue("%7", "peer", wait_seconds=0)
    assert first == second
    assert len(list(queue.glob("*.json"))) == 1


@pytest.mark.parametrize("reason", [
    "NOT SENT: agent is working. Nothing queued; wait and retry through the guarded tool.",
    "NOT SENT: unfinished human draft",
    "waiting for safe input; do not resend",
])
def test_pending_status_never_tells_sender_to_retry(queue, reason):
    delivery.enqueue("%7", "peer", wait_seconds=0)
    path = next(queue.glob("*.json"))
    job = json.loads(path.read_text())
    job.update(status="waiting", reason=reason)
    path.write_text(json.dumps(job))

    out = delivery.status(path.stem)
    assert out.startswith("QUEUED " + path.stem)
    assert "automatically" in out
    assert "do not resend" in out.lower()
    assert "Nothing queued" not in out
    assert "retry" not in out
    assert "NOT SENT" not in out
    assert json.loads(path.read_text())["text"] == "peer"


def test_terminal_status_preserves_uncertain_delivery_warning(queue):
    delivery.enqueue("%7", "peer", wait_seconds=0)
    path = next(queue.glob("*.json"))
    job = json.loads(path.read_text())
    reason = "NOT SENT or delivery uncertain: tmux timed out. Inspect the pane before retrying."
    job.update(status="failed", reason=reason)
    path.write_text(json.dumps(job))
    assert delivery.status(path.stem) == f"FAILED {path.stem}: {reason}"


def test_worker_waits_then_delivers_once(queue, monkeypatch):
    delivery.enqueue("%7", "peer", wait_seconds=0)
    path = next(queue.glob("*.json"))
    calls = []
    def send(*args, **kwargs):
        calls.append(args)
        return "NOT SENT: unfinished human draft" if len(calls) == 1 else ""
    monkeypatch.setattr(delivery.guard, "send", send)
    monkeypatch.setattr(delivery.time, "sleep", lambda seconds: None)
    delivery.run_job(path)
    assert json.loads(path.read_text())["status"] == "sent"
    delivery.run_job(path)
    assert len(calls) == 2


def test_unknown_delivery_is_not_retried(queue, monkeypatch):
    delivery.enqueue("%7", "peer", wait_seconds=0)
    path = next(queue.glob("*.json"))
    monkeypatch.setattr(delivery.guard, "send", lambda *a, **kw: "NOT SENT or delivery uncertain: tmux timed out")
    delivery.run_job(path)
    assert json.loads(path.read_text())["status"] == "failed"


def test_switched_conversation_never_receives_queued_message(queue, monkeypatch):
    delivery.enqueue("%7", "peer", wait_seconds=0)
    path = next(queue.glob("*.json"))
    monkeypatch.setattr(delivery, "identity", lambda target: ["socket", "%7", "10", "20", "different-session"])
    monkeypatch.setattr(delivery.guard, "send", lambda *a, **kw: pytest.fail("wrong conversation"))
    delivery.run_job(path)
    assert json.loads(path.read_text())["status"] == "failed"


def test_cancelled_queue_never_sends(queue, monkeypatch):
    delivery.enqueue("%7", "peer", wait_seconds=0)
    path = next(queue.glob("*.json"))
    delivery.cancel(path.stem)
    monkeypatch.setattr(delivery.guard, "send", lambda *a, **kw: pytest.fail("cancelled message sent"))
    delivery.run_job(path)
    assert json.loads(path.read_text())["status"] == "cancelled"


def test_dead_worker_is_reported_not_silently_queued(queue):
    delivery.enqueue("%7", "peer", wait_seconds=0)
    path = next(queue.glob("*.json"))
    job = json.loads(path.read_text())
    job["created"] -= 20
    path.write_text(json.dumps(job))
    assert "FAILED" in delivery.status(path.stem)
    assert "worker exited" in delivery.status(path.stem)


def test_expired_message_is_never_sent(queue, monkeypatch):
    delivery.enqueue("%7", "peer", wait_seconds=0)
    path = next(queue.glob("*.json"))
    job = json.loads(path.read_text())
    job["expires"] = 0
    path.write_text(json.dumps(job))
    monkeypatch.setattr(delivery.guard, "send", lambda *a, **kw: pytest.fail("expired message sent"))
    delivery.run_job(path)
    assert json.loads(path.read_text())["status"] == "expired"


def test_conversation_is_rechecked_during_send(queue, monkeypatch):
    delivery.enqueue("%7", "peer", wait_seconds=0)
    path = next(queue.glob("*.json"))
    def send(*args, **kwargs):
        monkeypatch.setattr(delivery, "identity", lambda target: ["a new conversation"])
        assert kwargs["cancelled"]()
        return "CANCELLED: no input sent"
    monkeypatch.setattr(delivery.guard, "send", send)
    delivery.run_job(path)
    assert json.loads(path.read_text())["status"] == "failed"
