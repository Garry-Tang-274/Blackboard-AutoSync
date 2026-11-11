from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bb_sync as bb
from study_queue import StudyQueue
from study_routing import SummaryPolicy
from study_window import today


class SummaryRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        bb.write_json(self.root / 'config.json', dict(base_url='https://example.org',
                      download_dir='files', course_selection_mode='all'))
        self.queue = StudyQueue(self.root)
        self.queue.init()
        self.policy = dict(enabled=True,
                           lecture_course_ids=['ifbs', 'ads', 'bg', 'dst'],
                           guidance_practical_course_ids=['ads'],
                           theory_only_course_ids=['dst'])
        bb.write_json(self.queue.directory / 'routing.json', dict(summary_policy=self.policy,
                      study_window=dict(require_timetable=True)))
        self.router = SummaryPolicy(self.queue.directory)

    def tearDown(self):
        self.temp.cleanup()

    def test_all_four_theory_courses_go_to_lecture(self):
        for course in ['ifbs', 'ads', 'bg', 'dst']:
            with self.subTest(course=course):
                self.assertEqual(self.router.lane(dict(course_id=course, kind='Lecture')), 'lecture')

    def test_only_ads_practical_goes_to_guidance(self):
        self.assertEqual(self.router.lane(dict(course_id='ads', kind='Practical')), 'guidance')
        for course, kind in [('bg', 'Tutorial'), ('ifbs', 'Tutorial'), ('bg', 'Practical'),
                             ('ads', 'Tutorial'), ('other', 'Lecture')]:
            with self.subTest(course=course, kind=kind):
                self.assertIsNone(self.router.lane(dict(course_id=course, kind=kind)))

    def test_dst_calendar_practical_label_never_goes_to_guidance(self):
        self.assertEqual(self.router.lane(dict(course_id='dst', kind='Practical')), 'lecture')

    def sources(self, specs):
        schedule = self.root / 'schedule.txt'
        schedule.write_bytes(b'official schedule')
        events, live, maps = [], {}, {}
        state = self.queue.read()
        for key, course, kind in specs:
            event = dict(id=key, course_id=course, date=today().isoformat(), kind=kind,
                         source=dict(path=str(schedule), sha256=bb.digest(schedule.read_bytes()), reference=key))
            events.append(event)
            item = dict(id=key, course_id=course, paths=[str(self.root / (key + '.pdf'))], sha256='source')
            live[key] = item
            state['jobs'][key] = dict(item, status='pending')
            maps[key] = dict(event_ids=[key])
        bb.write_json(self.queue.path, state)
        bb.write_json(self.queue.directory / 'timetable.json', dict(events=events))
        bb.write_json(self.queue.directory / 'lesson-map.json', maps)
        return live

    def test_review_excludes_other_class_types_but_keeps_sources(self):
        before = (self.root / 'config.json').read_bytes()
        live = self.sources([('a', 'ifbs', 'Lecture'), ('b', 'bg', 'Tutorial'), ('c', 'ads', 'Practical')])
        with patch.object(self.queue, 'current', return_value=live):
            review = self.queue.review()
        self.assertEqual({j['id'] for j in review['ready']}, {'a', 'c'})
        self.assertEqual(len(review['excluded_classes']), 1)
        self.assertEqual(self.queue.read()['jobs']['b']['status'], 'pending')
        self.assertEqual((self.root / 'config.json').read_bytes(), before)

    def test_dst_wrong_lane_is_rejected_at_batch_and_sending(self):
        live = self.sources([('a', 'dst', 'Practical')])
        with patch.object(self.queue, 'current', return_value=live):
            with self.assertRaises(ValueError):
                self.queue.batch('guidance', ['a'])
            batch = self.queue.batch('lecture', ['a'])
        state = self.queue.read()
        state['batches'][batch['batch_id']]['lane'] = 'guidance'
        bb.write_json(self.queue.path, state)
        with self.assertRaises(ValueError):
            self.queue.mark(batch['batch_id'], 'sending')
