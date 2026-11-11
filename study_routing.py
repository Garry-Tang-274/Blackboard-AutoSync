"""User-selected summary scope; independent of Blackboard download selection."""
import json
from pathlib import Path


class SummaryPolicy:
    def __init__(self, directory: Path):
        path = directory / 'routing.json'
        routing = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        self.policy = routing.get('summary_policy', {})
        self.enabled = self.policy.get('enabled', False)

    def includes_course(self, course_id):
        if not self.enabled:
            return True
        return course_id in set(self.policy.get('lecture_course_ids', [])) | set(
            self.policy.get('guidance_practical_course_ids', []))

    def lane(self, event):
        course = event['course_id']
        kind = event.get('kind', '').strip().lower()
        if not self.enabled:
            return 'lecture' if kind in ('lecture', '理论课') else 'guidance'
        if course in self.policy.get('theory_only_course_ids', []):
            # Explicit course policy takes precedence over the timetable's kind.
            return 'lecture'
        if kind in ('lecture', '理论课') and course in self.policy.get('lecture_course_ids', []):
            return 'lecture'
        if kind in ('practical', '实习') and course in self.policy.get('guidance_practical_course_ids', []):
            return 'guidance'
        return None

    def require(self, lane, jobs, window):
        if self.enabled and any(not any(self.lane(e) == lane for e in window.events(job)) for job in jobs):
            raise ValueError('所选课程/课型不在当前自动总结范围内，或接收对话不符合路由设置')
