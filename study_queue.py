"""Local durable handoff queue. Sending a batch is distinct from completing a PDF."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import uuid
import bb_sync as bb
from study_window import StudyWindow, today
from study_routing import SummaryPolicy
from datetime import timedelta

class StudyQueue:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = self.root / '.state' / 'study-handoff'
        self.path = self.directory / 'state.json'
        self.cfg = bb.Config.load(self.root / 'config.json')

    def read(self):
        if not self.path.exists():
            raise RuntimeError('请先执行 init 建立已有资料基线，不能将全部旧课件当作新增')
        return json.loads(self.path.read_text(encoding='utf-8'))

    def current(self):
        if not (self.root / '.state' / 'manifest.sqlite3').exists():
            return {}
        archive = bb.Archive(self.root, self.cfg)
        records = {}
        try:
            courses = {row['id']: row['name'] for row in archive.db.execute('SELECT * FROM courses')}
            for row in archive.db.execute('SELECT * FROM files').fetchall():
                if not bb.course_selected(self.cfg, bb.Course(row['course_id'], courses.get(row['course_id'], ''), '')):
                    continue
                path = (archive.output / row['relative_path']).resolve()
                if not path.is_relative_to(archive.output.resolve()):
                    raise ValueError('来源文件超出资料根目录')
                if not archive.valid(row):
                    continue
                key = bb.digest(row['course_id'] + '\n' + row['sha256'])
                item = records.setdefault(key, dict(id=key, course_id=row['course_id'], course_name=courses.get(row['course_id'], ''),
                                                   sha256=row['sha256'], size=row['size'], paths=[]))
                if str(path) not in item['paths']:
                    item['paths'].append(str(path))
        finally:
            archive.close()
        return records

    def init(self):
        if self.path.exists():
            return dict(initialized=False, known=len(self.read()['known']))
        current = self.current()
        state = dict(schema='blackboard-study-handoff/v1', initialized_at=bb.now(), known=list(current), jobs={}, batches={})
        bb.write_json(self.directory / 'baseline.json', dict(created_at=bb.now(), files=list(current.values())))
        bb.write_json(self.path, state)
        return dict(initialized=True, known=len(current), baseline_paths=sum(len(x['paths']) for x in current.values()))

    def scan(self):
        state, current = self.read(), self.current()
        known = set(state['known'])
        added = []
        for key, item in current.items():
            if key not in known:
                state['jobs'][key] = dict(item, status='pending', detected_at=bb.now())
                known.add(key)
                added.append(key)
            elif key in state['jobs'] and state['jobs'][key]['status'] == 'pending':
                state['jobs'][key]['paths'] = item['paths']
        for key, job in state['jobs'].items():
            if job['status'] == 'pending' and key not in current:
                job.update(status='superseded', reason='来源已经更新、移除或校验失败，不派发过期内容')
        state['known'] = sorted(known)
        state['last_scan_at'] = bb.now()
        bb.write_json(self.path, state)
        return dict(added=len(added), pending=[v for v in state['jobs'].values() if v['status'] == 'pending'])

    def batch(self, lane, ids):
        state = self.read()
        if lane not in ('lecture', 'guidance') or not ids or len(set(ids)) != len(ids):
            raise ValueError('请选择 lecture/guidance，并提供不重复的文件 ID')
        jobs = [state['jobs'][key] for key in ids]
        if any(job['status'] != 'pending' for job in jobs):
            raise ValueError('批次只接受尚未派发的文件')
        window = StudyWindow(self.directory)
        window.require(jobs)
        policy = SummaryPolicy(self.directory)
        policy.require(lane, jobs, window)
        live = self.current()
        if any(job['id'] not in live for job in jobs):
            raise ValueError('来源文件已经变更或不可用，请重新核对')
        for job in jobs:
            job['paths'] = live[job['id']]['paths']
        batch_id = uuid.uuid4().hex
        peers = {str(Path(p).parent) for job in jobs for p in job['paths']}
        related = [item for item in live.values() if item['id'] not in ids and any(str(Path(p).parent) in peers for p in item['paths'])]
        events = [event for job in jobs for event in window.events(job)]
        if policy.enabled:
            events = [event for event in events if policy.lane(event) == lane]
        if window.enabled:
            event_ids = {event['id'] for event in events}
            related = [item for item in live.values() if item['id'] not in ids and any(e['id'] in event_ids for e in window.events(item))]
        receipt = self.directory / 'receipts' / (batch_id + '.json')
        payload = dict(batch_id=batch_id, lane=lane, created_at=bb.now(), files=jobs, related_files=related,
                       receipt_path=str(receipt), source_root=str(self.cfg.output(self.root)),
                       archive_destination=dict(provider='google_drive', root_name='Year 2',
                                                use_existing_course_unit_folder=True,
                                                preserve_existing_file_ids=True),
                       class_sessions=events)
        batch_path = self.directory / 'batches' / (batch_id + '.json')
        bb.write_json(batch_path, payload)
        state['batches'][batch_id] = dict(lane=lane, status='prepared', path=str(batch_path), receipt_path=str(receipt), source_ids=ids)
        for job in jobs:
            job.update(status='prepared', batch_id=batch_id)
        bb.write_json(self.path, state)
        return dict(batch_id=batch_id, path=str(batch_path), lane=lane)

    def review(self, on=None):
        """Review every pending source against classes, even when scan.added is zero."""
        on = on or today()
        state, live = self.read(), self.current()
        window = StudyWindow(self.directory)
        policy = SummaryPolicy(self.directory)
        all_sessions = window.sessions(on)
        sessions = [e for e in all_sessions if not policy.enabled or policy.lane(e)]
        ready, unmapped, deferred, unavailable = [], [], [], []
        excluded = []
        for key, job in state['jobs'].items():
            if job['status'] != 'pending':
                continue
            if key not in live:
                unavailable.append(key)
                continue
            item = dict(job, paths=live[key]['paths'])
            matches = window.events(item, on)
            if policy.enabled:
                allowed = [event for event in matches if policy.lane(event)]
                if not policy.includes_course(job['course_id']) or (matches and not allowed):
                    excluded.append(dict(item, reason='用户未选择总结此课程/课堂类型；继续正常下载'))
                    continue
                matches = allowed
            if matches or not window.enabled:
                ready.append(dict(item, class_sessions=matches,
                                  allowed_lanes=sorted({policy.lane(e) for e in matches if policy.lane(e)})))
            elif not window.maps.get(key, {}).get('event_ids'):
                unmapped.append(item)
            else:
                deferred.append(key)
        coverage = []
        for event in sessions:
            mapped = []
            for key, mapping in window.maps.items():
                if (event['id'] not in mapping.get('event_ids', []) or key not in live
                        or live[key]['course_id'] != event['course_id']):
                    continue
                job = state['jobs'].get(key, {})
                mapped.append(dict(source_id=key, status=job.get('status', 'baseline'),
                                   batch_id=job.get('batch_id')))
            coverage.append(dict(event=event, expected_lane=policy.lane(event), sources=mapped,
                                 needs_source_review=not mapped))
        result = dict(reviewed_at=bb.now(), window_start=on.isoformat(),
                      window_end=(on + timedelta(days=2)).isoformat(),
                      selection='all_pending_in_window_regardless_of_detection_date',
                      ready=ready, needs_mapping=unmapped, deferred=deferred,
                      unavailable=unavailable, excluded_by_policy=excluded,
                      excluded_classes=[e for e in all_sessions if policy.enabled and not policy.lane(e)],
                      class_coverage=coverage)
        bb.write_json(self.directory / ('review-' + on.isoformat() + '.json'), result)
        return result

    def mark(self, batch_id, status):
        state = self.read()
        batch = state['batches'][batch_id]
        allowed = {'sending': {'prepared'}, 'sent': {'sending'}}
        if status not in allowed or batch['status'] not in allowed[status]:
            raise ValueError('批次状态转换无效；不能把派发标记当作已完成')
        if status == 'sending':
            window = StudyWindow(self.directory)
            jobs = [state['jobs'][key] for key in batch['source_ids']]
            window.require(jobs)
            SummaryPolicy(self.directory).require(batch['lane'], jobs, window)
        batch.update(status=status, updated_at=bb.now())
        for key in batch['source_ids']:
            state['jobs'][key]['status'] = status
        bb.write_json(self.path, state)
        return dict(batch_id=batch_id, status=status)

    def skip(self, ids, reason):
        if not reason.strip():
            raise ValueError('跳过需要记录原因')
        state = self.read()
        for key in ids:
            if state['jobs'][key]['status'] != 'pending':
                raise ValueError('只能跳过尚未派发的文件')
        for key in ids:
            state['jobs'][key].update(status='skipped', reason=reason)
        bb.write_json(self.path, state)
        return dict(skipped=len(ids))

    def reconcile(self):
        state = self.read()
        verified = []
        awaiting_drive = []
        for key, batch in state['batches'].items():
            receipt = Path(batch['receipt_path'])
            if batch['status'] == 'completed' or not receipt.exists():
                continue
            result = json.loads(receipt.read_text(encoding='utf-8'))
            if result.get('status') != 'completed':
                continue
            if result.get('batch_id') != key or set(result.get('source_ids', [])) != set(batch['source_ids']):
                raise ValueError('回执批次或来源 ID 不匹配')
            if not result.get('visual_qa_passed'):
                raise ValueError('回执缺少已校对的 PDF 成果')
            # Local PDFs are optional QA copies, never the archive destination.
            for output in result.get('outputs', []):
                path = Path(output['path']).resolve()
                if path.suffix.lower() != '.pdf':
                    raise ValueError('本地校对副本必须是 PDF')
                with bb.filesystem_path(path).open('rb') as handle:
                    if handle.read(5) != b'%PDF-':
                        raise ValueError('成果不是 PDF')
                if bb.digest(bb.filesystem_path(path).read_bytes()) != output['sha256']:
                    raise ValueError('成果校验失败')
            cloud = result.get('drive_outputs', [])
            if not cloud:
                awaiting_drive.append(key)
                continue
            ids = [output.get('file_id') for output in cloud]
            if not all(ids) or len(ids) != len(set(ids)) or any(not x.get('folder_id') or not x.get('url') for x in cloud):
                raise ValueError('Drive 回执缺少文件 ID、归档目录 ID 或链接')
            # The coordinator writes this evidence only after native connector readback.
            proof_path = self.directory / 'drive-verifications' / (key + '.json')
            if not proof_path.exists():
                awaiting_drive.append(key)
                continue
            proof = json.loads(proof_path.read_text(encoding='utf-8'))
            if (proof.get('batch_id') != key or set(proof.get('source_ids', [])) != set(batch['source_ids'])
                    or proof.get('method') != 'google_drive.get_file_metadata' or not proof.get('checked_at')):
                raise ValueError('Drive 归档读回证据与批次不匹配')
            entries = proof.get('outputs', [])
            if len(entries) != len(cloud) or {x.get('file_id') for x in entries} != set(ids):
                raise ValueError('Drive 归档读回证据未覆盖全部 PDF')
            for output in cloud:
                entry = next(x for x in entries if x['file_id'] == output['file_id'])
                meta, folder = entry.get('file_metadata', {}), entry.get('folder_metadata', {})
                if (meta.get('id') != output['file_id'] or meta.get('mimeType') != 'application/pdf'
                        or meta.get('trashed') is not False or int(meta.get('size', 0)) <= 0
                        or output['folder_id'] not in meta.get('parents', [])
                        or folder.get('id') != output['folder_id']
                        or folder.get('mimeType') != 'application/vnd.google-apps.folder'
                        or folder.get('trashed') is not False
                        or (output.get('name') and meta.get('name') != output['name'])):
                    raise ValueError('Drive PDF 或归档目录读回校验失败')
            batch.update(status='completed', completed_at=bb.now(), outputs=result.get('outputs', []),
                         drive_outputs=cloud, drive_verification_path=str(proof_path))
            for job_id in batch['source_ids']:
                state['jobs'][job_id]['status'] = 'completed'
            verified.append(key)
        bb.write_json(self.path, state)
        return dict(verified_completed=verified, awaiting_drive_verification=awaiting_drive)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init','scan','review','status','batch','sending','sent','skip','reconcile','map'])
    parser.add_argument('--root', type=Path, default=bb.ROOT)
    parser.add_argument('--lane', choices=['lecture','guidance'])
    parser.add_argument('--ids', nargs='+', default=[])
    parser.add_argument('--batch-id')
    parser.add_argument('--reason', default='')
    parser.add_argument('--event-ids', nargs='+', default=[])
    args = parser.parse_args()
    with bb.single_instance(args.root):
        queue = StudyQueue(args.root)
        if args.command == 'map':
            if len(args.ids) != 1: raise ValueError('每次映射一个来源文件 ID')
            result = StudyWindow(queue.directory).map(args.ids[0], args.event_ids)
        elif args.command == 'batch': result = queue.batch(args.lane, args.ids)
        elif args.command in ('sending','sent'): result = queue.mark(args.batch_id, args.command)
        elif args.command == 'skip': result = queue.skip(args.ids, args.reason)
        elif args.command == 'status': result = queue.read()
        else: result = getattr(queue, args.command)()
        print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
