import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest

import bb_sync as bb
from course_selector import CourseSelector, parse_keywords


class SelectionTests(unittest.TestCase):
    def test_exact_course_ids_distinguish_identical_names(self):
        cfg = bb.Config("https://example.org", course_selection_mode="selected", selected_course_ids=["_1_1"])
        self.assertTrue(bb.course_selected(cfg, bb.Course("_1_1", "Same name", "")))
        self.assertFalse(bb.course_selected(cfg, bb.Course("_2_1", "Same name", "")))

    def test_selected_mode_does_not_include_new_courses(self):
        cfg = bb.Config("https://example.org", course_selection_mode="selected", selected_course_ids=["_1_1"])
        self.assertFalse(bb.course_selected(cfg, bb.Course("_new_1", "New Course", "")))

    def test_all_mode_keeps_keyword_filters(self):
        cfg = bb.Config("https://example.org", include_courses=["ADS2"], exclude_courses=["2025"])
        self.assertTrue(bb.course_selected(cfg, bb.Course("_1_1", "ADS2 2026", "")))
        self.assertFalse(bb.course_selected(cfg, bb.Course("_2_1", "BG2 2026", "")))
        self.assertFalse(bb.course_selected(cfg, bb.Course("_3_1", "ADS2 2025", "")))

    def test_empty_selected_mode_matches_no_courses(self):
        cfg = bb.Config("https://example.org", course_selection_mode="selected")
        self.assertFalse(bb.course_selected(cfg, bb.Course("_1_1", "Any Course", "")))

    def test_save_preserves_settings_and_original_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = {"base_url": "https://example.org", "retries": 7, "download_dir": "original-files"}
            path.write_text(json.dumps(original), encoding="utf-8")
            cfg = bb.save_course_preferences(path, "selected", ["_2_1", "_1_1", "_1_1"], [], [])
            self.assertEqual(cfg.selected_course_ids, ["_1_1", "_2_1"])
            self.assertEqual(cfg.retries, 7)
            self.assertEqual(cfg.download_dir, "original-files")
            backups = list((path.parent / ".state/config-history").glob("*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)

    def test_invalid_selection_does_not_replace_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"base_url":"https://example.org"}', encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                bb.save_course_preferences(path, "invalid", [], [], [])
            self.assertEqual(path.read_bytes(), before)

    def test_catalog_does_not_cross_school_sites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = bb.Config("https://school-a.org")
            bb.cache_courses(root, cfg, [bb.Course("_1_1", "Course A", cfg.base_url)], complete=True)
            self.assertEqual(len(bb.load_course_catalog(root, cfg)["courses"]), 1)
            self.assertEqual(bb.load_course_catalog(root, bb.Config("https://school-b.org"))["courses"], [])

    def test_old_configuration_defaults_to_all_courses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"base_url":"https://example.org"}', encoding="utf-8")
            self.assertEqual(bb.Config.load(path).course_selection_mode, "all")

    def test_chinese_keyword_separators(self):
        self.assertEqual(parse_keywords("ADS2， BG2; ADS2\nDST2"), ["ADS2", "BG2", "DST2"])

    def test_fixed_root_can_be_changed_without_changing_course_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.write_text('{"base_url":"https://example.org","course_selection_mode":"selected","selected_course_ids":["_1_1"]}', encoding="utf-8")
            cfg = bb.save_course_preferences(path, "selected", ["_1_1"], [], [], download_dir="course-materials")
            self.assertEqual(cfg.output(root), root / "course-materials")
            self.assertTrue(Path(cfg.download_dir).is_absolute())
            self.assertEqual(cfg.selected_course_ids, ["_1_1"])


class DialogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({"base_url": "https://example.org", "course_selection_mode": "selected", "selected_course_ids": ["_1_1", "_missing_1"]}), encoding="utf-8")
        bb.cache_courses(self.root, bb.Config.load(self.config_path), [bb.Course("_1_1", "ADS2", ""), bb.Course("_2_1", "BG2", "")], complete=True)
        self.window = tk.Tk()
        self.window.withdraw()
        self.dialog = CourseSelector(self.window, self.root)

    def tearDown(self):
        self.window.destroy()
        self.temp.cleanup()

    def test_search_preserves_hidden_and_unavailable_selection(self):
        self.dialog.search.set("BG2")
        self.assertTrue(self.dialog.course_vars["_1_1"].get())
        self.assertTrue(self.dialog.course_vars["_missing_1"].get())
        self.dialog.save()
        self.assertEqual(bb.Config.load(self.config_path).selected_course_ids, ["_1_1", "_missing_1"])

    def test_clear_selection_persists_explicit_pause(self):
        self.dialog.set_checks(False)
        self.dialog.save()
        cfg = bb.Config.load(self.config_path)
        self.assertEqual(cfg.course_selection_mode, "selected")
        self.assertEqual(cfg.selected_course_ids, [])

    def test_new_courses_remain_unselected_after_refresh(self):
        self.dialog.catalog["courses"].append({"id": "_new_1", "name": "New course", "url": ""})
        self.dialog.reset_variables()
        self.assertFalse(self.dialog.course_vars["_new_1"].get())

    def test_all_mode_is_persisted_without_stale_explicit_ids(self):
        self.dialog.mode.set("all")
        self.dialog.all_mode()
        self.dialog.save()
        cfg = bb.Config.load(self.config_path)
        self.assertEqual(cfg.course_selection_mode, "all")
        self.assertEqual(cfg.selected_course_ids, [])
