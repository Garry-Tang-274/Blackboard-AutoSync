"""One successful/handled sync per Shanghai calendar day, including logon catch-up."""
from datetime import datetime, timedelta
import argparse
import json
from pathlib import Path
import subprocess
import sys
import bb_sync as bb
from study_window import CHINA

def is_due(now, state):
    if state.get('handled_date') == now.date().isoformat():
        return state.get('handled_schedule_time', '20:00') != '20:00' and now.hour >= 20
    if now.hour >= 20:
        return True
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    return bool(state.get('installed_date')) and state['installed_date'] <= yesterday and state.get('handled_date', '') < yesterday

def run(root, dry_run=False):
    state_path = root / '.state/study-handoff/daily-sync.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    now = datetime.now(CHINA)
    if not state.get('installed_date'):
        state['installed_date'] = now.date().isoformat()
    due = is_due(now, state)
    if dry_run:
        return dict(due=due, local_date=now.date().isoformat(), action='read-only check')
    if not due:
        # Download de-duplication must not skip newly eligible classroom work.
        from study_queue import StudyQueue
        with bb.single_instance(root):
            queue = StudyQueue(root)
            if not queue.path.exists():
                return dict(due=False, reason='尚未到同步时间，学习资料基线尚未初始化')
            review = queue.review(now.date())
        return dict(due=False, reason='今天已处理或尚未到 20:00 且没有错过的运行',
                    ready_handoff_count=len(review['ready']),
                    unmapped_handoff_count=len(review['needs_mapping']),
                    handoff_review_path=str(queue.directory / ('review-' + now.date().isoformat() + '.json')))
    logs = root / '.state/logs'
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / ('daily-sync-' + now.strftime('%Y%m%d-%H%M%S') + '.log')
    report_path = root / 'reports/latest.json'
    before = json.loads(report_path.read_text(encoding='utf-8')).get('run_id') if report_path.exists() else None
    state.update(started_at=bb.now(), status='running', log_path=str(log))
    bb.write_json(state_path, state)
    env = __import__('os').environ.copy()
    env['PYTHONUTF8'] = '1'
    with log.open('w', encoding='utf-8') as output:
        process = subprocess.run([sys.executable, str(root/'bb_sync.py'), 'sync'], cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT)
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    if report.get('run_id') == before:
        state.update(status='failed', finished_at=bb.now(), message='同步没有产生新报告，可能已有其他任务运行')
        bb.write_json(state_path, state)
        return state
    if report.get('status') == 'failed' and any('ERR_CONNECTION_CLOSED' in i.get('message','') for i in report.get('issues', [])):
        with log.open('a', encoding='utf-8') as output:
            subprocess.run([sys.executable, str(root/'bb_sync.py'), 'sync', '--headed'], cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT)
        report = json.loads(report_path.read_text(encoding='utf-8'))
    from study_queue import StudyQueue
    queue = StudyQueue(root)
    with bb.single_instance(root):
        delta = queue.scan()
        review = queue.review(now.date())
    state.update(status=report.get('status','failed'), finished_at=bb.now(), run_id=report.get('run_id'),
                 added=delta['added'], pending_count=len(delta['pending']), pending_handoff=True,
                 ready_handoff_count=len(review['ready']), unmapped_handoff_count=len(review['needs_mapping']),
                 handoff_review_path=str(queue.directory / ('review-' + now.date().isoformat() + '.json')))
    if report.get('status') in ('synced','partial','paused','auth_required'):
        state['handled_date'] = now.date().isoformat()
        state['handled_schedule_time'] = '20:00'
    bb.write_json(state_path, state)
    return state

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args=parser.parse_args()
    try:
        with bb.single_instance(bb.ROOT/'.state/daily-controller'):
            print(json.dumps(run(bb.ROOT, args.dry_run), ensure_ascii=False, indent=2))
    except RuntimeError as exc:
        print(str(exc))
