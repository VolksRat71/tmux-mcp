"""Lifecycle tests use a real detached runner and a fake OpenCode executable."""
import importlib
import json
import os
from pathlib import Path
import signal
import sys
import time

import pytest


def worker_module():
    assert (Path(__file__).parent / 'worker.py').exists(), 'headless worker is not implemented'
    return importlib.import_module('worker')


@pytest.fixture
def runner(tmp_path, monkeypatch):
    worker = worker_module()
    executable = tmp_path / 'opencode'
    executable.write_text('#!' + sys.executable + '\n' + r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
base = Path(__file__).parent
args = sys.argv[1:]
with (base / 'calls.jsonl').open('a') as f: f.write(json.dumps(args) + '\n')
mode = (base / 'mode').read_text()
if args[0] == 'api':
    if args[1:3] == ['post', '/api/session']:
        body = json.loads(args[args.index('--data') + 1])
        (base / 'created.json').write_text(json.dumps(body))
        print(json.dumps(dict(body, id='ses_fake')))
    elif 'interrupt' in args[2]:
        if mode == 'abort_failure': sys.exit(1)
        (base / 'interrupted').touch()
        print('{}')
    elif args[2] == '/api/session/active':
        print(json.dumps({'ses_foreign': {'status': 'running'}} if mode == 'foreign_active' else {}))
    elif '/message?' in args[2]:
        prompt = (base / 'prompt').read_text()
        messages = [{'id':'msg_user','type':'user','text':prompt,'time':{'created':1}}]
        if mode.startswith('replay_'):
            messages += [
                {'id':'msg_2','type':'assistant','finish':'stop','time':{'created':2,'completed':3},
                 'tokens':{'input':20,'output':5,'cache':{'read':2,'write':0}},
                 'content':[{'type':'text','text':'Recovered completed report: a.py:1.'}]},
                {'id':'msg_idle','type':'idle','outcome':'succeeded','time':{'created':4}}]
            if mode == 'replay_wrong_prompt': messages[0]['text'] = 'unrelated user text'
            if mode == 'replay_no_idle': messages.pop()
            if mode == 'replay_failed_tool': messages[1]['content'].append({'type':'tool','state':{'status':'error'}})
        print(json.dumps({'data':list(reversed(messages)), 'cursor':{}}))
    else: sys.exit(2)
    sys.exit(0)
assert args[0] == 'run'
(base / 'prompt').write_text(sys.stdin.read())
def emit(kind, part=None, **kw):
    print(json.dumps(dict(type=kind, sessionID='ses_fake', **({'part': part} if part is not None else {}), **kw)), flush=True)
def step(mid): emit('step_start', {'messageID': mid, 'type': 'step-start'})
def text(mid, value): emit('text', {'messageID': mid, 'type': 'text', 'text': value})
def finish(mid, reason): emit('step_finish', {'messageID': mid, 'type': 'step-finish', 'reason': reason, 'tokens': {'input': 10, 'output': 3}})
step('msg_1'); text('msg_1', 'I am still searching.'); finish('msg_1', 'tool-calls')
if mode in ('sleep', 'abort_failure'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    (base / 'child.pid').write_text(str(child.pid))
    time.sleep(60)
if mode == 'progress': sys.exit(0)
if mode == 'malformed': print('{bad'); sys.exit(0)
if mode == 'truncated': print('{"type":', end=''); sys.exit(0)
if mode == 'overflow': print('x' * 1100000); sys.exit(0)
step('msg_2'); text('msg_2', 'Conclusion: src/a.py:1 defines the entry point.')
if mode == 'error': emit('error', error={'message': 'permission denied'})
if mode == 'tool_error': emit('tool_use', {'type': 'tool', 'state': {'status': 'error', 'error': 'permission denied'}})
if not mode.startswith('replay_'): finish('msg_2', 'length' if mode == 'length' else 'stop')
if mode == 'nonzero': sys.exit(3)
''')
    executable.chmod(0o700)
    (tmp_path / 'mode').write_text('success')
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'a.py').write_text('print(1)\n')
    monkeypatch.setattr(worker, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(worker, 'GPU_LOCK', tmp_path / 'gpu-lock')
    monkeypatch.setattr(worker, 'OPENCODE', str(executable))
    jobs = []
    def submit(mode='success', timeout=10, scope=None):
        (tmp_path / 'mode').write_text(mode)
        result = worker.submit('Trace the entry point.', str(repo), scope or ['.'], 'test', timeout)
        jobs.append(result['job_id'])
        return result
    yield worker, submit, tmp_path, repo
    for job in jobs:
        worker.cancel(job)
    for job in jobs:
        wait(worker, job)


def wait(worker, job, wanted=None):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        result = worker.status(job)
        if result['status'] in (wanted or worker.TERMINAL): return result
        time.sleep(.03)
    pytest.fail(f'job did not settle: {result}')


def test_detached_success_report_and_observable_session(runner):
    worker, submit, tmp, repo = runner
    job = submit()
    result = wait(worker, job['job_id'])
    assert result['status'] == 'completed', result
    assert result['session_id'] == 'ses_fake'
    assert result['title'] == f"qwen-worker:test [{job['job_id'][-8:]}]"
    assert Path(result['report_path']).read_text() == 'Conclusion: src/a.py:1 defines the entry point.\n'
    assert result['tokens']['input'] == 20
    assert result['started_at'] <= result['finished_at']
    assert result['accepted'] is False
    assert 'I am still searching' not in json.dumps(result)
    calls = [json.loads(s) for s in (tmp / 'calls.jsonl').read_text().splitlines()]
    run = next(c for c in calls if c[0] == 'run')
    assert '--standalone' not in run
    assert run[run.index('--session') + 1] == 'ses_fake'
    assert run[run.index('--title') + 1] == result['title']
    assert not worker.GPU_LOCK.exists()


@pytest.mark.parametrize('mode', ['progress', 'nonzero', 'malformed', 'truncated', 'error', 'tool_error', 'length', 'overflow'])
def test_invalid_completion_never_publishes_report(runner, mode):
    worker, submit, _, _ = runner
    result = wait(worker, submit(mode)['job_id'])
    assert result['status'] == 'failed', result
    assert result['report_path'] is None
    assert result['diagnostic']
    assert not worker.GPU_LOCK.exists()


def test_occupied_lock_deduplicates_then_times_out_without_touching_owner(runner):
    worker, submit, _, _ = runner
    worker.GPU_LOCK.mkdir()
    owner = worker.GPU_LOCK / 'owner'
    owner.write_text('Claude owns this GPU')
    first = submit(timeout=1)
    second = submit(timeout=1)
    assert first['job_id'] == second['job_id']
    assert second['deduplicated'] is True
    result = wait(worker, first['job_id'])
    assert result['status'] == 'timed_out'
    assert owner.read_text() == 'Claude owns this GPU'


def test_cancel_interrupts_shared_session_and_cleans_process_tree(runner):
    worker, submit, tmp, _ = runner
    job = submit('sleep')
    wait(worker, job['job_id'], {'running'})
    deadline = time.monotonic() + 5
    while not (tmp / 'child.pid').exists() and time.monotonic() < deadline: time.sleep(.03)
    child = int((tmp / 'child.pid').read_text())
    worker.cancel(job['job_id'])
    result = wait(worker, job['job_id'])
    assert result['status'] == 'cancelled'
    assert (tmp / 'interrupted').exists()
    assert not worker.GPU_LOCK.exists()
    deadline = time.monotonic() + 3
    while worker._alive(child) and time.monotonic() < deadline: time.sleep(.03)
    assert not worker._alive(child)


def test_timeout_interrupts_session(runner):
    worker, submit, tmp, _ = runner
    result = wait(worker, submit('sleep', timeout=1)['job_id'])
    assert result['status'] == 'timed_out'
    assert (tmp / 'interrupted').exists()
    assert not worker.GPU_LOCK.exists()


def test_unconfirmed_server_abort_retains_gpu_lock(runner):
    worker, submit, _, _ = runner
    result = wait(worker, submit('abort_failure', timeout=1)['job_id'])
    assert result['status'] == 'cleanup_required'
    assert worker.GPU_LOCK.exists()
    assert result['gpu_lock_retained'] is True


def test_scope_traversal_and_symlink_escape_rejected(runner):
    worker, _, tmp, repo = runner
    (repo / 'escape').symlink_to(tmp)
    for scope in [['../'], ['escape'], ['*']]:
        with pytest.raises(ValueError):
            worker.submit('task', str(repo), scope, 'test')


def test_scoped_permissions_override_inherited_tools(runner):
    worker, submit, tmp, repo = runner
    (repo / 'link').symlink_to(tmp / 'mode')
    result = wait(worker, submit(scope=['a.py'])['job_id'])
    body = json.loads((tmp / 'created.json').read_text())
    assert body['permissions'][0] == {'action': '*', 'resource': '*', 'effect': 'deny'}
    allowed = [r for r in body['permissions'] if r['effect'] == 'allow']
    assert allowed == [{'action': 'read', 'resource': 'a.py', 'effect': 'allow'}]
    assert result['effective_tools'] == ['read']


def test_missing_worker_is_not_running_forever(runner):
    worker, submit, _, _ = runner
    worker.GPU_LOCK.mkdir()
    job = submit()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        record = worker._read(job['job_id'])
        if record.get('worker_pid'): break
        time.sleep(.02)
    os.kill(record['worker_pid'], signal.SIGKILL)
    time.sleep(.1)
    result = wait(worker, job['job_id'])
    assert result['status'] == 'failed'
    assert 'worker' in result['diagnostic'].lower()


def test_native_read_scope_does_not_allow_parent_traversal(runner):
    worker, _, _, repo = runner
    subdir = repo / 'src'
    subdir.mkdir()
    rules, tools, _ = worker._permissions(str(subdir), ['.'], {'root': str(repo)})
    import fnmatch
    def effect(action, resource):
        return [rule['effect'] for rule in rules
                if fnmatch.fnmatchcase(action, rule['action']) and fnmatch.fnmatchcase(resource, rule['resource'])][-1]
    assert effect('read', 'a.py') == 'allow'
    assert effect('read', '../a.py') == 'deny'
    assert effect('bash', 'ls') == 'deny'
    assert tools == ['read']


def test_scope_symlink_denies_and_search_tool_restriction(runner):
    worker, _, tmp, repo = runner
    (repo / 'escape').symlink_to(tmp)
    rules, tools, _ = worker._permissions(str(repo), ['.'], {'root': str(repo)})
    assert {'action': 'read', 'resource': 'escape/*', 'effect': 'deny'} in rules
    assert tools == ['read']
    (repo / 'escape').unlink()
    (repo / 'internal').symlink_to(repo / 'a.py')
    rules, tools, _ = worker._permissions(str(repo), ['.'], {'root': str(repo)})
    assert tools == ['read', 'grep', 'glob']


def test_protocol_rejects_text_after_stop(runner):
    worker, _, _, _ = runner
    events = worker.Events('ses_fake')
    def event(kind, **part):
        events.accept(json.dumps({'type': kind, 'sessionID': 'ses_fake', 'part': {'messageID': 'msg_1', **part}}))
    event('step_start')
    event('text', text='Final')
    event('step_finish', reason='stop')
    with pytest.raises(worker.ProtocolError):
        event('text', text='Truncated next response')


def test_failed_job_records_actual_output_size(runner):
    worker, submit, _, _ = runner
    result = wait(worker, submit('malformed')['job_id'])
    assert result['output_bytes'] > 0


def test_cli_fallback_and_completed_jobs_are_not_cached(runner):
    worker, submit, _, _ = runner
    first = submit()
    wait(worker, first['job_id'])
    second = submit()
    assert second['job_id'] != first['job_id']
    wait(worker, second['job_id'])
    import subprocess
    # CLI public status uses the normal default directory; testing it is a
    # separate interpreter with only its state location adapted, not a mock API.
    code = "import worker; from pathlib import Path; worker.STATE_DIR=Path(" + repr(str(worker.STATE_DIR)) + "); worker.main()"
    result = subprocess.run([sys.executable, '-c', code, 'status', second['job_id']],
                            cwd=Path(worker.__file__).parent, capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)['session_id'] == 'ses_fake'


def test_inflight_dedupe_detects_changed_non_git_scope(runner):
    worker, submit, _, repo = runner
    worker.GPU_LOCK.mkdir()
    first = submit(timeout=1)
    (repo / 'a.py').write_text('print(2)\n')
    second = submit(timeout=1)
    assert first['job_id'] != second['job_id']


def test_queued_scope_cannot_redirect_through_new_symlink(runner):
    worker, submit, _, repo = runner
    (repo / 'src').mkdir()
    (repo / 'private').mkdir()
    worker.GPU_LOCK.mkdir()
    first = submit(scope=['src'])
    (repo / 'src').rmdir()
    (repo / 'src').symlink_to(repo / 'private')
    worker.GPU_LOCK.rmdir()
    result = wait(worker, first['job_id'])
    assert result['status'] == 'failed'
    assert result['session_id'] is None
    assert 'scope' in result['diagnostic']



def test_cli_completion_race_recovers_authoritative_session_report(runner):
    worker, submit, _, _ = runner
    result = wait(worker, submit('replay_success')['job_id'])
    assert result['status'] == 'completed', result
    assert Path(result['report_path']).read_text() == 'Recovered completed report: a.py:1.\n'
    assert result['completion_source'] == 'session_messages'
    assert result['tokens']['cache']['read'] == 2


@pytest.mark.parametrize('mode', ['replay_wrong_prompt', 'replay_no_idle', 'replay_failed_tool'])
def test_incomplete_cli_stream_cannot_accept_unproven_transcript(runner, mode):
    worker, submit, _, _ = runner
    result = wait(worker, submit(mode)['job_id'])
    assert result['status'] == 'failed'
    assert result['report_path'] is None


def test_foreign_active_session_blocks_new_inference_and_keeps_foreign_session(runner):
    worker, submit, tmp, _ = runner
    result = wait(worker, submit('foreign_active')['job_id'])
    assert result['status'] == 'failed'
    assert 'active' in result['diagnostic']
    calls = [json.loads(s) for s in (tmp / 'calls.jsonl').read_text().splitlines()]
    assert not any(c[0] == 'run' or c[1] == 'post' for c in calls)
    assert not worker.GPU_LOCK.exists()


def test_failure_preserves_bounded_private_diagnostics(runner):
    worker, submit, _, _ = runner
    result = wait(worker, submit('overflow')['job_id'])
    directory = worker._dir(result['job_id'])
    raw = directory / 'stdout.tail.log'
    assert raw.exists()
    assert raw.stat().st_size <= 65536
    assert raw.stat().st_mode & 0o777 == 0o600
    metadata = json.loads((directory / 'diagnostics.json').read_text())
    assert metadata['stdout_truncated'] is True
    assert 'stdout.tail.log' not in json.dumps(result)
