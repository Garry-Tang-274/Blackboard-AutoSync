from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

import bb_sync as bb


class UnitTests(unittest.TestCase):
    def test_registered_nested_course_folder_is_recreated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = bb.Archive(root, bb.Config("https://example.org"))
            course = bb.Course("_1_1", "Course", "https://example.org/")
            archive.db.execute("INSERT INTO courses VALUES(?,?,?,?)", (course.id, course.name, course.url, "ADS2/Blackboard"))
            archive.db.commit()
            expected = archive.output / "ADS2" / "Blackboard"
            self.assertEqual(archive.course_dir(course), expected)
            self.assertTrue(expected.is_dir())
            self.assertEqual(archive.db.execute("SELECT relative_path FROM courses WHERE id=?", (course.id,)).fetchone()[0], "ADS2/Blackboard")
            archive.close()
    def test_closed_browser_stops_without_retrying_queued_pages(self):
        closed = type("TargetClosedError", (Exception,), {})
        context = Mock()
        context.new_page.return_value.goto.side_effect = closed()
        reader = bb.BrowserReader(context, bb.Config("https://example.org"), Mock())
        with self.assertRaises(bb.SyncInterrupted):
            reader.goto("https://example.org/")
        self.assertEqual(reader.page.goto.call_count, 1)
        reader.page.wait_for_timeout.assert_not_called()

    def test_closed_browser_stops_file_network_retries(self):
        closed = type("TargetClosedError", (Exception,), {})
        context, archive = Mock(), Mock()
        context.request.get.side_effect = closed()
        archive.normalize_file_path.return_value = ("key", None, None)
        downloader = bb.Downloader(context, bb.Config("https://example.org"), archive)
        with self.assertRaises(bb.SyncInterrupted):
            downloader.get(bb.Course("_1_1", "Test", "https://example.org/"), (),
                           bb.Link("https://example.org/lecture.pdf"), "https://example.org/")
        self.assertEqual(context.request.get.call_count, 1)

    def test_committed_file_is_not_failed_when_browser_closes_during_cleanup(self):
        closed = type("TargetClosedError", (Exception,), {})
        context, archive, response = Mock(), Mock(), Mock()
        archive.normalize_file_path.return_value = ("key", None, None)
        archive.save_file.return_value = "downloaded"
        response.status = 200
        response.headers = {"content-type": "application/pdf"}
        response.body.return_value = b"%PDF-test"
        response.dispose.side_effect = closed()
        context.request.get.return_value = response
        downloader = bb.Downloader(context, bb.Config("https://example.org"), archive)
        self.assertEqual(downloader.get(bb.Course("_1_1", "Test", "https://example.org/"), (),
                         bb.Link("https://example.org/lecture.pdf"), "https://example.org/"), "downloaded")
        archive.save_file.assert_called_once()

    def test_course_launcher_without_course_id(self):
        url = "https://example.org/webapps/blackboard/execute/launcher?type=Course&id=_123_1&url="
        self.assertEqual(bb.course_id(url), "_123_1")
        self.assertTrue(bb.is_course_entry(url))

    def test_safe_windows_names_and_length(self):
        for name in ["CON.pdf", "aux", "..\\evil/../a?.pdf", ". .", "学习" * 80 + ".pdf"]:
            result = bb.safe_name(name)
            self.assertFalse(any(c in result for c in '\\/:?'))
            self.assertFalse(result.endswith((".", " ")))
            self.assertLessEqual(len(result), 70)
            self.assertFalse(bb.RESERVED.match(result))

    def test_filename_star_and_traversal(self):
        self.assertEqual(bb.filename_from_headers("attachment; filename*=UTF-8''%E8%AF%BE%E4%BB%B6.pdf", "https://test/download", "", "application/pdf"), "课件.pdf")
        self.assertNotIn("/", bb.filename_from_headers('attachment; filename="../../CON.pdf"', "https://test/download", "", "application/pdf"))

    def test_secret_redaction_and_stable_identity(self):
        a = "https://example.org/file.pdf?content_id=1&token=private&X-Amz-Signature=secret#access_token=hidden"
        b = "https://example.org/file.pdf?token=changed&content_id=1"
        self.assertEqual(bb.canonical_url(a), bb.canonical_url(b))
        safe = bb.public_url(a)
        for value in ["private", "secret", "hidden"]:
            self.assertNotIn(value, safe)

    def test_read_only_navigation_allowlist(self):
        cfg = bb.Config("https://example.org")
        self.assertTrue(bb.is_navigation("https://example.org/webapps/blackboard/content/listContent.jsp?course_id=_1_1&content_id=_2_1", "_1_1", cfg))
        for url in ["https://example.org/webapps/assessment/take?course_id=_1_1",
                    "https://example.org/webapps/blackboard/content/listContent.jsp?course_id=_1_1&method=delete",
                    "https://other.org/webapps/blackboard/content/listContent.jsp?course_id=_1_1",
                    "https://example.org/webapps/blackboard/content/listContent.jsp?course_id=_99_1"]:
            self.assertFalse(bb.is_navigation(url, "_1_1", cfg))

    def test_course_content_menu_reset_is_only_navigation(self):
        cfg = bb.Config("https://example.org")
        self.assertTrue(bb.is_navigation('https://example.org/webapps/blackboard/content/listContent.jsp?course_id=_1_1&content_id=_2_1&mode=reset','_1_1',cfg))
        self.assertFalse(bb.is_navigation('https://example.org/webapps/blackboard/content/listContent.jsp?course_id=_1_1&content_id=_2_1&action=reset','_1_1',cfg))

    def test_login_callback_parameters_are_redacted(self):
        safe = bb.public_url('https://example.org/webapps/bb-zjdxsso-BBLEARN/index.jsp?code=private-auth-code&state=private-state')
        self.assertNotIn('private-auth-code', safe)
        self.assertNotIn('private-state', safe)

    def test_config_rejects_real_http_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for url in ["http://example.org", "https://user:password@example.org", "https://example.org/?token=private"]:
                path.write_text(json.dumps({"base_url": url}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    bb.Config.load(path)

    def test_config_rejects_external_course_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"base_url": "https://example.org", "courses_url": "https://other.org/courses"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                bb.Config.load(path)

    def test_archive_versions_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = bb.Archive(root, bb.Config("https://example.org"))
            course = bb.Course("_1_1", "Test Course", "https://example.org/course")
            link = bb.Link("https://example.org/bbcswebdav/item/lecture.pdf")
            try:
                self.assertEqual(archive.save_file(course, ("Lecture",), link, "lecture.pdf", b"version one", {"etag": "1"}), "downloaded")
                self.assertEqual(archive.save_file(course, ("Lecture",), link, "lecture.pdf", b"version one", {"etag": "1"}), "unchanged")
                self.assertEqual(archive.save_file(course, ("Lecture",), link, "lecture.pdf", b"version two", {"etag": "2"}), "updated")
                row = archive.get(bb.resource_key(course, link, ("Lecture",)))
                path = archive.output / row["relative_path"]
                self.assertTrue(archive.valid(row))
                path.write_bytes(b"damaged file")
                self.assertFalse(archive.valid(row))
                archive.save_file(course, ("Lecture",), link, "lecture.pdf", b"version two", {})
                self.assertTrue(archive.valid(archive.get(row["key"])))
                histories = list(archive.output.rglob("_history/*"))
                self.assertEqual({p.read_bytes() for p in histories}, {b"version one", b"damaged file"})
            finally:
                archive.close()

    def test_instance_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with bb.single_instance(root):
                with self.assertRaises(RuntimeError):
                    with bb.single_instance(root):
                        self.fail("Second instance unexpectedly acquired lock")
            with bb.single_instance(root):
                pass

    def test_session_cache_is_encrypted_for_windows_user(self):
        secret = b'fixture-session-token-should-not-be-readable'
        encrypted = bb.protect_session(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(bb.protect_session(encrypted, decrypt=True), secret)


class FixtureState:
    def __init__(self):
        self.version = 1
        self.mode = "normal"
        self.requests = []
        self.file_calls = 0


def handler_class(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, data, status=200, headers=None):
            if isinstance(data, str):
                data = data.encode("utf-8")
            self.send_response(status)
            for key, value in (headers or {"Content-Type": "text/html; charset=utf-8"}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            state.requests.append("POST " + self.path)
            self.respond("unexpected POST", 500)

        def do_GET(self):
            state.requests.append(self.path)
            parsed = urlsplit(self.path)
            path, q = parsed.path, parse_qs(parsed.query)
            if state.mode == "sso_login":
                return self.respond('<html><body><h1>Welcome to Blackboard</h1><a href="/webapps/auth/login.jsp">Sign in with INTL account</a></body></html>')
            if state.mode == "cookie_auth" and "BBSESSION=fixture-valid" not in self.headers.get("Cookie", ""):
                return self.respond('<html><body><input type="password">Sign in</body></html>')
            if path == "/webapps/login/" or state.mode == "expired":
                return self.respond('<html><body><h1>Login</h1><form><input type="password"></form></body></html>')
            if path == "/":
                if state.mode == "empty":
                    return self.respond('<html><body><h1>My Blackboard</h1><a href="/logout">Logout</a></body></html>')
                paging = '''<div class="pagination"><button onclick="const a=document.createElement('a');a.href='/webapps/blackboard/execute/launcher?type=Course&id=_3_1';a.innerText='Extra Course';document.body.appendChild(a);this.disabled=true">Next</button></div>''' if state.mode == "button_pagination" else ""
                unsafe = '''<button onclick="fetch('/unexpected-action')">Next</button>''' if state.mode == "unsafe_next" else ""
                return self.respond('''<html><body><h1>My Blackboard</h1><a href="/logout">Logout</a>
<a href="/webapps/blackboard/execute/launcher?type=Course&amp;id=_1_1">ADS2 课程</a>
<a href="/webapps/blackboard/execute/courseList?page=2">Next courses</a>''' + paging + unsafe + '</body></html>')
            if path.endswith("/courseList"):
                return self.respond('''<html><body><a href="/webapps/blackboard/execute/launcher?type=Course&amp;id=_2_1">BG2 Course</a></body></html>''')
            if path.endswith("/launcher"):
                cid = q["id"][0]
                if cid == "_3_1":
                    return self.respond('<html><body><h1>Extra course</h1><p>Extra course page</p></body></html>')
                if cid == "_2_1":
                    if state.mode == "course_forbidden":
                        return self.respond("course access denied", 403)
                    return self.respond('''<html><body><h1>BG2</h1><p>Course guidance text.</p></body></html>''')
                extra = '<a href="/webapps/assessment/take?course_id=_1_1">Take test</a>' if state.mode == "unknown_tool" else ""
                reset = '&amp;mode=reset' if state.mode == 'menu_reset' else ''
                return self.respond('''<html><body><h1>ADS2</h1><div id="courseMenuPalette">
<a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_root_1''' + reset + '''">课件</a></div>''' + extra + '</body></html>')
            if path.endswith("/listContent.jsp"):
                content = q.get("content_id", [""])[0]
                if state.mode == "deep_hierarchy":
                    menu = '''<div id="courseMenuPalette"><a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_root_1">Course Content</a><a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_lab_1">Lab Materials</a></div>'''
                    header = '<h1>Open Quick Links</h1>'
                    if content == "_root_1":
                        body = '''<h1 id="pageTitleText">Course Content</h1><a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_depth0_1">Level 0</a>'''
                    elif content == "_lab_1":
                        body = '<h1 id="pageTitleText">Lab Materials</h1><a href="/bbcswebdav/docs/lecture.pdf">lecture.pdf</a>'
                    else:
                        depth = int(content.split("depth", 1)[1].split("_", 1)[0])
                        body = '<h1 id="pageTitleText">Level ' + str(depth) + '</h1>'
                        body += '<nav id="breadcrumbs"><a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_root_1">Course Content</a></nav>'
                        body += ('<a href="/bbcswebdav/docs/lecture.pdf">lecture.pdf</a>' if depth == 5 else
                                 '<a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_depth' + str(depth + 1) + '_1">Level ' + str(depth + 1) + '</a>')
                    return self.respond('<html><body>' + header + menu + body + '</body></html>')
                if content == "_root_1":
                    return self.respond('''<html><body><h1>课件</h1><p>Source text</p>
<a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_week_1">Lecture 1</a>
<a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_root_1&amp;page=2">Next</a>
</body></html>''' if "page" not in q else '''<html><body><h1>课件 page 2</h1>
<a href="/bbcswebdav/docs/supplement.csv">Supplement.csv</a></body></html>''')
                return self.respond('''<html><body><h1>Lecture 1</h1><p>完整原始课程正文</p>
<a href="/bbcswebdav/docs/lecture.pdf?token=PRIVATE_SIGNED_VALUE">Lecture.pdf</a>
<a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_root_1">Back</a>
</body></html>''')
            if path.startswith("/bbcswebdav/"):
                state.file_calls += 1
                if state.mode == "html_attachment" and path.endswith(".pdf"):
                    return self.respond('<html><body><input type="password">Login</body></html>')
                if state.mode == "attachment_redirect" and path.endswith(".pdf"):
                    return self.respond("", 302, {"Location": "/webapps/login/"})
                if state.mode == "external_redirect" and path.endswith(".pdf"):
                    return self.respond("", 302, {"Location": "https://unconfigured.example.org/private.pdf"})
                if state.mode == "retry" and state.file_calls == 1:
                    return self.respond("temporary limit", 429, {"Retry-After": "0"})
                if path.endswith(".pdf"):
                    tag, data, mime = f'"v{state.version}"', f'%PDF-1.4\nfixture-version-{state.version}\n%%EOF'.encode(), "application/pdf"
                else:
                    tag, data, mime = '"csv-1"', b"x,y\n1,2\n", "text/csv"
                if self.headers.get("If-None-Match") == tag:
                    return self.respond(b"", 304, {"ETag": tag})
                return self.respond(data, headers={"Content-Type": mime, "ETag": tag,
                                                   "Content-Disposition": 'attachment; filename="' + path.rsplit("/", 1)[-1] + '"'})
            return self.respond("not found", 404)
    return Handler


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, channel="chromium")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = FixtureState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.cfg = bb.Config(self.base, request_delay_seconds=0, allow_local_http=True,
                             max_scroll_rounds=5, timeout_seconds=10)
        self.context = self.browser.new_context()

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def run_sync(self):
        return bb.SyncEngine(self.context, self.cfg, self.root).run()

    def test_nested_course_discovery_pagination_idempotence_and_versions(self):
        first = self.run_sync()
        self.assertEqual(first["status"], "synced", first["issues"])
        self.assertTrue(first["complete"])
        self.assertEqual(first["discovered_courses"], 2)
        self.assertEqual(first["counts"]["downloaded"], 2)
        self.assertEqual(first["counts"]["pages"], 5)
        self.assertIn("完整原始课程正文", "".join(p.read_text(encoding="utf-8") for p in (self.root / "downloads").rglob("*.txt")))
        second = self.run_sync()
        self.assertEqual(second["counts"]["unchanged"], 2)
        self.assertEqual(second["counts"]["downloaded"], 0)
        self.state.version = 2
        third = self.run_sync()
        self.assertEqual(third["counts"]["updated"], 1)
        histories = list((self.root / "downloads").rglob("_history/*.pdf"))
        self.assertEqual(len(histories), 1)
        self.assertIn(b"version-1", histories[0].read_bytes())
        for report_path in (self.root / "reports").glob("*"):
            self.assertNotIn("PRIVATE_SIGNED_VALUE", report_path.read_text(encoding="utf-8"))

    def test_missing_local_file_is_repaired_without_304(self):
        self.run_sync()
        pdf = next((self.root / "downloads").rglob("*.pdf"))
        pdf.unlink()
        report = self.run_sync()
        self.assertEqual(report["status"], "synced", report["issues"])
        self.assertEqual(report["counts"]["updated"], 1)
        self.assertTrue(pdf.is_file())

    def test_html_login_attachment_is_never_saved(self):
        self.state.mode = "html_attachment"
        report = self.run_sync()
        self.assertEqual(report["status"], "auth_required")
        self.assertFalse(list((self.root / "downloads").rglob("*.pdf")))
        self.assertFalse(report["complete"])

    def test_attachment_auth_redirect_stops_run(self):
        self.state.mode = "attachment_redirect"
        report = self.run_sync()
        self.assertEqual(report["status"], "auth_required")

    def test_expired_session_is_not_empty_success(self):
        self.state.mode = "expired"
        report = self.run_sync()
        self.assertEqual(report["status"], "auth_required")
        self.assertFalse(report["complete"])

    def test_zero_course_is_failed(self):
        self.state.mode = "empty"
        report = self.run_sync()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["complete"])

    def test_rate_limit_retries(self):
        self.state.mode = "retry"
        report = self.run_sync()
        self.assertEqual(report["status"], "synced", report["issues"])
        self.assertEqual(report["counts"]["downloaded"], 2)
        self.assertEqual(self.state.file_calls, 3)

    def test_external_redirect_blocked_and_partial(self):
        self.state.mode = "external_redirect"
        report = self.run_sync()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertFalse(list((self.root / "downloads").rglob("*.pdf")))

    def test_unknown_tools_are_reported_without_entering(self):
        self.state.mode = "unknown_tool"
        report = self.run_sync()
        self.assertEqual(report["status"], "partial")
        self.assertIn("unsupported_course_link", {issue["code"] for issue in report["issues"]})
        self.assertFalse(any("/assessment/take" in path or path.startswith("POST") for path in self.state.requests))

    def test_page_limit_is_not_complete(self):
        self.cfg.max_pages_per_course = 1
        report = self.run_sync()
        self.assertEqual(report["status"], "partial")
        self.assertIn("page_limit", {issue["code"] for issue in report["issues"]})

    def test_course_filters(self):
        self.cfg.include_courses = ["BG2"]
        report = self.run_sync()
        self.assertEqual(report["status"], "synced")
        self.assertEqual(len(report["courses"]), 1)
        self.assertEqual(report["counts"]["downloaded"], 0)

    def test_zero_selected_courses_is_failed(self):
        self.cfg.include_courses = ["Nonexistent Course"]
        report = self.run_sync()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["complete"])

    def test_forbidden_course_does_not_stop_other_downloads(self):
        self.state.mode = "course_forbidden"
        report = self.run_sync()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["counts"]["downloaded"], 2)
        self.assertTrue(any("403" in item["message"] for item in report["issues"]))
        self.assertNotEqual(report["status"], "auth_required")

    def test_persistent_session_and_download_cookie_reuse(self):
        self.state.mode = "cookie_auth"
        authenticated = bb.browser_context(self.pw, self.cfg, self.root, headed=False)
        authenticated.add_cookies([{"name": "BBSESSION", "value": "fixture-valid", "url": self.base}])
        try:
            first = bb.SyncEngine(authenticated, self.cfg, self.root).run()
            self.assertEqual(first["status"], "synced", first["issues"])
            self.assertEqual(first["counts"]["downloaded"], 2)
        finally:
            bb.save_browser_session(authenticated, self.cfg, self.root)
            authenticated.close()
        restored = bb.browser_context(self.pw, self.cfg, self.root, headed=False)
        try:
            second = bb.SyncEngine(restored, self.cfg, self.root).run()
            self.assertEqual(second["status"], "synced", second["issues"])
            self.assertEqual(second["counts"]["unchanged"], 2)
        finally:
            restored.close()

    def test_explicit_pagination_button_discovers_additional_courses(self):
        self.state.mode = "button_pagination"
        report = self.run_sync()
        self.assertEqual(report["status"], "synced", report["issues"])
        self.assertEqual(report["discovered_courses"], 3)

    def test_next_button_outside_pagination_is_not_clicked(self):
        self.state.mode = "unsafe_next"
        report = self.run_sync()
        self.assertEqual(report["status"], "synced", report["issues"])
        self.assertFalse(any('/unexpected-action' in path for path in self.state.requests))

    def test_discovery_caches_courses_without_downloading(self):
        catalog = bb.discover_and_cache(self.context, self.cfg, self.root)
        self.assertEqual(len(catalog["courses"]), 2)
        self.assertTrue(catalog["complete"])
        self.assertEqual(len(bb.load_course_catalog(self.root, self.cfg)["courses"]), 2)
        self.assertFalse(any('/bbcswebdav/' in path for path in self.state.requests))
        self.assertFalse((self.root / "downloads").exists())

    def test_selected_ids_only_crawl_checked_courses(self):
        self.cfg.course_selection_mode = "selected"
        self.cfg.selected_course_ids = ["_1_1"]
        report = self.run_sync()
        self.assertEqual(report["status"], "synced", report["issues"])
        self.assertEqual([course["id"] for course in report["courses"]], ["_1_1"])
        self.assertEqual(report["counts"]["downloaded"], 2)
        self.assertFalse(any(parse_qs(urlsplit(path).query).get("id") == ["_2_1"] for path in self.state.requests))
        self.assertEqual(len(bb.load_course_catalog(self.root, self.cfg)["courses"]), 2)

    def test_empty_selection_pauses_without_contacting_blackboard(self):
        self.cfg.course_selection_mode = "selected"
        self.cfg.selected_course_ids = []
        report = self.run_sync()
        self.assertEqual(report["status"], "paused")
        self.assertEqual(self.state.requests, [])
        self.assertEqual(report["counts"]["downloaded"], 0)
        self.assertEqual(bb.exit_code(report["status"]), 0)

    def test_missing_selected_course_is_reported(self):
        self.cfg.course_selection_mode = "selected"
        self.cfg.selected_course_ids = ["_1_1", "_missing_1"]
        report = self.run_sync()
        self.assertEqual(report["status"], "partial")
        self.assertTrue(any(issue["code"] == "selected_course_unavailable" and issue["course_id"] == "_missing_1" for issue in report["issues"]))

    def test_bb_hierarchy_uses_content_title_and_preserves_all_levels(self):
        self.state.mode = "deep_hierarchy"
        root_url = self.base + '/webapps/blackboard/content/listContent.jsp?course_id=_1_1&content_id=_root_1'
        self.cfg.extra_course_urls = [{"name": "ADS2", "url": root_url}]
        self.cfg.include_courses = ["ADS2"]
        report = self.run_sync()
        self.assertEqual(report["status"], "synced", report["issues"])
        self.assertEqual(report["counts"]["downloaded"], 2)
        paths = [p.relative_to(self.root / 'downloads').parts[1:] for p in (self.root / 'downloads').rglob('*.pdf')]
        self.assertIn(('Lab Materials', 'lecture.pdf'), paths)
        self.assertIn(('Course Content', *(f'Level {i}' for i in range(6)), 'lecture.pdf'), paths)
        self.assertFalse(any('Open Quick Links' in parts for parts in paths))
        second = self.run_sync()
        self.assertEqual(second["counts"]["unchanged"], 2)

    def test_bb_pagination_keeps_the_original_folder(self):
        report = self.run_sync()
        self.assertEqual(report["status"], "synced")
        csv = next((self.root / 'downloads').rglob('*.csv'))
        self.assertEqual(csv.parent.name, '课件')
        self.assertEqual(csv.name, 'supplement.csv')

    def test_sso_only_login_page_is_reported_as_login_required(self):
        self.state.mode = "sso_login"
        report = self.run_sync()
        self.assertEqual(report["status"], "auth_required")
        self.assertEqual(report["counts"]["pages"], 0)

    def test_cached_selected_course_entries_are_revalidated(self):
        self.cfg.course_selection_mode = "selected"
        self.cfg.selected_course_ids = ["_1_1"]
        entry = self.base + '/webapps/blackboard/execute/launcher?type=Course&id=_1_1'
        bb.cache_courses(self.root, self.cfg, [bb.Course("_1_1", "ADS2", entry)], complete=True)
        self.state.mode = "empty"
        report = self.run_sync()
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["complete"])
        self.assertEqual(report["counts"]["downloaded"], 2)
        self.assertEqual(report["course_list_source"], "cached_selected")

    def test_all_mode_does_not_use_a_stale_course_catalog(self):
        entry = self.base + '/webapps/blackboard/execute/launcher?type=Course&id=_1_1'
        bb.cache_courses(self.root, self.cfg, [bb.Course("_1_1", "ADS2", entry)], complete=True)
        self.state.mode = "empty"
        report = self.run_sync()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["counts"]["downloaded"], 0)

    def test_real_school_menu_reset_links_reach_attachments(self):
        self.state.mode = 'menu_reset'
        report = self.run_sync()
        self.assertEqual(report['status'], 'synced', report['issues'])
        self.assertEqual(report['counts']['downloaded'], 2)


if __name__ == "__main__":
    unittest.main()
