from pathlib import Path
import tempfile
import unittest

import bb_sync as bb


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = bb.Archive(self.root, bb.Config("https://example.org"))
        self.course = bb.Course("_1_1", "ADS2 Course", "https://example.org/course")
        self.link = bb.Link("https://example.org/bbcswebdav/lecture.pdf")

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def row(self, crumbs=(), link=None):
        return self.archive.get(bb.resource_key(self.course, link or self.link, crumbs))

    def test_full_depth_and_original_filename(self):
        crumbs = tuple(bb.Folder(f"_level_{i}_1", f"Level {i}") for i in range(7))
        self.archive.save_file(self.course, crumbs, self.link, "Original Lecture.pdf", b"data", {})
        path = Path(self.row(crumbs)["relative_path"])
        self.assertEqual(path.parts[1:-1], tuple(f"Level {i}" for i in range(7)))
        self.assertEqual(path.name, "Original Lecture.pdf")

    def test_same_resource_appears_in_each_source_folder(self):
        a, b = (bb.Folder("_a_1", "Lectures"),), (bb.Folder("_b_1", "Readings"),)
        self.archive.save_file(self.course, a, self.link, "lecture.pdf", b"data", {})
        self.archive.save_file(self.course, b, self.link, "lecture.pdf", b"data", {})
        for crumbs in (a, b):
            self.assertTrue(self.archive.valid(self.row(crumbs)))
            self.assertEqual(Path(self.row(crumbs)["relative_path"]).name, "lecture.pdf")
        self.assertNotEqual(self.row(a)["relative_path"], self.row(b)["relative_path"])

    def test_root_copy_is_not_mistaken_for_old_layout(self):
        crumbs = (bb.Folder("_a_1", "Lectures"),)
        self.archive.save_file(self.course, (), self.link, "lecture.pdf", b"data", {})
        self.archive.save_file(self.course, crumbs, self.link, "lecture.pdf", b"data", {})
        self.assertTrue(self.archive.valid(self.row()))
        self.assertTrue(self.archive.valid(self.row(crumbs)))

    def test_duplicate_folder_labels_do_not_merge(self):
        a, b = (bb.Folder("_a_1", "Week 1"),), (bb.Folder("_b_1", "Week 1"),)
        first = self.archive.folder_dir(self.course, a)
        second = self.archive.folder_dir(self.course, b)
        self.assertNotEqual(first, second)
        self.assertEqual(first.name, "Week 1")
        self.assertTrue(second.name.startswith("Week 1__"))
        self.assertEqual(self.archive.folder_dir(self.course, b), second)

    def test_different_files_with_same_name_are_preserved(self):
        crumbs = (bb.Folder("_a_1", "Lectures"),)
        other = bb.Link("https://example.org/bbcswebdav/other/lecture.pdf")
        self.archive.save_file(self.course, crumbs, self.link, "Lecture.pdf", b"one", {})
        self.archive.save_file(self.course, crumbs, other, "lecture.pdf", b"two", {})
        first, second = self.row(crumbs), self.row(crumbs, other)
        self.assertNotEqual(first["relative_path"].casefold(), second["relative_path"].casefold())
        self.assertTrue(self.archive.valid(first))
        self.assertTrue(self.archive.valid(second))

    def test_folder_rename_keeps_subtree_and_history(self):
        old = (bb.Folder("_a_1", "Old name"), bb.Folder("_b_1", "Week 1"))
        new = (bb.Folder("_a_1", "New name"), bb.Folder("_b_1", "Week 1"))
        self.archive.save_file(self.course, old, self.link, "lecture.pdf", b"one", {})
        self.archive.save_file(self.course, old, self.link, "lecture.pdf", b"two", {})
        self.assertEqual(self.archive.save_file(self.course, new, self.link, "lecture.pdf", b"two", {}), "unchanged")
        row = self.row(new)
        self.assertIn("/New name/Week 1/", row["relative_path"])
        self.assertTrue(self.archive.valid(row))
        history = self.archive.db.execute("SELECT relative_path FROM versions").fetchall()
        self.assertEqual(len(history), 1)
        self.assertEqual((self.archive.output / history[0][0]).read_bytes(), b"one")
        self.assertFalse((self.archive.course_dir(self.course) / "Old name").exists())

    def test_course_folder_stays_fixed_after_course_label_changes(self):
        before = self.archive.course_dir(self.course)
        renamed = bb.Course(self.course.id, "A renamed course", self.course.url)
        self.assertEqual(self.archive.course_dir(renamed), before)

    def test_legacy_hashed_file_is_relocated_without_redownload(self):
        self.archive.save_file(self.course, (), self.link, "lecture.pdf", b"data", {})
        row = self.row()
        current = self.archive.output / row["relative_path"]
        old = current.with_name("lecture__" + row["key"][:10] + ".pdf")
        current.rename(old)
        self.archive.db.execute("UPDATE files SET relative_path=? WHERE key=?", (self.archive.relative(old), row["key"]))
        self.archive.db.commit()
        crumbs = (bb.Folder("_a_1", "Lecture 1"),)
        self.assertEqual(self.archive.save_file(self.course, crumbs, self.link, "lecture.pdf", b"data", {}), "unchanged")
        updated = self.row(crumbs)
        self.assertEqual(Path(updated["relative_path"]).name, "lecture.pdf")
        self.assertTrue(self.archive.valid(updated))
        self.assertFalse(old.exists())

    def test_header_filename_rename_keeps_bytes(self):
        crumbs = (bb.Folder("_a_1", "Lecture 1"),)
        self.archive.save_file(self.course, crumbs, self.link, "old.pdf", b"data", {})
        self.assertEqual(self.archive.save_file(self.course, crumbs, self.link, "new.pdf", b"data", {}), "unchanged")
        row = self.row(crumbs)
        self.assertEqual(Path(row["relative_path"]).name, "new.pdf")
        self.assertTrue(self.archive.valid(row))

    def test_full_windows_paths_longer_than_260_chars(self):
        crumbs = tuple(bb.Folder(f"_l{i}_1", "A full folder label " + str(i) + "x" * 35) for i in range(6))
        self.archive.save_file(self.course, crumbs, self.link, "lecture.pdf", b"data", {})
        row = self.row(crumbs)
        self.assertGreater(len(str(self.archive.output / row["relative_path"])), 300)
        self.assertTrue(self.archive.valid(row))
        self.assertEqual(len(Path(row["relative_path"]).parts), 8)

    def test_directory_move_cannot_escape_output_root(self):
        with self.assertRaises(ValueError):
            self.archive.rebase_folder("../outside", "inside")

    def test_literal_percent_characters_are_preserved(self):
        self.archive.save_file(self.course, (bb.Folder("_a_1", "100%20 literal"),), self.link, "Lecture%20literal.pdf", b"data", {})
        row = self.row((bb.Folder("_a_1", "100%20 literal"),))
        self.assertIn("100%20 literal/Lecture%20literal.pdf", row["relative_path"])
