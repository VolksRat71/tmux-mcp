"""Private, bounded peer-message queue; all delivery still goes through guard."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import guard
import guard_hook

QUEUE_DIR = guard.STATE_DIR / "messages"
TERMINAL = {"sent", "failed", "expired", "cancelled"}


def identity(target):
    s = guard.snapshot(target)
    if not s.kind:
        raise ValueError("target is not a recognized agent pane")
    server_pid, pane_pid = guard.tmux("display-message", "-p", "-t", s.pane,
                                     "#{pid}|#{pane_pid}").strip().split("|")
    return [s.socket, s.pane, server_pid, pane_pid, guard_hook.pane_session(s.pane)]


def save(path, job):
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(job))
    tmp.replace(path)


def job_path(message_id):
    if not re.fullmatch(r"[a-f0-9]{64}", message_id):
        raise ValueError("invalid message ID")
    return QUEUE_DIR / (message_id + ".json")


def status(message_id):
    path = job_path(message_id)
    job = json.loads(path.read_text())
    if job["status"] not in TERMINAL and time.time() - job["created"] > 10:
        with path.with_suffix(".worker-lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                job = json.loads(path.read_text())
                if job["status"] not in TERMINAL:
                    job.update(status="failed", reason="delivery worker exited; inspect the pane before retrying", updated=time.time())
                    job.pop("text", None)
                    save(path, job)
    label = {"sent": "DELIVERED", "waiting": "QUEUED"}.get(job["status"], job["status"].upper())
    return f"{label} {message_id}: {job.get('reason', '')}"


def cancel(message_id):
    path = job_path(message_id)
    job = json.loads(path.read_text())
    if job["status"] in TERMINAL:
        return status(message_id)
    path.with_suffix(".cancel").touch()
    return f"CANCELLATION REQUESTED {message_id}; check status for the delivery outcome."


def launch(path):
    with (QUEUE_DIR / "worker.log").open("a") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(path)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)


def enqueue(target, text, wait_seconds=25):
    QUEUE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    target_identity = identity(target)
    message_id = hashlib.sha256(json.dumps([target_identity, text]).encode()).hexdigest()
    path = job_path(message_id)
    with path.with_suffix(".submit-lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = json.loads(path.read_text()) if path.exists() else None
        # A pending retry refers to the existing delivery, never a second send.
        # Failed/uncertain jobs are retained for inspection, not retried blindly.
        if old is None or (old["status"] == "sent" and time.time() - old["updated"] >= 60):
            path.with_suffix(".cancel").unlink(missing_ok=True)
            job = {"id": message_id, "identity": target_identity, "text": text,
                   "status": "queued", "reason": "waiting for safe input; do not resend",
                   "created": time.time(), "updated": time.time(), "expires": time.time() + 3600}
            save(path, job)
            try:
                launch(path)
            except OSError as exc:
                job.update(status="failed", reason=f"could not start delivery worker: {exc}")
                save(path, job)
    deadline = time.monotonic() + min(max(wait_seconds, 0), 50)
    while time.monotonic() < deadline:
        if json.loads(path.read_text())["status"] in TERMINAL:
            break
        time.sleep(.1)
    return status(message_id)


def run_job(path):
    path = Path(path)
    with path.with_suffix(".worker-lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        job = json.loads(path.read_text())
        if job["status"] in TERMINAL:
            return
        cancelled = path.with_suffix(".cancel").exists
        def stop_requested():
            return (cancelled() or time.time() >= job["expires"]
                    or identity(job["identity"][1]) != job["identity"])
        try:
            while True:
                if cancelled():
                    job.update(status="cancelled", reason="no input sent")
                    break
                if time.time() >= job["expires"]:
                    job.update(status="expired", reason="input never became safe within one hour; no input sent")
                    break
                if identity(job["identity"][1]) != job["identity"]:
                    job.update(status="failed", reason="target pane or conversation changed; no input sent")
                    break
                job.update(status="waiting", worker_pid=os.getpid(), updated=time.time())
                save(path, job)
                result = guard.send(job["identity"][1], [job["text"]], literal=True,
                                    enter=True, wait_seconds=2, cancelled=stop_requested)
                if not result:
                    job.update(status="sent", reason="peer message delivered")
                    break
                if result.startswith("CANCELLED"):
                    if cancelled():
                        job.update(status="cancelled", reason=result)
                    elif time.time() >= job["expires"]:
                        job.update(status="expired", reason="message expired; no input sent")
                    else:
                        job.update(status="failed", reason="target conversation changed; no input sent")
                    break
                if "uncertain" in result or "duplicate" in result:
                    job.update(status="failed", reason=result + " Inspect the pane before any new attempt.")
                    break
                job.update(reason=result, updated=time.time())
                save(path, job)
                time.sleep(.25)
        except Exception as exc:
            job.update(status="failed", reason=f"delivery stopped: {exc}; inspect the pane before retrying")
        job["updated"] = time.time()
        # Retain status and a content hash, but discard message text on completion.
        job.pop("text", None)
        save(path, job)


if __name__ == "__main__":
    run_job(sys.argv[1])
