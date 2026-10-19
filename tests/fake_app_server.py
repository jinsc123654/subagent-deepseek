#!/usr/bin/env python3
"""Deterministic external protocol fixture; never accesses a model service."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

thread = 'test-thread'
turn = 'test-turn'
scenario = os.environ.get('TEST_SCENARIO', 'success')
trace = os.environ.get('TEST_TRACE')
if trace:
    Path(trace).write_text(json.dumps({'pid': os.getpid(), 'home': os.environ['CODEX_HOME']}))


def send(message):
    print(json.dumps(message), flush=True)


def response(key, result):
    send({'id': key, 'result': result})


def event(method, **params):
    send({'method': method, 'params': params})


def finish(status='completed'):
    event('turn/completed', threadId=thread, turn={'id': turn, 'status': status})


def answer(text, phase='final_answer', tid=turn, th=thread):
    event('item/completed', threadId=th, turnId=tid,
          item={'type': 'agentMessage', 'text': text, 'phase': phase})


for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if not method:
        if scenario == 'approval' and message.get('id') == 900:
            assert message['result']['decision'] == 'decline'
            answer('permission declined')
            finish()
        continue
    if method == 'initialize':
        if scenario == 'malformed':
            print('not JSON', flush=True)
            continue
        response(message['id'], {})
    elif method == 'thread/start':
        params = message['params']
        assert params['approvalPolicy'] == 'never'
        assert params['modelProvider'] == 'deepseek'
        assert params['ephemeral'] is True
        response(message['id'], {
            'thread': {'id': thread, 'ephemeral': scenario != 'not-ephemeral'},
            'modelProvider': 'openai' if scenario == 'route-mismatch' else 'deepseek',
            'model': 'deepseek-flash', 'reasoningEffort': 'high',
            'approvalPolicy': 'never',
            'sandbox': {'type': 'dangerFullAccess' if scenario == 'permission-mismatch' else {
                'read-only': 'readOnly', 'workspace-write': 'workspaceWrite',
                'danger-full-access': 'dangerFullAccess'}[params['sandbox']]}})
    elif method == 'turn/start':
        event('turn/started', threadId=thread, turn={'id': turn})
        if scenario == 'approval':
            send({'id': 900, 'method': 'item/commandExecution/requestApproval',
                  'params': {'threadId': thread, 'turnId': turn}})
        elif scenario in ('unknown-request', 'wrong-request-thread'):
            send({'id': 900, 'method': 'unknown' if scenario == 'unknown-request' else 'item/tool/requestUserInput',
                  'params': {'threadId': 'wrong' if scenario == 'wrong-request-thread' else thread, 'turnId': turn}})
        if scenario == 'early-events':
            answer('early final')
            finish()
        response(message['id'], {'turn': {'id': turn}})
        if scenario in ('approval', 'unknown-request', 'wrong-request-thread', 'early-events'):
            continue
        if scenario in ('hang', 'cancel'):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            if trace:
                data = json.loads(Path(trace).read_text())
                data['child_pid'] = child.pid
                Path(trace).write_text(json.dumps(data))
            continue
        if scenario == 'progress':
            event('item/started', threadId='foreign', turnId=turn,
                  item={'type': 'commandExecution', 'status': 'inProgress'})
            event('item/started', threadId=thread, turnId=turn,
                  item={'type': 'commandExecution', 'command': 'echo sk-fixture-secret', 'status': 'inProgress'})
            time.sleep(0.4)
            event('item/completed', threadId=thread, turnId=turn,
                  item={'type': 'commandExecution', 'status': 'completed', 'exitCode': 0,
                        'aggregatedOutput': 'sk-fixture-secret'})
        if scenario == 'eof':
            sys.exit(0)
        if scenario == 'failed':
            answer('do not accept partial')
            finish('failed')
        elif scenario == 'empty':
            finish()
        elif scenario == 'unphased':
            answer('legacy final', None)
            finish()
        else:
            answer('wrong thread', th='foreign')
            answer('wrong turn', tid='foreign')
            answer('commentary is private', 'commentary')
            answer('legacy fallback', None)
            answer('final answer')
            finish()
    elif method == 'turn/interrupt':
        response(message['id'], {})
        finish('interrupted')
