from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bb_sync as bb
from study_queue import StudyQueue
from daily_sync import run


class HandoffReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        bb.write_json(self.root / 'config.json', dict(base_url='https://example.org',
                      download_dir='files', course_selection_mode='all'))
        self.queue = StudyQueue(self.root)
        self.queue.init()
        source = self.root / 'schedule.txt'
        source.write_bytes(b'official W6 on October 27')
        self.event = dict(id='oct27', course_id='_1_1', date='2026-10-27',
                          source=dict(path=str(source), sha256=bb.digest(source.read_bytes()), reference='row 6'))
        bb.write_json(self.queue.directory / 'routing.json', dict(study_window=dict(require_timetable=True)))
        bb.write_json(self.queue.directory / 'timetable.json', dict(events=[self.event]))
        cfg = bb.Config.load(self.root / 'config.json')
        archive = bb.Archive(self.root, cfg)
        archive.save_file(bb.Course('_1_1', 'Test', 'https://example.org/'), ('Week6',),
                          bb.Link('https://example.org/source.pdf'), 'source.pdf', b'%PDF-original', {})
        archive.close()
        self.key = self.queue.scan()['pending'][0]['id']
        state = self.queue.read()
        state['jobs'][self.key]['detected_at'] = '2026-10-23T00:00:00+00:00'
        bb.write_json(self.queue.path, state)

    def tearDown(self):
        self.temp.cleanup()

    def map(self):
        bb.write_json(self.queue.directory / 'lesson-map.json', {self.key: dict(event_ids=['oct27'])})

    def test_early_download_enters_window_even_when_scan_adds_zero(self):
        self.map()
        self.assertEqual(self.queue.scan()['added'], 0)
        self.assertEqual(self.queue.review(date(2026, 10, 24))['ready'], [])
        review = self.queue.review(date(2026, 10, 25))
        self.assertEqual([j['id'] for j in review['ready']], [self.key])
        self.assertEqual(review['ready'][0]['detected_at'], '2026-10-23T00:00:00+00:00')

    def test_unmapped_current_class_is_visible_as_missing_coverage(self):
        review = self.queue.review(date(2026, 10, 27))
        self.assertEqual(review['ready'], [])
        self.assertEqual(len(review['needs_mapping']), 1)
        self.assertTrue(review['class_coverage'][0]['needs_source_review'])

    def test_sent_source_is_covered_without_resending(self):
        self.map()
        state = self.queue.read()
        state['jobs'][self.key].update(status='sent', batch_id='existing')
        bb.write_json(self.queue.path, state)
        review = self.queue.review(date(2026, 10, 27))
        self.assertEqual(review['ready'], [])
        self.assertEqual(review['class_coverage'][0]['sources'][0]['status'], 'sent')

    def test_cancelled_class_and_changed_source_are_not_ready(self):
        self.map()
        self.event['cancelled'] = True
        bb.write_json(self.queue.directory / 'timetable.json', dict(events=[self.event]))
        self.assertEqual(self.queue.review(date(2026, 10, 27))['ready'], [])
        self.event['cancelled'] = False
        bb.write_json(self.queue.directory / 'timetable.json', dict(events=[self.event]))
        Path(self.event['source']['path']).write_bytes(b'changed schedule')
        self.assertEqual(self.queue.review(date(2026, 10, 27))['ready'], [])

    def test_not_due_sync_still_reviews_handoff_without_opening_browser(self):
        from study_window import today
        self.map()
        bb.write_json(self.queue.directory / 'daily-sync.json', dict(handled_date=today().isoformat(),
                      handled_schedule_time='20:00', installed_date='2026-10-20'))
        with patch('daily_sync.subprocess.run', side_effect=AssertionError('must not sync again')):
            result = run(self.root)
        self.assertFalse(result['due'])
        self.assertTrue(Path(result['handoff_review_path']).is_file())
