import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import bb_sync as bb


def report():
    return {"schema":"blackboard-autosync/v1", "run_id":"fixture", "site":"https://example.org",
            "started_at":bb.now(), "finished_at":None, "status":"running", "complete":False,
            "download_dir":"fixture-downloads", "courses":[], "issues":[], "external_links":[],
            "current_step":"同步课程 1/6", "current_course":"ADS2",
            "counts":{"downloaded":0,"updated":0,"unchanged":0,"failed":0,"pages":0}}


class ProgressTests(unittest.TestCase):
    def test_live_report_refreshes_and_final_history_is_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = report()
            bb.publish_report(root, value)
            document = (root/'reports/latest.html').read_text(encoding='utf-8')
            self.assertIn('http-equiv="refresh"', document)
            self.assertIn('同步课程 1/6', document)
            self.assertEqual(len(list((root/'reports').glob('*.json'))), 1)
            value.update(status='synced',finished_at=bb.now(),complete=True)
            bb.publish_report(root, value)
            self.assertNotIn('http-equiv="refresh"', (root/'reports/latest.html').read_text(encoding='utf-8'))
            self.assertEqual(len(list((root/'reports').glob('*.json'))), 2)

    def test_console_result_includes_failure_reason(self):
        value = report()
        value.update(status='failed',issues=[{'message':'没有识别到课程'}])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bb.print_sync_result(value, Path('fixture'))
        self.assertIn('状态：同步失败', output.getvalue())
        self.assertIn('原因：没有识别到课程', output.getvalue())
