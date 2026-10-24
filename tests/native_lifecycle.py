"""Native Codex lifecycle integration, with bounded synthetic test conversations."""

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--work-dir', required=True, help='Directory for synthetic test artifacts')
parser.add_argument('--vault')
parser.add_argument('--codex-bin')
args = parser.parse_args()
skill = Path(__file__).resolve().parents[1]
settings = json.loads((skill / 'settings.json').read_text()) if (skill / 'settings.json').exists() else {}
if not (args.vault or settings.get('vault')):
    parser.error('Supply --vault or use the installed skill')
VAULT = Path(args.vault or settings['vault']).resolve()
config = json.loads((VAULT / '.state/config.json').read_text())
CLI = args.codex_bin or config['codex_bin']
BASE = Path(args.work_dir).resolve()
BASE.mkdir(parents=True, exist_ok=True)
events = []
stderr = (BASE / 'server.log').open('wb')
env = dict(os.environ)
env.pop('CODEX_THREAD_ID', None)
proc = subprocess.Popen(
    [CLI, 'app-server', '--listen', 'stdio://'],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=stderr,
    env=env,
    text=True,
    bufsize=1,
)
inbox = queue.Queue()


def pump():
    for line in proc.stdout:
        try:
            inbox.put(json.loads(line))
        except ValueError:
            pass


threading.Thread(target=pump, daemon=True).start()
seq = 0


def send(msg):
    proc.stdin.write(json.dumps(msg, ensure_ascii=False) + '\n')
    proc.stdin.flush()


def receive(timeout):
    try:
        message = inbox.get(timeout=timeout)
    except queue.Empty:
        return {}
    if 'method' in message:
        if 'id' in message:
            # This test never authorizes tool executions or escalations.
            send({'id': message['id'], 'result': {'decision': 'decline'}})
        events.append(message)
        if message['method'] in ('hook/started', 'hook/completed', 'turn/completed', 'error'):
            p = message.get('params', {})
            run = p.get('run', {})
            print(
                'EVENT',
                json.dumps(
                    {
                        'method': message['method'],
                        'thread': p.get('threadId'),
                        'event': run.get('eventName'),
                        'status': run.get('status'),
                        'message': p.get('error', {}).get('message'),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return message


def call(method, params, timeout=90):
    global seq
    seq += 1
    rid = seq
    send({'id': rid, 'method': method, 'params': params})
    deadline = time.monotonic() + timeout
    while True:
        msg = receive(max(0.1, deadline - time.monotonic()))
        if msg.get('id') == rid and 'method' not in msg:
            if 'error' in msg:
                raise RuntimeError(method + ': ' + json.dumps(msg['error']))
            return msg['result']
        if time.monotonic() >= deadline:
            raise TimeoutError(method)


def wait_event(predicate, after, timeout=360):
    deadline = time.monotonic() + timeout
    while True:
        for event in events[after:]:
            if predicate(event):
                return event
        if time.monotonic() >= deadline:
            raise TimeoutError('event wait')
        receive(max(0.1, min(15, deadline - time.monotonic())))


def turn(tid, text):
    before = len(events)
    reply = call(
        'turn/start',
        {'threadId': tid, 'input': [{'type': 'text', 'text': text, 'text_elements': []}], 'effort': 'low'},
        90,
    )
    turn_id = reply['turn']['id']
    result = wait_event(
        lambda e: e.get('method') == 'turn/completed'
        and e['params'].get('threadId') == tid
        and e['params'].get('turn', {}).get('id') == turn_id,
        before,
    )
    if result['params']['turn'].get('status') != 'completed':
        raise RuntimeError('test turn did not complete: ' + str(result))
    return before


def metadata(tid):
    return json.loads((VAULT / '.state' / tid / 'state.json').read_text())


def own_runs(tid, after=0):
    return [
        e['params']['run']
        for e in events[after:]
        if e.get('method') == 'hook/completed'
        and e['params'].get('threadId') == tid
        and str(e['params'].get('run', {}).get('statusMessage', '')).startswith('preserve-context:')
    ]


threads = []
report = {}
try:
    call(
        'initialize',
        {
            'clientInfo': {'name': 'preserve-context-integration', 'version': '1.0'},
            'capabilities': {'experimentalApi': True},
        },
    )
    send({'method': 'initialized', 'params': {}})
    listed = call('hooks/list', {'cwds': [str(BASE)]})
    all_hooks = listed['data'][0]['hooks']
    own = [h for h in all_hooks if 'preserve-context/scripts/hooks.py' in h.get('command', '')]
    print(
        'TRUST',
        json.dumps([{'event': h['eventName'], 'trust': h['trustStatus'], 'enabled': h['enabled']} for h in own]),
        flush=True,
    )
    assert len(own) == 2 and all(h['enabled'] and h['trustStatus'] == 'trusted' for h in own)
    assert not listed['data'][0]['errors']
    common = {
        'cwd': str(BASE),
        'model': config.get('model'),
        'sandbox': 'read-only',
        'ephemeral': False,
        'developerInstructions': 'This is an isolated preserve-context lifecycle test. Do not use tools, skills, or change files. Answer only the brief requested acknowledgement. The hooks themselves maintain memory.',
        'config': {'project_doc_max_bytes': 0, 'features.memories': False, 'model_reasoning_effort': 'low'},
    }
    started = call('thread/start', common)
    manual = started['thread']['id']
    threads.append(manual)
    turn(
        manual,
        'Цель теста: сохранить точный operation ID native-job-42, ограничение retry_delay не менять, решение не кешировать (кеш скроет сбой), следующий шаг pytest tests/test_retry.py::test_limit. Исправление пока не проверено. Ответь только OK.',
    )
    before = len(events)
    call('thread/compact/start', {'threadId': manual})
    wait_event(
        lambda e: e.get('method') == 'item/completed'
        and e['params'].get('threadId') == manual
        and e['params'].get('item', {}).get('type') == 'contextCompaction',
        before,
    )
    wait_event(lambda e: e.get('method') == 'turn/completed' and e['params'].get('threadId') == manual, before)
    # Manual compaction has no immediate model continuation. Restoration is
    # delivered before the NEXT request, so start that request before asserting.
    turn(manual, 'Продолжение синтетического теста после ручного компакта. Ответь только OK.')
    runs = own_runs(manual, before)
    assert any(r['eventName'] == 'preCompact' and r['status'] == 'completed' for r in runs), runs
    assert any(r['eventName'] == 'sessionStart' and r['status'] == 'completed' for r in runs), runs
    state = metadata(manual)
    assert not state['pending'], state['pending']
    combined = json.dumps([state['checkpoint'], state['entries']], ensure_ascii=False)
    assert 'native-job-42' in combined and 'pytest' in combined and 'retry_delay' in combined
    report['manual'] = {
        'thread': manual,
        'coverage': state['coverage'],
        'hook_runs': [{'event': r['eventName'], 'status': r['status']} for r in runs],
    }
    print('MANUAL_OK', flush=True)
    # Override only this disposable thread to make automatic compaction reachable.
    auto_args = dict(common)
    auto_args['config'] = dict(common['config'], model_auto_compact_token_limit=28000)
    started = call('thread/start', auto_args)
    automatic = started['thread']['id']
    threads.append(automatic)
    prefix = 'Цель теста: сохранить operation ID auto-job-77 и следующий шаг проверить retry limit. Ниже синтетическое приложение без полезных сведений; достаточно помнить только цель и ID.\n'
    padding = '\n'.join(
        'Synthetic appendix line %d: alpha beta gamma delta epsilon zeta eta theta.' % i for i in range(550)
    )
    before = len(events)
    turn(
        automatic,
        prefix + padding + '\nКонец приложения. ID auto-job-77; следующий шаг проверить retry limit. Ответь только OK.',
    )
    turn(automatic, 'Продолжение теста: ID auto-job-77 всё ещё важен. Ответь только OK.')
    # Drain ready notifications without starting another turn.
    until = time.monotonic() + 3
    while time.monotonic() < until:
        try:
            receive(0.2)
        except queue.Empty:
            pass
    runs = own_runs(automatic, before)
    assert any(r['eventName'] == 'preCompact' and r['status'] == 'completed' for r in runs), runs
    assert any(r['eventName'] == 'sessionStart' and r['status'] == 'completed' for r in runs), runs
    state = metadata(automatic)
    assert not state['pending'], state['pending']
    assert 'auto-job-77' in json.dumps([state['checkpoint'], state['entries']], ensure_ascii=False)
    report['automatic'] = {
        'thread': automatic,
        'coverage': state['coverage'],
        'hook_runs': [{'event': r['eventName'], 'status': r['status']} for r in runs],
        'test_only_auto_limit': 28000,
    }
    print('AUTOMATIC_OK', flush=True)
finally:
    for tid in threads:
        try:
            call('thread/archive', {'threadId': tid}, 20)
        except Exception as error:
            print('ARCHIVE_NOTE', str(error), flush=True)
    (BASE / 'events.json').write_text(json.dumps(events, ensure_ascii=False, indent=2))
    (BASE / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    stderr.close()
print('NATIVE_INTEGRATION_OK', flush=True)
