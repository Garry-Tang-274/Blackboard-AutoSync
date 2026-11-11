"""Verified timetable gates for the current day and the following two calendar days."""
from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path
import hashlib

CHINA = timezone(timedelta(hours=8))
def today():
    return datetime.now(CHINA).date()

class StudyWindow:
    def __init__(self, directory: Path):
        self.directory = directory
        routing = directory / 'routing.json'
        self.policy = json.loads(routing.read_text(encoding='utf-8')).get('study_window', {}) if routing.exists() else {}
        self.enabled = self.policy.get('require_timetable', False)
        table = directory / 'timetable.json'
        mapping = directory / 'lesson-map.json'
        self.table = json.loads(table.read_text(encoding='utf-8')) if table.exists() else {'events': []}
        self.maps = json.loads(mapping.read_text(encoding='utf-8')) if mapping.exists() else {}

    def sessions(self, on=None):
        """All verified classes in the window, independent of file discovery time."""
        if not self.enabled:
            return []
        on = on or today()
        result = []
        hashes = {}
        for event in self.table['events']:
            if event.get('cancelled') or event.get('tentative'):
                continue
            day = date.fromisoformat(event['date'])
            if not on <= day <= on + timedelta(days=2):
                continue
            source = event.get('source', {})
            path = Path(source.get('path', ''))
            if not path.is_file() or not source.get('reference'):
                continue
            if path not in hashes:
                with path.open('rb') as handle:
                    hashes[path] = hashlib.file_digest(handle, 'sha256').hexdigest()
            if hashes[path] != source.get('sha256'):
                continue
            result.append(event)
        return result

    def events(self, job, on=None):
        selected = set(self.maps.get(job['id'], {}).get('event_ids', []))
        return [event for event in self.sessions(on)
                if event['id'] in selected and event['course_id'] == job['course_id']]

    def require(self, jobs, on=None):
        if self.enabled and any(not self.events(job, on) for job in jobs):
            raise ValueError('只允许派发今天及未来两天、且已按课表核实日期的资料；其他文件继续排队')

    def map(self, job_id, event_ids):
        known = {e['id'] for e in self.table['events']}
        if not event_ids or not set(event_ids).issubset(known):
            raise ValueError('映射必须引用课表索引中真实存在的课次 ID')
        self.maps[job_id] = dict(event_ids=sorted(set(event_ids)))
        import bb_sync as bb
        bb.write_json(self.directory / 'lesson-map.json', self.maps)
        return self.maps[job_id]
