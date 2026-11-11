from datetime import date,datetime
from pathlib import Path
import tempfile
import unittest
import bb_sync as bb
from study_window import StudyWindow,CHINA
from daily_sync import is_due

class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)
        self.source=self.path/'timetable.txt'
        self.source.write_bytes(b'official date')
        bb.write_json(self.path/'routing.json',dict(study_window=dict(require_timetable=True)))
        self.events=[dict(id=str(i),course_id='c1',date=f'2026-10-{20+i}',source=dict(path=str(self.source),sha256=bb.digest(self.source.read_bytes()),reference='row 1')) for i in range(4)]
        bb.write_json(self.path/'timetable.json',dict(events=self.events))
        self.job=dict(id='file',course_id='c1')
    def tearDown(self): self.temp.cleanup()
    def window(self, ids):
        bb.write_json(self.path/'lesson-map.json',{'file':dict(event_ids=ids)})
        return StudyWindow(self.path)
    def test_today_and_next_two_days_only(self):
        window=self.window(['0','1','2','3'])
        self.assertEqual(len(window.events(self.job,date(2026,10,20))),3)
        with self.assertRaises(ValueError): self.window(['3']).require([self.job],date(2026,10,20))
    def test_cancelled_classes_and_unmapped_files_are_not_allowed(self):
        self.events[0]['cancelled']=True
        bb.write_json(self.path/'timetable.json',dict(events=self.events))
        with self.assertRaises(ValueError): self.window(['0']).require([self.job],date(2026,10,20))
        with self.assertRaises(ValueError): self.window([]).require([self.job],date(2026,10,20))
    def test_source_change_requires_remapping(self):
        window=self.window(['0'])
        self.source.write_bytes(b'changed')
        with self.assertRaises(ValueError): window.require([self.job],date(2026,10,20))
    def test_window_rolls_on_calendar_days_without_weekend_extension(self):
        window=self.window(['0'])
        with self.assertRaises(ValueError): window.require([self.job],date(2026,10,21))
    def test_course_mismatch_is_rejected(self):
        window=self.window(['0'])
        with self.assertRaises(ValueError): window.require([dict(id='file',course_id='c2')],date(2026,10,20))
    def test_logon_after_twenty_catches_up_once(self):
        now=datetime(2026,10,20,21,tzinfo=CHINA)
        self.assertTrue(is_due(now,dict(installed_date='2026-10-20')))
        self.assertFalse(is_due(now,dict(handled_date='2026-10-20')))
    def test_before_twenty_waits_unless_previous_day_was_missed(self):
        now=datetime(2026,10,21,19,tzinfo=CHINA)
        self.assertTrue(is_due(now,dict(installed_date='2026-10-20')))
        self.assertFalse(is_due(now,dict(installed_date='2026-10-20',handled_date='2026-10-20')))
    def test_morning_run_does_not_suppress_new_evening_schedule(self):
        state=dict(handled_date='2026-10-21',handled_schedule_time='08:00')
        self.assertFalse(is_due(datetime(2026,10,21,19,tzinfo=CHINA),state))
        self.assertTrue(is_due(datetime(2026,10,21,20,tzinfo=CHINA),state))
        state['handled_schedule_time']='20:00'
        self.assertFalse(is_due(datetime(2026,10,21,21,tzinfo=CHINA),state))
