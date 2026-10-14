import json
from pathlib import Path
import tempfile
import unittest
import bb_sync as bb
from user_settings import save_settings

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'config.json'
        bb.write_json(self.path, dict(base_url='https://example.org', download_dir='old', retries=3))
    def tearDown(self):
        self.temp.cleanup()
    def seed(self):
        archive = bb.Archive(self.root, bb.Config.load(self.path))
        course = bb.Course('_1_1', 'Test', 'https://example.org/')
        archive.save_file(course, (bb.Folder('folder', 'Lectures'),), bb.Link('https://example.org/bbcswebdav/file.pdf'), 'file.pdf', b'%PDF-test', {})
        archive.close()
        return self.root / 'old'
    def test_preferences_preserve_other_keys_and_backup(self):
        cfg, _ = save_settings(self.path, dict(schedule_enabled=False, schedule_every_hours=12, max_file_mb=200))
        self.assertFalse(cfg.schedule_enabled)
        self.assertEqual(cfg.schedule_every_hours, 12)
        self.assertEqual(cfg.retries, 3)
        self.assertEqual(len(list((self.root / '.state/config-history').glob('*.json'))), 1)
    def test_invalid_schedule_does_not_change_configuration(self):
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            save_settings(self.path, dict(schedule_every_hours=0))
        self.assertEqual(self.path.read_bytes(), original)
    def test_copy_preserves_original_and_manifest(self):
        old = self.seed()
        unrelated = old / 'my-other-file.txt'
        unrelated.write_text('keep')
        cfg, count = save_settings(self.path, dict(download_dir='new'), copy_existing=True)
        self.assertEqual(count, 1)
        self.assertTrue(unrelated.exists())
        self.assertFalse((self.root / 'new/my-other-file.txt').exists())
        archive = bb.Archive(self.root, cfg)
        row = archive.db.execute('SELECT * FROM files').fetchone()
        self.assertTrue(archive.valid(row))
        self.assertTrue((old / row['relative_path']).exists())
        archive.close()
    def test_conflict_keeps_settings_and_target(self):
        self.seed()
        original = self.path.read_bytes()
        target = self.root / 'new/Test___1_1/Lectures/file.pdf'
        # Read actual registered path instead of guessing the root suffix.
        archive = bb.Archive(self.root, bb.Config.load(self.path))
        relative = archive.db.execute('SELECT relative_path FROM files').fetchone()[0]
        archive.close()
        target = self.root / 'new' / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(b'other')
        with self.assertRaises(ValueError):
            save_settings(self.path, dict(download_dir='new'), copy_existing=True)
        self.assertEqual(target.read_bytes(), b'other')
        self.assertEqual(self.path.read_bytes(), original)
    def test_active_sync_blocks_preference_changes(self):
        with bb.single_instance(self.root):
            with self.assertRaises(RuntimeError):
                save_settings(self.path, dict(retries=2))
    def test_changing_school_clears_stale_course_selection(self):
        save_settings(self.path, dict(selected_course_ids=['_1_1']))
        cfg, _ = save_settings(self.path, dict(base_url='https://other.org'))
        self.assertEqual(cfg.selected_course_ids, [])
        self.assertEqual(cfg.course_selection_mode, 'selected')
    def test_nan_delay_and_full_url_host_are_rejected(self):
        for value in ({'request_delay_seconds':float('nan')}, {'allowed_file_hosts':['https://files.example.org']}):
            with self.assertRaises(ValueError):
                save_settings(self.path, value)
