"""Persist a user-authorized 15-second mode choice; no background task or permission grant."""
import json, time
from .workflow import Board, atomic, locked
MODES=('quick','standard','long','investigate')

def intake(settings,cwd,task_id,action='read',mode=''):
 board=Board(settings,cwd)
 if task_id not in board.read()['tasks']:raise ValueError('Unknown task')
 if action not in ('start','read','select') or (action=='select' and mode not in MODES):raise ValueError('Invalid mode action')
 path=board.root/'.deepseek-intake.json'
 with locked(board.root/'.deepseek-intake.lock'):
  if path.is_symlink():raise ValueError('Intake cannot be a symlink')
  data=json.loads(path.read_text()) if path.exists() else {}
  if not isinstance(data,dict):raise ValueError('Invalid intake record')
  entry=data.get(task_id)
  if entry is None:
   if action=='read':return {'task_id':task_id,'state':'not_started'}
   current=time.time()
   entry=dict(task_id=task_id,state='waiting',started_at=current,deadline=current+15,mode=None,source=None)
  current=time.time()
  if entry['state']=='waiting' and current>=entry['deadline']:
   entry.update(state='selected',mode='standard',source='timeout')
  if action=='select':
   # A later explicit user choice overrides the default; caller must review an active revision.
   entry.update(state='selected',mode=mode,source='user')
  data[task_id]=entry
  atomic(path,json.dumps(data,ensure_ascii=False,indent=2)+'\n')
  return dict(entry,remaining_seconds=max(0,min(15,int(entry['deadline']-current+0.999))) if entry['state']=='waiting' else 0,
              default_mode='standard',note='Mode only; validation scope, write permissions and destructive actions require actual authorization. No background wake-up; caller must read after deadline.')
