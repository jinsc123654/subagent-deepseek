"""Validation scope and evidence layout; instructions, not an execution sandbox."""
from pathlib import PurePosixPath, Path

MODES = ('inherit', 'smoke', 'targeted', 'full', 'stability', 'none', 'method-only')

def validation_plan(value=None):
    if value is None:
        return {'mode': 'inherit', 'timing': 'this-round', 'budget_minutes': 0, 'resources': []}
    if not isinstance(value, dict) or set(value) - {'mode', 'timing', 'budget_minutes', 'resources'}:
        raise ValueError('Invalid validation plan')
    mode=value.get('mode', 'inherit'); timing=value.get('timing', 'this-round')
    budget=value.get('budget_minutes', 0); resources=value.get('resources', [])
    if mode not in MODES or timing not in ('this-round', 'final'):
        raise ValueError('Invalid validation mode or timing')
    if type(budget) is not int or not 0 <= budget <= 10080:
        raise ValueError('Invalid validation time budget')
    if not isinstance(resources, list) or len(resources)>16 or not all(isinstance(x,str) and 0<len(x)<=256 for x in resources):
        raise ValueError('Invalid validation resources')
    if mode in ('none', 'method-only') and budget:
        raise ValueError('Skipped tests cannot reserve runtime')
    return dict(mode=mode, timing=timing, budget_minutes=budget, resources=list(resources))

def evidence_root(value='docs/agent/evidence'):
    if not isinstance(value,str) or not value or len(value)>512 or '\\' in value or any(ord(c)<32 for c in value):
        raise ValueError('Invalid evidence root')
    p=PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or ':' in value or str(p)=='.':
        raise ValueError('Evidence root must be project relative')
    return str(p)

def evidence_directory(cwd, root, task_id, revision):
    root=evidence_root(root)
    if not isinstance(task_id,str) or not task_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in task_id):
        raise ValueError('Invalid evidence task identifier')
    base=Path(cwd).resolve(); target=base/root/task_id/f'R{revision}'
    if not target.resolve().is_relative_to(base):
        raise ValueError('Evidence path escapes project through symlink')
    return str(target)

def verify_checks(report, plan):
    checks=report.get('checks', [])
    if not isinstance(checks,list) or len(checks)>64:
        raise ValueError('Invalid checks report')
    if plan['mode'] != 'inherit' and not checks:
        raise ValueError('Explicit validation requires itemized checks')
    for check in checks:
        if not isinstance(check,dict) or not all(isinstance(check.get(k),str) and len(check[k])<=4000 for k in ('name','status','evidence')):
            raise ValueError('Invalid check result')
        if not check['name'].strip() or check['status'] not in ('PASS','FAIL','BLOCKED','NOT_RUN','SUBSTITUTED'):
            raise ValueError('Invalid check status')
        if check['status']=='PASS' and not check['evidence'].strip():
            raise ValueError('PASS requires evidence')
        if (plan['mode'] in ('none','method-only') or plan['timing']=='final') and check['status']!='NOT_RUN':
            raise ValueError('Unselected or deferred checks must be NOT_RUN')
    return checks
