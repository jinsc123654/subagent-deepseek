"""Persist bounded coordinator memory and validate reported workers against native ancestry."""
import json
import re
from .workflow import atomic, locked, now

NAME = 'DEEPSEEK_COORDINATION.md'
MARKER = '<!-- deepseek-coordination-v1\n'


def parse_report(text):
    matches = re.findall(r'<coordination_report>\s*(.*?)\s*</coordination_report>', text, re.S)
    if len(matches) != 1:
        raise ValueError('Coordinator must return a structured coordination_report')
    data = json.loads(matches[0])
    if not isinstance(data, dict) or not isinstance(data.get('memory'), str) or not data['memory'].strip() or len(data['memory']) > 16000:
        raise ValueError('Invalid coordinator memory')
    for field in ('decisions', 'risks', 'next_steps'):
        values = data.setdefault(field, [])
        if not isinstance(values, list) or len(values) > 32 or not all(isinstance(x, str) and len(x) <= 4000 for x in values):
            raise ValueError('Invalid coordinator summary field')
    children = data.get('children')
    if not isinstance(children, list) or not 1 <= len(children) <= 16:
        raise ValueError('Coordinator must identify its actual native workers')
    seen = set()
    for child in children:
        if not isinstance(child, dict) or not all(isinstance(child.get(k), str) and len(child[k]) <= 4000 for k in ('id', 'task', 'status', 'evidence')):
            raise ValueError('Invalid child report')
        if not child['id'] or child['id'] in seen:
            raise ValueError('Duplicate or empty worker id')
        seen.add(child['id'])
        if child['status'] == 'PASS' and not child['evidence'].strip():
            raise ValueError('Worker PASS requires evidence')
        if child['status'] not in ('PASS', 'FAIL', 'BLOCKED', 'NOT_RUN', 'SUBSTITUTED'):
            raise ValueError('Invalid child status')
    return data


def verify_report(report, tree, existing_ids=(), max_workers=4, baseline_seq=None):
    nodes = tree['nodes']
    direct = {n['id']: n for n in nodes if n['depth'] == 1}
    if tree.get('truncated') or tree.get('errors'):
        raise ValueError('Cannot verify complete native worker ancestry')
    if any(n['depth'] > 1 for n in nodes):
        raise ValueError('Fourth-layer delegation detected')
    if any(n['mode'] not in ('continuable', 'one-shot') for n in nodes):
        raise ValueError('Only local native Harness workers are allowed')
    if len(set(direct) - set(existing_ids)) > max_workers:
        raise ValueError('Worker creation budget exceeded')
    if not direct:
        raise ValueError('No native worker was observed; prose is not delegation evidence')
    for child in report['children']:
        node = direct.get(child['id'])
        if node is None:
            raise ValueError('Reported worker has no native parent-child relation')
        if child['status'] == 'PASS' and child['id'] in existing_ids:
            before = (baseline_seq or {}).get(child['id'])
            after = node.get('seq')
            if type(before) is not int or type(after) is not int or after <= before:
                raise ValueError('Worker has no observed new activity in this revision')
        if child['status'] == 'PASS' and node.get('completed') is not True:
            raise ValueError('Worker success is not confirmed by native turn state')
    if set(direct) - {c['id'] for c in report['children']} - set(existing_ids):
        raise ValueError('Coordinator omitted native workers from its report')
    return True


class Coordination:
    def __init__(self, settings, cwd):
        self.root = settings.directory(cwd)
        self.path = self.root / NAME

    def read(self):
        if self.path.is_symlink():
            raise ValueError('Coordination file cannot be a symlink')
        if not self.path.exists():
            return {'version': 1, 'tasks': {}}
        text = self.path.read_text()
        start = text.rfind(MARKER)
        end = text.rfind('\n-->')
        if start < 0 or end <= start or text[end + len('\n-->'):].strip():
            raise ValueError('Existing coordination Markdown is not managed; preserve it')
        data = json.loads(text[start + len(MARKER):end])
        if data.get('version') != 1 or not isinstance(data.get('tasks'), dict):
            raise ValueError('Invalid coordination format')
        return data

    def context(self, task_id):
        task = self.read()['tasks'].get(task_id, {})
        rounds = task.get('rounds', {})
        if not rounds:
            return ''
        latest = rounds[max(rounds, key=int)]
        summary = latest.get('report', {}).get('memory') or latest.get('memory') or ''
        return json.dumps({'prior_revision': latest['revision'], 'memory': summary,
                           'risks': latest.get('report', {}).get('risks', []),
                           'next_steps': latest.get('report', {}).get('next_steps', []),
                           'checks': latest.get('report', {}).get('checks', []),
                           'workers': latest.get('observed_children', [])}, ensure_ascii=False)[:24000]

    def record(self, task_id, revision, **fields):
        with locked(self.root / '.deepseek-coordination.lock'):
            data = self.read()
            task = data['tasks'].setdefault(task_id, {'rounds': {}})
            entry = task['rounds'].setdefault(str(revision), {'revision': revision})
            entry.update(fields, updated_at=now())
            lines = ['# DeepSeek 协调层记忆与子任务', '',
                     'Codex：顶层决策与验收。DeepSeek：协调、记忆与分发。worker：执行，不继续委派。',
                     '原生父子关系由程序观察；memory和测试结论为协调层报告，仍待Codex审查。', '']
            for tid, item in data['tasks'].items():
                lines += ['## 工作包 ' + tid, '']
                for key in sorted(item['rounds'], key=int):
                    value = item['rounds'][key]; report = value.get('report', {})
                    lines += [f"### 修订 {key} · {value.get('state', 'in_progress')}",
                              '协调会话：' + str(value.get('session_id', '待提交')),
                              'Codex决定：' + value.get('decision', '未记录'), '',
                              '#### 阶段记忆', report.get('memory') or value.get('memory') or '待回传', '',
                              '#### 原生子任务关系']
                    for child in value.get('observed_children', []):
                        lines.append(f"- {child['id']} · parent={child['parent_id']} · depth={child['depth']} · completed={child.get('completed')} · {child.get('label', '')}")
                    lines += ['#### 验证计划', json.dumps(value.get('contract', {}).get('validation', {}), ensure_ascii=False),
                              '#### 检查与证据索引', json.dumps(report.get('checks', []), ensure_ascii=False, indent=2),
                              '#### 子任务报告', json.dumps(report.get('children', []), ensure_ascii=False, indent=2),
                              '#### 异常与待决问题', value.get('anomaly') or '无新增记录',
                              *report.get('risks', []), '#### 下一步', *report.get('next_steps', []), '']
            lines += [MARKER.rstrip('\n'), json.dumps(data, ensure_ascii=False, indent=2), '-->', '']
            atomic(self.path, '\n'.join(lines))
        return str(self.path)
