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
        count_file = base / 'session_count'
        count = int(count_file.read_text()) + 1 if count_file.exists() else 1
        count_file.write_text(str(count))
        print(json.dumps(dict(body, id='ses_fake' if count == 1 else 'ses_fake2')))
    elif 'interrupt' in args[2]:
        if mode in ('abort_failure', 'budget_abort_failure'): sys.exit(1)
        (base / 'interrupted').touch()
        print('{}')
    elif args[2] == '/api/session/active':
        print(json.dumps({'ses_foreign': {'status': 'running'}} if mode == 'foreign_active' else {}))
    elif '/message?' in args[2]:
        if mode == 'budget_api_slow' and not (base / 'interrupted').exists(): time.sleep(3)
        if mode == 'budget_writeup_verify_slow' and 'ses_fake2/' in args[2]: time.sleep(3)
        prompt = (base / 'prompt').read_text()
        messages = [{'id':'msg_user','type':'user','text':prompt,'time':{'created':1}}]
        if mode.startswith('replay_') or 'ses_fake2/' in args[2]:
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
session_id = args[args.index('--session') + 1]
if session_id == 'ses_fake2':
    (base / 'writeup_prompt').write_text((base / 'prompt').read_text())
    if mode == 'budget_writeup_stall': time.sleep(60)
    if mode == 'budget_writeup_error': sys.exit(3)
def emit(kind, part=None, **kw):
    print(json.dumps(dict(type=kind, sessionID=session_id, **({'part': part} if part is not None else {}), **kw)), flush=True)
def step(mid): emit('step_start', {'messageID': mid, 'type': 'step-start'})
def text(mid, value): emit('text', {'messageID': mid, 'type': 'text', 'text': value})
def finish(mid, reason): emit('step_finish', {'messageID': mid, 'type': 'step-finish', 'reason': reason, 'tokens': {'input': 10, 'output': 3}})
if mode.startswith('budget_') and session_id == 'ses_fake':
    step('msg_1')
    text('msg_1', 'NARRATIVE MUST NOT BE EVIDENCE')
    emit('tool_use', {'messageID':'msg_1','id':'tool_1','tool':'read','state':{
        'status':'completed','input':{'path':'a.py','offset':1},
        'output':'1: print(1)' + ('\n' + ('x'*50000) if mode == 'budget_long_evidence' else '')}})
    emit('step_finish', {'messageID':'msg_1','type':'step-finish','reason':'tool-calls',
                        'tokens':{'input':80,'output':999,'cache':{'read':30,'write':0}}})
    (base / 'explored').touch()
    time.sleep(60)
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
if mode == 'complete_hang' and session_id == 'ses_fake': time.sleep(60)
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
    def submit(mode='success', timeout=10, scope=None, **limits):
        (tmp_path / 'mode').write_text(mode)
        result = worker.submit('Trace the entry point.', str(repo), scope or ['.'], 'test', timeout, **limits)
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
    assert json.loads(result.stdout)['session_id'] == 'ses_fake2'


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



@pytest.mark.parametrize('mode,limits,reason', [
    ('budget_steps', {'max_steps':1}, 'max_steps'),
    ('budget_context', {'max_context_tokens':100}, 'max_context_tokens'),
    ('budget_time', {'exploration_seconds':1}, 'exploration_seconds'),
])
def test_exploration_budget_stops_then_uses_fresh_deny_all_writeup(runner, mode, limits, reason):
    worker, submit, tmp, _ = runner
    result = wait(worker, submit(mode, **limits)['job_id'])
    assert result['status'] == 'partial', result
    assert result['stop_reason'] == reason
    assert result['steps'] == 1
    assert result['observed_context_tokens'] == 110  # latest input+cache, excludes output
    assert result['exploration_session_id'] == 'ses_fake'
    assert result['writeup_session_id'] == 'ses_fake2'
    assert result['phase'] == 'finished'
    calls = [json.loads(line) for line in (tmp / 'calls.jsonl').read_text().splitlines()]
    creates = [c for c in calls if c[:3] == ['api','post','/api/session']]
    assert len(creates) == 2
    assert json.loads(creates[1][-1])['agent'] == 'build'
    runs = [c for c in calls if c[0] == 'run']
    assert runs[1][runs[1].index('--agent') + 1] == 'build'
    assert json.loads(creates[1][-1])['permissions'] == [{'action':'*','resource':'*','effect':'deny'}]
    interrupt = next(i for i,c in enumerate(calls) if 'interrupt' in ' '.join(c))
    assert interrupt < calls.index(creates[1])
    prompt = (tmp / 'writeup_prompt').read_text()
    assert '500' in prompt and 'untrusted' in prompt.lower()
    assert '1: print(1)' in prompt
    assert 'NARRATIVE MUST NOT BE EVIDENCE' not in prompt
    assert not worker.GPU_LOCK.exists()


@pytest.mark.parametrize('mode', ['budget_writeup_stall', 'budget_writeup_error'])
def test_writeup_failure_preserves_deterministic_partial_handoff(runner, mode):
    worker, submit, _, _ = runner
    result = wait(worker, submit(mode, max_steps=1, writeup_seconds=1)['job_id'])
    assert result['status'] == 'partial', result
    assert result['completion_source'] == 'evidence_handoff'
    report = Path(result['report_path']).read_text()
    assert 'a.py' in report and '1: print(1)' in report
    assert 'max_steps' in report and 'partial' in report.lower()
    assert not worker.GPU_LOCK.exists()


def test_evidence_packet_is_bounded_and_marks_truncation(runner):
    worker, submit, _, _ = runner
    result = wait(worker, submit('budget_long_evidence', max_steps=1)['job_id'])
    packet = Path(result['evidence_path'])
    assert packet.stat().st_size <= 12288
    assert 'truncated' in packet.read_text().lower()
    assert packet.stat().st_mode & 0o777 == 0o600


def test_budget_abort_uncertain_never_starts_writeup(runner):
    worker, submit, tmp, _ = runner
    result = wait(worker, submit('budget_abort_failure', max_steps=1)['job_id'])
    assert result['status'] == 'cleanup_required'
    assert result['writeup_session_id'] is None
    assert (tmp / 'session_count').read_text() == '1'
    assert worker.GPU_LOCK.exists()


def test_cancel_precedes_budget_writeup(runner):
    worker, submit, tmp, _ = runner
    job = submit('budget_cancel', exploration_seconds=2)
    deadline = time.monotonic() + 3
    while not (tmp / 'explored').exists() and time.monotonic() < deadline: time.sleep(.02)
    worker.cancel(job['job_id'])
    result = wait(worker, job['job_id'])
    assert result['status'] == 'cancelled'
    assert result['writeup_session_id'] is None
    assert (tmp / 'session_count').read_text() == '1'


@pytest.mark.parametrize('name,value', [('max_steps',0),('max_steps',9),('max_steps',True),('max_context_tokens',0),('max_context_tokens',16001),
    ('exploration_seconds',121),('writeup_seconds',61),('writeup_seconds',0)])
def test_budget_limits_are_validated(runner, name, value):
    worker, _, _, repo = runner
    with pytest.raises(ValueError):
        worker.submit('task',str(repo),['.'],'test', **{name:value})


def test_normal_fast_report_does_not_start_writeup(runner):
    worker, submit, tmp, _ = runner
    result = wait(worker, submit()['job_id'])
    assert result['status'] == 'completed'
    assert result['writeup_session_id'] is None
    assert result['budgets'] == {'max_steps':8,'max_context_tokens':16000,
                                  'exploration_seconds':120,'writeup_seconds':60}
    assert (tmp / 'session_count').read_text() == '1'


def test_reconcile_ignores_only_our_confirmed_abort_artifacts(runner):
    worker, _, _, _ = runner
    messages = [
        {'type':'user','text':'task'},
        {'type':'assistant','id':'msg_1','error':{'type':'aborted'},'time':{'created':1,'completed':2},
         'content':[{'type':'tool','id':'tool_1','name':'read','state':{'status':'error','error':{'type':'aborted'}}}]}]
    events = worker.Events('ses_fake')
    worker._observe_messages(events,messages,'task',after_abort=True)
    assert not events.evidence.entries
    messages[1]['error'] = {'type':'permission_denied'}
    with pytest.raises(worker.ProtocolError):
        worker._observe_messages(events,messages,'task',after_abort=True)


def test_repeated_and_older_usage_does_not_inflate_steps_or_regress_context(runner):
    worker, _, _, _ = runner
    events = worker.Events('ses_fake')
    events.observe_usage('msg_1',{'input':20,'output':3})
    events.observe_usage('msg_2',{'input':30,'output':4,'cache':{'read':10}})
    events.observe_usage('msg_1',{'input':20,'output':3})
    assert len(events.step_ids) == 2
    assert events.tokens['input'] == 50
    assert events.observed_context_tokens == 40


def test_writeup_bounds_long_original_task_and_discloses_truncation(runner):
    worker, _, tmp, repo = runner
    (tmp / 'mode').write_text('budget_steps')
    job = worker.submit('x'*32768,str(repo),['.'],'test',10,max_steps=1)
    result = wait(worker,job['job_id'])
    assert result['status'] == 'partial'
    prompt = (tmp / 'writeup_prompt').read_bytes()
    assert len(prompt) <= 16384 and b'[truncated]' in prompt



@pytest.mark.parametrize('mode,limits', [
    ('budget_api_slow', {'exploration_seconds':6}),
    ('complete_hang', {'exploration_seconds':1}),
    ('budget_writeup_verify_slow', {'max_steps':1, 'writeup_seconds':1}),
])
def test_phase_expiry_during_api_or_post_completion_stall_keeps_partial(runner, mode, limits):
    worker, submit, _, _ = runner
    result = wait(worker,submit(mode,timeout=12,**limits)['job_id'])
    assert result['status'] == 'partial', result
    assert result['report_path']
    assert not worker.GPU_LOCK.exists()


def test_evidence_tool_ids_are_scoped_to_assistant_message(runner):
    worker, _, _, _ = runner
    evidence = worker.Evidence()
    def part(message, output):
        return {'messageID':message,'id':'call_0','tool':'read',
                'state':{'status':'completed','input':{'path':'a.py'},'output':output}}
    evidence.add(part('msg_1','first'))
    evidence.add(part('msg_2','second'))
    # The session API has an enclosing message ID, not a field on each part.
    api_part = part('msg_2','second')
    del api_part['messageID']
    evidence.add(api_part, message_id='msg_2')
    assert [entry['output'] for entry in evidence.entries] == ['first','second']
