"""Single-file project board, explicit review gate, and idempotent policy installation."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

NAME = 'DEEPSEEK_PROJECT.md'
BEGIN = '<!-- subagent-deepseek workflow begin -->'
END = '<!-- subagent-deepseek workflow end -->'
STATES = ('pending', 'in_progress', 'review', 'done', 'blocked')
LABELS = dict(zip(STATES, ('待执行', '执行中', '待审阅', '已完成', '受阻')))
POLICY = Path(__file__).with_name('workflow_policy.md').read_text()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def atomic(path, text):
    if path.is_symlink():
        raise ValueError('Refusing symbolic-link output')
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def install_policy(path):
    """Append/update only our marked instructions, preserving and backing up existing text."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name('.' + path.name + '.deepseek.lock')
    with locked(lock):
        text = path.read_text() if path.exists() else ''
        block = BEGIN + '\n' + POLICY + '\n' + END
        if BEGIN in text or END in text:
            if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) > text.index(END):
                raise ValueError('Malformed existing workflow block; no instructions changed')
            updated = text[:text.index(BEGIN)] + block + text[text.index(END) + len(END):]
        else:
            updated = text + ('\n\n' if text else '') + block + '\n'
        if updated != text:
            if path.exists():
                atomic(path.with_name(path.name + '.before-deepseek-' + uuid4().hex[:8] + '.bak'), text)
            atomic(path, updated)
    return str(path)


@contextmanager
def locked(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


class Board:
    def __init__(self, settings, cwd):
        self.root = settings.directory(cwd)
        self.path = self.root / NAME

    def read(self):
        if self.path.is_symlink():
            raise ValueError('Project board must not be a symlink')
        if not self.path.exists():
            raise ValueError('Initialize the project board first')
        text = self.path.read_text()
        match = re.search(r'<!-- deepseek-board-v1\n(.*?)\n-->', text, re.S)
        if not match:
            raise ValueError('Invalid board; preserve it and repair manually before updating')
        data = json.loads(match[1])
        if data.get('version') != 1 or not isinstance(data.get('tasks'), dict):
            raise ValueError('Unsupported project board format')
        return data

    def write(self, data):
        data['updated_at'] = now()
        tasks = list(data['tasks'].values())
        complete = sum(t['state'] == 'done' for t in tasks)
        percent = round(100 * complete / len(tasks)) if tasks else 0
        lines = ['# DeepSeek 项目规划与进度', '',
                 f"更新时间：{data['updated_at']}",
                 f'验收完成度：{complete}/{len(tasks)}（{percent}%）；按任务计数，非工时估算。', '',
                 '主 Agent：规划、审阅、验收。DeepSeek：执行。执行返回后进入待审阅。', '',
                 '> 本文件由 MCP 维护；请通过 project_board 更新，避免覆盖结构化状态。', '']
        for task in tasks:
            lines += [f"## {task['id']} · {task['title']}", '',
                      f"类型：{task['kind']} | 状态：{LABELS[task['state']]}",
                      f"执行进度（估计）：{task['progress']}%", '',
                      '### 当前交接契约',
                      f"修订：{task.get('revision', 0)} | 类型：{task.get('contract', {}).get('kind', '未分发')}",
                      '主 Agent 决定：' + task.get('current_decision', '未记录'),
                      '```json', json.dumps(task.get('contract', {}), ensure_ascii=False, indent=2), '```', '',
                      '### 分发轮次',
                      *[f"- r{r['revision']} {r['state']} · {r.get('backend', '')} · {r.get('started_at', '')} · 证据：{r.get('evidence') or '待验收'}" for r in task.get('rounds', [])], '',
                      '### 规划 / Bug 描述', task['description'], '',
                      '### 验收条件', task['acceptance'], '',
                      '### 执行报告（待主 Agent 核验）', task.get('result_summary') or '尚无结果', '',
                      '### 验证证据', task.get('evidence') or '尚未验收', '',
                      '### 异常点', task.get('anomaly') or '无记录', '',
                      '### 变更记录']
            for event in task.get('history', []):
                lines.append('- ' + event['at'] + ' ' + event['note'])
            lines += ['']
        lines += ['<!-- deepseek-board-v1', json.dumps(data, ensure_ascii=False, indent=2), '-->', '']
        atomic(self.path, '\n'.join(lines))

    def change(self, action, task_id='', title='', description='', acceptance='', kind='task',
               state='', progress=-1, evidence='', anomaly='', note=''):
        if action == 'read':
            initialized = self.path.exists() or self.path.is_symlink()
            data = self.read() if initialized else {'version': 1, 'tasks': {}}
            tasks = data['tasks']
            return dict(path=str(self.path), task_id=task_id or None, tasks=tasks,
                        initialized=initialized,
                        completed=sum(t['state'] == 'done' for t in tasks.values()), total=len(tasks))
        with locked(self.root / '.deepseek-project.lock'):
            if action == 'init' and not self.path.exists():
                data = {'version': 1, 'tasks': {}, 'updated_at': now()}
                self.write(data)
            else:
                data = self.read()
            if action == 'add':
                if not title.strip() or not description.strip() or not acceptance.strip():
                    raise ValueError('title, description and acceptance are required')
                if kind not in ('task', 'bug', 'plan'):
                    raise ValueError('kind must be task, bug or plan')
                task_id = 'T-' + uuid4().hex[:10]
                data['tasks'][task_id] = dict(id=task_id, title=title, description=description,
                    acceptance=acceptance, kind=kind, state='pending', progress=0,
                    evidence='', anomaly='', history=[dict(at=now(), note='主 Agent 建立任务')])
                self.write(data)
            elif action == 'update':
                task = data['tasks'].get(task_id)
                if task is None:
                    raise ValueError('Unknown task_id')
                if state and state not in STATES:
                    raise ValueError('Invalid state')
                if progress != -1 and not 0 <= progress <= 100:
                    raise ValueError('progress must be 0–100')
                if state == 'done' and not evidence.strip():
                    raise ValueError('Main-agent review evidence is required to mark done')
                if task.get('dispatching'):
                    raise ValueError('Dispatch is pending; wait for receipt before updating')
                for key, value in dict(title=title, description=description, acceptance=acceptance,
                                       state=state, evidence=evidence, anomaly=anomaly).items():
                    if value:
                        task[key] = value
                if progress >= 0:
                    task['progress'] = progress
                if state == 'done':
                    task['progress'] = 100
                if task.get('rounds'):
                    task['rounds'][-1].update(state=task['state'], evidence=task.get('evidence', ''))
                task['history'].append(dict(at=now(), note=note or ('主 Agent 更新：' + (state or '记录'))))
                self.write(data)
            elif action not in ('init', 'read'):
                raise ValueError('action must be init, read, add or update')
            tasks = data['tasks']
            return dict(path=str(self.path), task_id=task_id or None, tasks=tasks,
                        completed=sum(t['state'] == 'done' for t in tasks.values()), total=len(tasks))

    def reserve(self, task_id, backend, contract=None, decision=''):
        with locked(self.root / '.deepseek-project.lock'):
            data = self.read()
            task = data['tasks'].get(task_id)
            if not task or task['state'] not in ('pending', 'blocked', 'review'):
                raise ValueError('Task must be pending, blocked or review before dispatch')
            if task.get('dispatching') or task.get('uncertain'):
                raise ValueError('Previous dispatch unresolved; inspect and clear explicitly first')
            if task.get('backend') != backend:
                task.pop('session_id', None)
            revision = task.get('revision', 0) + 1
            task.update(state='in_progress', dispatching=True, uncertain=True, backend=backend, progress=0,
                        revision=revision, contract=contract or {}, evidence='', result_summary='', anomaly='', coord_report_wait_since=0)
            if decision:
                task['current_decision'] = decision
            task.setdefault('rounds', []).append(dict(revision=revision, backend=backend, contract=contract or {},
                decision=task.get('current_decision', ''), state='in_progress', started_at=now()))
            task['history'].append(dict(at=now(), note=f'开始下发 DeepSeek r{revision}'))
            self.write(data)
            return dict(task)

    def execution(self, task_id, expected_session=None, expected_revision=None, **fields):
        with locked(self.root / '.deepseek-project.lock'):
            data = self.read()
            task = data['tasks'][task_id]
            if task['state'] == 'done' or (expected_session is not None and task.get('session_id') != expected_session) or (expected_revision is not None and task.get('revision') != expected_revision):
                return
            task.update(fields, dispatching=False)
            if task.get('rounds'):
                task['rounds'][-1].update(fields, updated_at=now())
            if task['state'] == 'done':
                raise ValueError('Execution cannot mark a task done')
            task['history'].append(dict(at=now(), note='执行状态：' + task['state']))
            self.write(data)

    def resolve_uncertain(self, task_id, note):
        if not note.strip():
            raise ValueError('Inspection evidence is required')
        with locked(self.root / '.deepseek-project.lock'):
            data = self.read(); task = data['tasks'][task_id]
            if not task.get('uncertain'):
                raise ValueError('Task has no uncertain dispatch')
            task.update(uncertain=False, dispatching=False, state='blocked')
            task['history'].append(dict(at=now(), note='主 Agent 核查含糊提交：' + note))
            self.write(data)
            return dict(path=str(self.path), task=task)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--agents-file', type=Path, required=True)
    args = parser.parse_args()
    print('Workflow instructions installed:', install_policy(args.agents_file))


if __name__ == '__main__':
    main()
