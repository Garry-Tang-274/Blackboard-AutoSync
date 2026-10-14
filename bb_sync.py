"""Read-only Blackboard Original synchronizer, with conservative Ultra DOM discovery.

No credentials are read by this program. Log in in the dedicated browser once.
Only known read-only Blackboard navigation URLs and resource GETs are followed.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
import hashlib
import html
import json
import mimetypes
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
import uuid

ROOT = Path(__file__).resolve().parent
FILE_EXTENSIONS = set("pdf doc docx ppt pptx xls xlsx csv tsv txt r rmd py ipynb zip rar 7z mp4 mov mp3 wav png jpg jpeg gif svg webp tif tiff tex bib ris sav sps sas fasta fastq gz tar json xml html htm m c cpp java mat nb ods odp odt".split())
SECRET_KEYS = re.compile(r"token|ticket|signature|credential|password|secret|session|jwt|auth|saml|assertion|x-amz|x-goog|^code$|^state$", re.I)
RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)
AUTH_PATH = re.compile(r"/(?:login(?:[./]|$)|webapps/login(?:[./]|$)|cas/login|saml|adfs|oauth2?/authorize)", re.I)
COURSE_PATH = re.compile(r"/ultra/courses/([^/?#]+)")
NAV_PATHS = {
    "/webapps/blackboard/content/listcontent.jsp",
    "/webapps/blackboard/content/contentwrapper.jsp",
    "/webapps/blackboard/execute/content/contentview",
    "/webapps/blackboard/execute/courseMain".lower(),
    "/webapps/blackboard/execute/launcher",
    "/webapps/blackboard/execute/coursehomepage",
    "/webapps/blackboard/execute/announcement",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def safe_name(value: str, limit: int = 70) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    value = re.sub(r"\s+", " ", value) or "unnamed"
    if RESERVED.match(value):
        value = "_" + value
    if len(value) > limit:
        suffix = Path(value).suffix[:12]
        value = value[: max(1, limit - len(suffix) - 9)] + "~" + digest(value)[:8] + suffix
    return value


def filesystem_path(path: Path) -> Path:
    """Keep the source hierarchy on Windows even when its full path exceeds 260 chars."""
    if os.name != "nt":
        return path
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return path
    return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)


def public_url(url: str) -> str:
    """For provenance only; never persist authentication or signed query values."""
    u = urlsplit(url)
    netloc = u.netloc.rsplit("@", 1)[-1]
    query = [(k, "[redacted]" if SECRET_KEYS.search(k) else v) for k, v in parse_qsl(u.query)]
    fragment = "" if SECRET_KEYS.search(u.fragment) else u.fragment
    return urlunsplit((u.scheme, netloc, u.path, urlencode(query), fragment))


def canonical_url(url: str) -> str:
    u = urlsplit(url)
    query = sorted((k, v) for k, v in parse_qsl(u.query) if not SECRET_KEYS.search(k) and k.lower() not in {"ts", "timestamp", "cache", "cachebust", "nonce"})
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path, urlencode(query), ""))


def course_id(url: str) -> str | None:
    u = urlsplit(url)
    q = parse_qs(u.query)
    value = q.get("course_id", q.get("courseId", [None]))[0]
    if not value and u.path.lower().endswith("/launcher") and q.get("type", [""])[0].lower() == "course":
        value = q.get("id", [None])[0]
    match = COURSE_PATH.search(u.path)
    return value or (unquote(match[1]) if match else None)


def filename_from_headers(disposition: str, url: str, hint: str, content_type: str) -> str:
    message = Message()
    message["Content-Disposition"] = disposition
    name = message.get_filename() or ""
    if not name:
        tail = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        name = tail if Path(tail).suffix.lstrip(".").lower() in FILE_EXTENSIONS else hint or "file"
    name = safe_name(name, 240)
    if not Path(name).suffix:
        name += mimetypes.guess_extension(content_type.split(";", 1)[0]) or ".bin"
    return name


def error_label(exc: Exception) -> str:
    match = re.search(r"(?:net::)?(ERR_[A-Z_]+)", str(exc))
    return match[1] if match else type(exc).__name__


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


class AuthRequired(RuntimeError):
    pass


class PartialRun(RuntimeError):
    pass


class SyncInterrupted(RuntimeError):
    pass


def check_browser_error(exc: Exception):
    if type(exc).__name__ == "TargetClosedError":
        raise SyncInterrupted("专用浏览器已关闭，已完成文件已保存；重新同步可继续") from None


class NoCoursesFound(PartialRun):
    pass


@dataclass
class Config:
    base_url: str
    courses_url: str = ""
    download_dir: str = "downloads"
    browser_channel: str = ""
    course_selection_mode: str = "all"
    selected_course_ids: list[str] = field(default_factory=list)
    include_courses: list[str] = field(default_factory=list)
    exclude_courses: list[str] = field(default_factory=list)
    extra_course_urls: list[dict] = field(default_factory=list)
    allowed_file_hosts: list[str] = field(default_factory=list)
    request_delay_seconds: float = .7
    timeout_seconds: int = 30
    login_timeout_seconds: int = 600
    max_pages_per_course: int = 500
    max_discovery_pages: int = 50
    max_scroll_rounds: int = 30
    max_file_mb: int = 1024
    retries: int = 3
    save_page_text: bool = True
    allow_local_http: bool = False
    sync_headed: bool = False
    schedule_enabled: bool = True
    schedule_every_hours: int = 6

    @classmethod
    def load(cls, path: Path) -> Config:
        if path.name == 'config.json' and not path.exists() and (path.parent / 'config.example.json').exists():
            shutil.copyfile(path.parent / 'config.example.json', path)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        cfg = cls(**data)
        if cfg.course_selection_mode not in {"all", "selected"}:
            raise ValueError("course_selection_mode 必须为 all 或 selected")
        for name in ("selected_course_ids", "include_courses", "exclude_courses"):
            values = getattr(cfg, name)
            if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(name + " 必须是非空字符串列表（可为空列表）")
        u = urlsplit(cfg.base_url)
        if not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("base_url 必须是不含账号、查询参数的 Blackboard 根网址")
        local = u.hostname in {"localhost", "127.0.0.1", "::1"}
        if u.scheme != "https" and not (cfg.allow_local_http and local and u.scheme == "http"):
            raise ValueError("实际 Blackboard 必须使用 HTTPS；HTTP 仅用于本地测试")
        cfg.base_url = cfg.base_url.rstrip("/")
        for key in ("timeout_seconds", "login_timeout_seconds", "max_pages_per_course", "max_discovery_pages", "max_scroll_rounds", "max_file_mb", "retries"):
            if not isinstance(getattr(cfg, key), int) or isinstance(getattr(cfg, key), bool) or getattr(cfg, key) < 1:
                raise ValueError(key + " 必须大于零")
        if not isinstance(cfg.request_delay_seconds, (int, float)) or not math.isfinite(cfg.request_delay_seconds) or cfg.request_delay_seconds < 0:
            raise ValueError("请求间隔不能小于零")
        if not isinstance(cfg.allowed_file_hosts, list) or any(not isinstance(host, str) or not host or urlsplit('https://' + host).hostname != host.lower() or '/' in host or ':' in host or '@' in host for host in cfg.allowed_file_hosts):
            raise ValueError('外部文件域名只填写主机名，不包含协议、端口、路径或账号')
        if not isinstance(cfg.schedule_every_hours, int) or isinstance(cfg.schedule_every_hours, bool) or not 1 <= cfg.schedule_every_hours <= 24:
            raise ValueError("自动同步间隔必须为 1–24 小时的整数")
        for name in ("save_page_text", "sync_headed", "schedule_enabled", "allow_local_http"):
            if not isinstance(getattr(cfg, name), bool):
                raise ValueError(name + " 必须为布尔值")
        if not isinstance(cfg.download_dir, str) or not cfg.download_dir.strip():
            raise ValueError("资料保存位置不能为空")
        if cfg.courses_url and not cfg.same_origin(cfg.courses_url):
            raise ValueError("courses_url 必须与 Blackboard 同源")
        return cfg

    def same_origin(self, url: str) -> bool:
        a, b = urlsplit(self.base_url), urlsplit(url)
        return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)

    def file_allowed(self, url: str) -> bool:
        u = urlsplit(url)
        return self.same_origin(url) or (u.scheme == "https" and u.hostname in self.allowed_file_hosts and not u.username)

    def output(self, root: Path) -> Path:
        path = Path(self.download_dir)
        return path.resolve() if path.is_absolute() else (root / path).resolve()


@dataclass
class Link:
    url: str
    text: str = ""
    kind: str = "anchor"
    download: bool = False
    scope: str = "content"


@dataclass
class Course:
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class Folder:
    id: str
    name: str


def folder_ids(crumbs) -> list[str]:
    return [crumb.id if isinstance(crumb, Folder) else "label:" + crumb for crumb in crumbs]


def resource_key(course: Course, link: Link, crumbs=()) -> str:
    source = course.id + "\n" + canonical_url(link.url)
    if crumbs:
        source += "\n" + json.dumps(folder_ids(crumbs), ensure_ascii=True)
    return digest(source)


def course_selected(cfg: Config, course: Course) -> bool:
    if cfg.course_selection_mode == "selected" and course.id not in cfg.selected_course_ids:
        return False
    target = (course.id + " " + course.name).casefold()
    include = not cfg.include_courses or any(s.casefold() in target for s in cfg.include_courses)
    return include and not any(s.casefold() in target for s in cfg.exclude_courses)


def cache_courses(root: Path, cfg: Config, courses: list[Course], complete: bool):
    payload = {"schema": "blackboard-courses/v1", "site": cfg.base_url, "discovered_at": now(),
               "complete": complete, "courses": [{"id": c.id, "name": c.name, "url": public_url(c.url)} for c in courses]}
    write_json(root / ".state" / "courses.json", payload)
    return payload


def load_course_catalog(root: Path, cfg: Config) -> dict:
    path = root / ".state" / "courses.json"
    empty = {"site": cfg.base_url, "discovered_at": "", "complete": False, "courses": []}
    if not path.exists():
        return empty
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if payload.get("site") == cfg.base_url else empty


def save_course_preferences(path: Path, mode: str, ids: list[str], include: list[str], exclude: list[str], download_dir: str | None = None) -> Config:
    # Read fresh configuration so unrelated settings are preserved when the dialog is open.
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    data.update(course_selection_mode=mode, selected_course_ids=sorted(set(ids)) if mode == "selected" else [],
                include_courses=include, exclude_courses=exclude)
    if download_dir is not None:
        if not download_dir.strip():
            raise ValueError("资料根目录不能为空")
        directory = Path(download_dir)
        data["download_dir"] = str(directory.resolve() if directory.is_absolute() else (path.parent / directory).resolve())
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        write_json(temp, data)
        cfg = Config.load(temp)  # Validate before replacing a working configuration.
        history = path.parent / ".state" / "config-history"
        history.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, history / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "__" + uuid.uuid4().hex[:8] + ".json"))
        os.replace(temp, path)
        return cfg
    finally:
        temp.unlink(missing_ok=True)


def discover_and_cache(context, cfg: Config, root: Path) -> dict:
    issues = []
    reader = BrowserReader(context, cfg, lambda code, url, message: issues.append({"code": code, "url": public_url(url), "message": message}))
    courses = reader.discover_courses()
    result = cache_courses(root, cfg, courses, complete=not issues)
    result["issues"] = issues
    return result


def is_course_entry(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return bool(course_id(url)) and ("/ultra/courses/" in path or path.endswith("/launcher") or path.endswith("/coursemain") or path.endswith("/coursehomepage"))


def is_file(link: Link) -> bool:
    u = urlsplit(link.url)
    path = u.path.lower()
    ext = Path(unquote(path)).suffix.lstrip(".")
    return (link.download or "/bbcswebdav/" in path or
            ("/attachments/" in path and path.endswith("/download")) or
            (ext in FILE_EXTENSIONS and not path.startswith("/webapps/")))


def is_navigation(url: str, cid: str, cfg: Config) -> bool:
    if not cfg.same_origin(url) or course_id(url) != cid:
        return False
    u = urlsplit(url)
    q = parse_qs(u.query)
    method = q.get("method", [""])[0].lower()
    if method and method not in {"search", "view", "display", "list", "refresh"}:
        return False
    for name in ("action", "command", "cmd", "mode"):
        if name == "mode" and u.path.lower() == "/webapps/blackboard/content/listcontent.jsp" and q.get(name) == ["reset"]:
            continue
        if re.search(r"delete|remove|edit|create|save|submit|attempt|grade|upload|send|reset", q.get(name, [""])[0], re.I):
            return False
    if u.path.lower() in NAV_PATHS:
        return True
    if "/ultra/courses/" in u.path:
        return bool(re.fullmatch(r"/ultra/courses/[^/]+/(?:outline|cl/outline)(?:/(?:folder|document)/[^/]+)?/?", u.path))
    return False


def is_discovery_navigation(link: Link, cfg: Config) -> bool:
    if not cfg.same_origin(link.url):
        return False
    u = urlsplit(link.url)
    if u.path.startswith("/ultra/course") and not course_id(link.url):
        return True
    if u.path.lower() in {"/webapps/portal/execute/tabs/tabaction", "/webapps/blackboard/execute/viewcatalog", "/webapps/blackboard/execute/courselist"}:
        return bool(re.search(r"course|课程|my|我的|next|下一", link.text, re.I))
    return False


SNAPSHOT_JS = r"""() => {
  const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const nodes = [...document.querySelectorAll('a[href],iframe[src],frame[src],embed[src],object[data],video[src],audio[src],source[src]')];
  return {
    url: location.href, title: document.title,
    heading: ((document.querySelector('#pageTitleText') || document.querySelector('main h1,[role=main] h1') || document.querySelector('h1,h2'))?.innerText || document.title).trim(),
    text: document.body?.innerText || '',
    password: [...document.querySelectorAll('input[type=password]')].some(visible),
    logged: !!document.querySelector('a[href*=logout],a[href*=Logout],#courseMenuPalette,#myCourses,#course_list'),
    links: nodes.map(e => ({url:e.href || e.src || e.data,
      text:(e.innerText || e.getAttribute('title') || e.getAttribute('aria-label') || '').trim(),
      kind:e.tagName.toLowerCase(), download:e.hasAttribute('download'),
      scope:e.closest('#courseMenuPalette,#courseMenu,#navigationPane') ? 'course-menu' :
        e.closest('#breadcrumbs,.breadcrumbs,#breadcrumb,.breadcrumb,[aria-label*=breadcrumb i]') ? 'breadcrumb' : 'content'}))
  };
}"""


class BrowserReader:
    def __init__(self, context, cfg: Config, warn):
        self.context, self.cfg, self.warn = context, cfg, warn
        self.page = context.new_page()
        self.page.set_default_timeout(cfg.timeout_seconds * 1000)

    def goto(self, url: str) -> None:
        for attempt in range(self.cfg.retries):
            try:
                response = self.page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.timeout_seconds * 1000)
                if response and response.status == 401:
                    raise AuthRequired("Blackboard 返回登录错误")
                if response and response.status == 403:
                    raise PartialRun("页面 HTTP 403：该目录不可访问")
                if response and response.status >= 400:
                    raise PartialRun(f"页面 HTTP {response.status}")
                # Wait for the rendered main content without requiring networkidle (LMS has polling).
                self.page.locator("body").wait_for(state="attached")
                self.page.wait_for_function("() => document.readyState === 'complete'", timeout=min(5000, self.cfg.timeout_seconds * 1000))
                self.page.wait_for_timeout(self.cfg.request_delay_seconds * 1000)
                self.check_auth()
                return
            except AuthRequired:
                raise
            except PartialRun:
                raise
            except Exception as exc:
                check_browser_error(exc)
                if attempt + 1 == self.cfg.retries:
                    raise PartialRun("页面访问失败：" + error_label(exc)) from None
                self.page.wait_for_timeout(min(8000, (attempt + 1) * 1000))

    def snapshots(self) -> list[dict]:
        records = []
        for frame in self.page.frames:
            if frame != self.page.main_frame and not self.cfg.same_origin(frame.url):
                continue
            if frame != self.page.main_frame and urlsplit(frame.url).path.lower().startswith("/webapps/bb-zjdxsso-"):
                continue  # Background SSO callback is not course content.
            try:
                records.append(frame.evaluate(SNAPSHOT_JS))
            except Exception as exc:
                check_browser_error(exc)
                self.warn("frame_read_failed", public_url(frame.url), error_label(exc))
        return records

    def check_auth(self) -> None:
        if not self.cfg.same_origin(self.page.url) or AUTH_PATH.search(urlsplit(self.page.url).path):
            raise AuthRequired("需要在专用浏览器登录 Blackboard")
        for record in self.snapshots():
            if record["password"] or AUTH_PATH.search(urlsplit(record["url"]).path):
                raise AuthRequired("登录会话失效，需要重新登录")
            if re.search(r"session (?:has )?expired|会话已过期|会话失效", record["text"], re.I):
                raise AuthRequired("登录会话已过期")
            if not record["logged"]:
                sign_in = re.compile(r"^(?:log\s*in|sign\s*in|统一身份认证|国际校区.*登录|INTL.*登录|登录)", re.I)
                if any(AUTH_PATH.search(urlsplit(item["url"] or "").path) or sign_in.search(item["text"])
                       for item in record["links"]):
                    raise AuthRequired("页面显示学校登录入口，需要重新登录 Blackboard")

    def links(self) -> list[Link]:
        seen = {}
        for record in self.snapshots():
            for item in record["links"]:
                url = urljoin(record["url"], item["url"] or "")
                if urlsplit(url).scheme in {"http", "https"}:
                    seen.setdefault(url, Link(url, item["text"], item["kind"], item["download"], item.get("scope", "content")))
        return list(seen.values())

    def collect_dynamic(self, discovery: bool = False) -> list[Link]:
        """Union of links across virtual scrolling; click only explicit read-only pagination."""
        collected: dict[str, Link] = {}
        stable, prev = 0, None
        for step in range(self.cfg.max_scroll_rounds):
            for link in self.links():
                collected.setdefault(link.url, link)
            metrics = self.page.evaluate("""() => [...document.querySelectorAll('body,main,[role=main],.course-list,.course-list-container,[class*=scroll]')].filter(e=>e.scrollHeight>e.clientHeight+4).map(e=>[e.scrollTop,e.scrollHeight,e.clientHeight])""")
            signature = (len(collected), json.dumps(metrics))
            stable = stable + 1 if signature == prev else 0
            if stable >= 2:
                break
            prev = signature
            self.page.evaluate("""() => { window.scrollTo(0,document.documentElement.scrollHeight); for(const e of document.querySelectorAll('main,[role=main],.course-list,.course-list-container,[class*=scroll]')) {if(e.scrollHeight>e.clientHeight+4)e.scrollTop=e.scrollHeight;} }""")
            self.page.wait_for_timeout(max(150, self.cfg.request_delay_seconds * 1000))
        else:
            self.warn("scroll_limit", public_url(self.page.url), "达到滚动上限，可能仍有未显示内容")
        # Explicit buttons do not submit forms, tests, or assignments.
        names = re.compile(r"^(?:next(?: page)?|load more|show more|下一页|加载更多|显示更多)$", re.I)
        paging_scope = "nav[aria-label*='pag' i],nav[aria-label*='分页'],.pagingcontrols,.pagingControls,.pagination,.pagination-controls,[data-testid*='pagination']"
        if discovery:
            paging_scope += ",#course_list,#myCourses,.course-list,.course-list-container"
        for _ in range(self.cfg.max_discovery_pages if discovery else 10):
            button = self.page.locator(paging_scope).get_by_role("button", name=names).filter(visible=True)
            if not button.count():
                break
            target = button.first
            if not target.is_enabled() or target.get_attribute("type") == "submit":
                break
            before = self.page.locator("body").inner_text()
            target.click()
            self.page.wait_for_timeout(max(250, self.cfg.request_delay_seconds * 1000))
            self.check_auth()
            for link in self.links():
                collected.setdefault(link.url, link)
            if self.page.locator("body").inner_text() == before:
                self.warn("pagination_unchanged", public_url(self.page.url), "分页按钮未产生可确认的变化")
                break
        else:
            self.warn("pagination_limit", public_url(self.page.url), "达到分页按钮上限")
        return list(collected.values())

    def discover_courses(self) -> list[Course]:
        self.goto(self.cfg.courses_url or self.cfg.base_url)
        queue, visited, found = [], set(), {}
        for record in self.snapshots():
            if record["url"].startswith("http"):
                visited.add(canonical_url(record["url"]))
        current_links = self.collect_dynamic(discovery=True)
        while True:
            for link in current_links:
                if self.cfg.same_origin(link.url) and is_course_entry(link.url):
                    cid = course_id(link.url)
                    name = " ".join(link.text.split()) or cid
                    if cid not in found or (found[cid].name == cid and name != cid):
                        found[cid] = Course(cid, name, link.url)
                elif is_discovery_navigation(link, self.cfg) and canonical_url(link.url) not in visited:
                    queue.append(link.url)
            if not queue:
                break
            if len(visited) >= self.cfg.max_discovery_pages:
                self.warn("discovery_limit", "", "课程列表未遍历完")
                break
            target = queue.pop(0)
            key = canonical_url(target)
            if key in visited:
                continue
            visited.add(key)
            self.goto(target)
            current_links = self.collect_dynamic(discovery=True)
        for item in self.cfg.extra_course_urls:
            cid = course_id(item["url"])
            if not cid or not self.cfg.same_origin(item["url"]) or not is_navigation(item["url"], cid, self.cfg):
                raise ValueError("extra_course_urls 必须为同源的只读课程页面")
            found[cid] = Course(cid, item.get("name", cid), item["url"])
        courses = list(found.values())
        if not courses:
            raise NoCoursesFound("没有识别到课程。请将 courses_url 设为已登录后的课程列表网址，或配置 extra_course_urls；不会将零课程视为成功")
        return courses


class Archive:
    def __init__(self, root: Path, cfg: Config):
        self.root, self.output = root, cfg.output(root)
        filesystem_path(self.output).mkdir(parents=True, exist_ok=True)
        state = root / ".state"
        state.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state / "manifest.sqlite3")
        self.db.row_factory = sqlite3.Row
        has_files = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='files'").fetchone()
        has_folders = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='folders'").fetchone()
        if has_files and not has_folders and self.db.execute("SELECT count(*) FROM files").fetchone()[0]:
            backups = state / "manifest-backups"
            backups.mkdir(exist_ok=True)
            target = sqlite3.connect(backups / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3"))
            try:
                self.db.backup(target)
            finally:
                target.close()
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS files (
          key TEXT PRIMARY KEY, course_id TEXT NOT NULL, url TEXT NOT NULL,
          filename TEXT NOT NULL, relative_path TEXT NOT NULL, sha256 TEXT NOT NULL,
          size INTEGER NOT NULL, etag TEXT, modified TEXT, seen_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS versions (
          id INTEGER PRIMARY KEY, key TEXT NOT NULL, relative_path TEXT NOT NULL,
          sha256 TEXT NOT NULL, size INTEGER NOT NULL, archived_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pages (
          key TEXT PRIMARY KEY, course_id TEXT NOT NULL, url TEXT NOT NULL,
          title TEXT NOT NULL, relative_path TEXT, sha256 TEXT, seen_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS courses (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, relative_path TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS folders (
          key TEXT PRIMARY KEY, course_id TEXT NOT NULL, name TEXT NOT NULL,
          relative_path TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def course_dir(self, course: Course) -> Path:
        row = self.db.execute("SELECT relative_path FROM courses WHERE id=?", (course.id,)).fetchone()
        relative = row[0] if row else safe_name(course.name, 65) + "__" + safe_name(course.id, 20)
        self.db.execute("INSERT INTO courses VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url", (course.id, course.name, public_url(course.url), relative))
        self.db.commit()
        path = self.output / relative
        filesystem_path(path).mkdir(parents=True, exist_ok=True)
        return path

    def checked_path(self, relative: str) -> Path:
        path = (self.output / relative).resolve()
        if not path.is_relative_to(self.output.resolve()):
            raise ValueError("目录必须位于配置的资料根目录内")
        return path

    def rebase_folder(self, old: str, new: str):
        source, target = self.checked_path(old), self.checked_path(new)
        moved = False
        try:
            if source != target and filesystem_path(source).exists():
                if filesystem_path(target).exists():
                    raise ValueError("目标目录已存在，不合并不同的 Blackboard 文件夹")
                filesystem_path(target.parent).mkdir(parents=True, exist_ok=True)
                os.rename(filesystem_path(source), filesystem_path(target))
                moved = True
            for table in ("files", "versions", "pages", "folders"):
                for record in self.db.execute(f"SELECT rowid,relative_path FROM {table}").fetchall():
                    relative = record[1]
                    if relative and (relative == old or relative.startswith(old + "/")):
                        replacement = new + relative[len(old):]
                        self.db.execute(f"UPDATE {table} SET relative_path=? WHERE rowid=?", (replacement, record[0]))
            self.db.commit()
        except Exception:
            self.db.rollback()
            if moved and filesystem_path(target).exists() and not filesystem_path(source).exists():
                os.rename(filesystem_path(self.checked_path(new)), filesystem_path(self.checked_path(old)))
            raise

    def folder_dir(self, course: Course, crumbs) -> Path:
        parent = self.course_dir(course)
        ids = []
        for crumb in crumbs:
            label = crumb.name if isinstance(crumb, Folder) else crumb
            ids.append(crumb.id if isinstance(crumb, Folder) else "label:" + crumb)
            key = digest(course.id + "\n" + json.dumps(ids, ensure_ascii=True))
            row = self.db.execute("SELECT * FROM folders WHERE key=?", (key,)).fetchone()
            name = safe_name(label, 220)
            candidate = parent / name
            foreign = {r["relative_path"].casefold() for r in self.db.execute("SELECT key,relative_path FROM folders WHERE key<>?", (key,)).fetchall()}
            index = 0
            while (self.relative(candidate).casefold() in foreign or
                   (row and self.relative(candidate).casefold() != row["relative_path"].casefold() and filesystem_path(candidate).exists())):
                index += 1
                candidate = parent / (name + "__" + key[:8] + (f"_{index}" if index > 1 else ""))
            relative = self.relative(candidate)
            self.checked_path(relative)
            if row and row["relative_path"] != relative:
                self.rebase_folder(row["relative_path"], relative)
            filesystem_path(candidate).mkdir(parents=True, exist_ok=True)
            self.db.execute("INSERT INTO folders VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name,relative_path=excluded.relative_path", (key, course.id, label, relative))
            self.db.commit()
            parent = candidate
        return parent

    def prepare_file(self, course: Course, crumbs, link: Link):
        folder = self.folder_dir(course, crumbs)
        key = resource_key(course, link, crumbs)
        previous = self.get(key)
        legacy_key = resource_key(course, link)
        if not previous and key != legacy_key:
            legacy = self.get(legacy_key)
            legacy_suffix = "__" + legacy_key[:10] + Path(legacy["filename"]).suffix if legacy else ""
            if legacy and Path(legacy["relative_path"]).name.endswith(legacy_suffix):
                self.db.execute("UPDATE files SET key=? WHERE key=?", (key, legacy_key))
                self.db.execute("UPDATE versions SET key=? WHERE key=?", (key, legacy_key))
                self.db.commit()
                previous = self.get(key)
        return key, previous, folder

    def file_path(self, folder: Path, name: str, key: str, previous) -> Path:
        name = safe_name(name, 240)
        path = folder / name
        foreign = {r["relative_path"].casefold() for r in self.db.execute("SELECT key,relative_path FROM files WHERE key<>?", (key,)).fetchall()}
        owned = previous["relative_path"].casefold() if previous else ""
        index = 0
        while self.relative(path).casefold() in foreign or (filesystem_path(path).exists() and self.relative(path).casefold() != owned):
            index += 1
            original = Path(name)
            path = folder / (safe_name(original.stem, 150) + "__" + key[:8] + (f"_{index}" if index > 1 else "") + original.suffix)
        return self.checked_path(self.relative(path))

    def normalize_file_path(self, course: Course, crumbs, link: Link, filename: str | None = None):
        key, previous, folder = self.prepare_file(course, crumbs, link)
        if previous:
            name = filename or previous["filename"]
            path = self.file_path(folder, name, key, previous)
            relative = self.relative(path)
            if relative != previous["relative_path"]:
                source = self.checked_path(previous["relative_path"])
                if filesystem_path(source).exists():
                    filesystem_path(path.parent).mkdir(parents=True, exist_ok=True)
                    if filesystem_path(path).exists():
                        raise ValueError("目标文件已存在，未覆盖旧文件")
                    os.rename(filesystem_path(source), filesystem_path(path))
                self.db.execute("UPDATE files SET relative_path=?,filename=? WHERE key=?", (relative, name, key))
                self.db.commit()
                previous = self.get(key)
        return key, previous, folder

    def get(self, key: str):
        return self.db.execute("SELECT * FROM files WHERE key=?", (key,)).fetchone()

    def valid(self, row) -> bool:
        if not row:
            return False
        path = filesystem_path(self.output / row["relative_path"])
        if not path.is_file() or path.stat().st_size != row["size"]:
            return False
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest() == row["sha256"]

    def seen(self, key: str) -> None:
        self.db.execute("UPDATE files SET seen_at=? WHERE key=?", (now(), key))
        self.db.commit()

    def relative(self, path: Path) -> str:
        return path.relative_to(self.output).as_posix()

    def keep_old(self, path: Path, key: str, sha: str, size: int) -> None:
        # Copy first: a failed download/commit can never remove the last good copy.
        target = path.parent / "_history" / (safe_name(path.stem, 180) + "__" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "__" + sha[:8] + path.suffix)
        filesystem_path(target.parent).mkdir(parents=True, exist_ok=True)
        shutil.copy2(filesystem_path(path), filesystem_path(target))
        self.db.execute("INSERT INTO versions(key,relative_path,sha256,size,archived_at) VALUES(?,?,?,?,?)", (key, self.relative(target), sha, size, now()))

    def save_file(self, course: Course, crumbs: tuple[str, ...], link: Link, name: str, data: bytes, headers: dict) -> str:
        key, previous, folder = self.normalize_file_path(course, crumbs, link, name)
        sha = digest(data)
        if previous and previous["sha256"] == sha and self.valid(previous):
            self.db.execute("UPDATE files SET etag=?,modified=?,url=?,seen_at=? WHERE key=?", (headers.get("etag"), headers.get("last-modified"), public_url(link.url), now(), key))
            self.db.commit()
            return "unchanged"
        path = self.file_path(folder, name, key, previous)
        filesystem_path(path.parent).mkdir(parents=True, exist_ok=True)
        temp = path.with_name(".bb-download-" + uuid.uuid4().hex + ".part")
        try:
            with filesystem_path(temp).open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if filesystem_path(path).exists():
                with filesystem_path(path).open("rb") as handle:
                    old_hash = hashlib.file_digest(handle, "sha256").hexdigest()
                self.keep_old(path, key, old_hash, filesystem_path(path).stat().st_size)
            os.replace(filesystem_path(temp), filesystem_path(path))
            self.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET url=excluded.url,filename=excluded.filename,relative_path=excluded.relative_path,sha256=excluded.sha256,size=excluded.size,etag=excluded.etag,modified=excluded.modified,seen_at=excluded.seen_at", (key, course.id, public_url(link.url), name, self.relative(path), sha, len(data), headers.get("etag"), headers.get("last-modified"), now()))
            self.db.commit()
        finally:
            filesystem_path(temp).unlink(missing_ok=True)
        return "updated" if previous else "downloaded"

    def save_page(self, course: Course, url: str, title: str, text: str, enabled: bool) -> None:
        key = digest(course.id + "\n" + canonical_url(url))
        sha, relative = digest(text), None
        if enabled:
            directory = self.course_dir(course) / "_pages"
            filesystem_path(directory).mkdir(exist_ok=True)
            path = directory / (safe_name(title, 60) + "__" + key[:10] + ".txt")
            previous = self.db.execute("SELECT * FROM pages WHERE key=?", (key,)).fetchone()
            if previous and previous["relative_path"]:
                path = self.output / previous["relative_path"]
            if filesystem_path(path).exists() and filesystem_path(path).read_text(encoding="utf-8") != text:
                old_bytes = filesystem_path(path).read_bytes()
                self.keep_old(path, key, digest(old_bytes), len(old_bytes))
            temp = path.with_name(path.name + ".tmp")
            filesystem_path(temp).write_text(text, encoding="utf-8")
            os.replace(filesystem_path(temp), filesystem_path(path))
            relative = self.relative(path)
        self.db.execute("INSERT INTO pages VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title,relative_path=excluded.relative_path,sha256=excluded.sha256,seen_at=excluded.seen_at", (key, course.id, public_url(url), title, relative, sha, now()))
        self.db.commit()


class Downloader:
    def __init__(self, context, cfg: Config, archive: Archive):
        self.context, self.cfg, self.archive = context, cfg, archive

    def get(self, course: Course, crumbs: tuple[str, ...], link: Link, referer: str) -> str:
        if not self.cfg.file_allowed(link.url):
            raise PartialRun("外部资源需加入 allowed_file_hosts 后下载")
        key, previous, _ = self.archive.normalize_file_path(course, crumbs, link)
        headers = {"Referer": referer}
        if previous and self.archive.valid(previous):
            if previous["etag"]:
                headers["If-None-Match"] = previous["etag"]
            elif previous["modified"]:
                headers["If-Modified-Since"] = previous["modified"]
        target, response = link.url, None
        # Bound and validate every redirect; do not follow an arbitrary SSO/file host silently.
        for _ in range(10):
            for attempt in range(self.cfg.retries):
                try:
                    response = self.context.request.get(target, headers=headers, timeout=self.cfg.timeout_seconds * 1000, max_redirects=0)
                    if response.status not in {429, 500, 502, 503, 504}:
                        break
                    retry = response.headers.get("retry-after", "")
                    delay = min(15, int(retry) if retry.isdigit() else 2 ** attempt)
                    response.dispose()
                    response = None
                    if attempt + 1 == self.cfg.retries:
                        raise PartialRun("资源多次返回服务器错误或限流")
                    time.sleep(delay)
                except PartialRun:
                    raise
                except Exception as exc:
                    check_browser_error(exc)
                    if attempt + 1 == self.cfg.retries:
                        raise PartialRun("下载网络错误：" + error_label(exc)) from None
                    time.sleep(min(8, 2 ** attempt))
            if response.status in {301, 302, 303, 307, 308}:
                location = urljoin(target, response.headers.get("location", ""))
                response.dispose()
                if AUTH_PATH.search(urlsplit(location).path):
                    raise AuthRequired("附件下载被重定向到登录页")
                if not self.cfg.file_allowed(location):
                    raise PartialRun("附件重定向到未配置的外部域名")
                if not self.cfg.same_origin(location):
                    headers = {}  # No Blackboard referer or conditional validators on external CDN.
                target = location
                continue
            break
        else:
            raise PartialRun("附件重定向次数过多")
        try:
            if response.status in {401, 403}:
                # A file-only 403 may be an unavailable attachment; preserve per-file evidence.
                if response.status == 401:
                    raise AuthRequired("附件登录失效")
                raise PartialRun("附件 HTTP 403：无权读取该资源")
            if response.status == 304:
                if not self.archive.valid(previous):
                    raise PartialRun("服务器返回 304 但本地文件缺失或损坏")
                self.archive.seen(key)
                return "unchanged"
            if response.status != 200:
                raise PartialRun(f"附件 HTTP {response.status}")
            meta = response.headers
            declared = meta.get("content-length", "")
            if declared.isdigit() and int(declared) > self.cfg.max_file_mb * 1024 * 1024:
                raise PartialRun("附件超过配置的大小上限")
            data = response.body()
            if len(data) > self.cfg.max_file_mb * 1024 * 1024:
                raise PartialRun("附件超过配置的大小上限")
            if not data:
                raise PartialRun("服务器返回空文件")
            content_type = meta.get("content-type", "")
            sample = data[:65536].decode("utf-8", errors="ignore")
            looks_html = "html" in content_type or bool(re.match(r"\s*(?:<!doctype\s+html|<html)", sample, re.I))
            if looks_html and re.search(r"type\s*=\s*['\"]?password|/webapps/login|session (?:has )?expired|统一身份认证|会话已过期", sample, re.I):
                raise AuthRequired("附件返回了登录网页，未将它保存成课件")
            name = filename_from_headers(meta.get("content-disposition", ""), target, link.text, content_type)
            if looks_html and Path(name).suffix.lower() not in {".html", ".htm"}:
                raise PartialRun("附件返回 HTML 错误/预览页，未保存为原文件")
            return self.archive.save_file(course, crumbs, link, name, data, meta)
        finally:
            try:
                response.dispose()
            except Exception as exc:
                if type(exc).__name__ != "TargetClosedError":
                    raise


class SyncEngine:
    def __init__(self, context, cfg: Config, root: Path):
        self.context, self.cfg, self.root = context, cfg, root
        self.archive = Archive(root, cfg)
        self.report = {"schema": "blackboard-autosync/v1", "run_id": uuid.uuid4().hex,
                       "started_at": now(), "finished_at": None, "status": "running", "complete": False,
                       "site": public_url(cfg.base_url), "download_dir": str(self.archive.output),
                       "courses": [], "counts": {"downloaded": 0, "updated": 0, "unchanged": 0, "failed": 0, "pages": 0},
                       "issues": [], "external_links": []}
        self.reader = BrowserReader(context, cfg, self.warn)
        self.downloader = Downloader(context, cfg, self.archive)
        self.last_checkpoint = 0.0

    def checkpoint(self, force: bool = False):
        if force or time.monotonic() - self.last_checkpoint >= 1:
            self.report["updated_at"] = now()
            publish_report(self.root, self.report)
            self.last_checkpoint = time.monotonic()

    def discover_for_sync(self):
        try:
            courses = self.reader.discover_courses()
            cache_courses(self.root, self.cfg, courses, complete=not self.report["issues"])
            self.report["course_list_source"] = "live"
            return courses
        except NoCoursesFound:
            if self.cfg.course_selection_mode != "selected":
                raise
            catalog = load_course_catalog(self.root, self.cfg)
            courses = [Course(item["id"], item["name"], item["url"]) for item in catalog["courses"]
                       if item["id"] in self.cfg.selected_course_ids and self.cfg.same_origin(item["url"])
                       and (is_course_entry(item["url"]) or is_navigation(item["url"], item["id"], self.cfg))]
            if not courses:
                raise
            self.report["course_list_source"] = "cached_selected"
            self.report["catalog_discovered_at"] = catalog["discovered_at"]
            self.warn("course_list_unavailable", "", "课程列表未能刷新，尝试已选课程的已知入口；各课程的访问权限和资料将重新验证")
            print("课程列表未能刷新，正在尝试已选课程的已知入口。", flush=True)
            return courses

    def warn(self, code: str, url: str, message: str, cid: str = ""):
        issue = {"code": code, "url": public_url(url), "message": message, "course_id": cid}
        if issue not in self.report["issues"]:
            self.report["issues"].append(issue)

    def selected(self, course: Course) -> bool:
        return course_selected(self.cfg, course)

    def crawl(self, course: Course):
        self.report["current_course"] = course.name
        self.checkpoint(force=True)
        item = {"id": course.id, "name": course.name, "url": public_url(course.url), "status": "running", "pages": 0, "files": 0}
        self.report["courses"].append(item)
        self.archive.course_dir(course)
        queue, visited, resources = [(course.url, ())], set(), set()
        baseline = len(self.report["issues"])
        while queue:
            if len(visited) >= self.cfg.max_pages_per_course:
                self.warn("page_limit", course.url, "达到课程页面上限，仍有目录未遍历", course.id)
                break
            url, crumbs = queue.pop(0)
            key = canonical_url(url)
            if key in visited:
                continue
            visited.add(key)
            self.report["current_page"] = public_url(url)
            self.checkpoint()
            try:
                self.reader.goto(url)
                # Do not let a course-page redirect silently move into another course.
                actual_cid = course_id(self.reader.page.url)
                if actual_cid and actual_cid != course.id:
                    raise PartialRun("课程页面重定向到其他课程")
                records = self.reader.snapshots()
                for record in records:
                    if record["text"]:
                        self.archive.save_page(course, record["url"], record["heading"], record["text"], self.cfg.save_page_text)
                links = self.reader.collect_dynamic()
                document_urls = {canonical_url(record["url"]) for record in records}
                item["pages"] += 1
                self.report["counts"]["pages"] += 1
                title = next((r["heading"] for r in records if r["heading"]), "")
                # A direct attachment directory keeps the original folder's title.
                page_crumbs = crumbs
                if not crumbs and urlsplit(url).path.lower().endswith("/listcontent.jsp"):
                    content_id = parse_qs(urlsplit(url).query).get("content_id", [canonical_url(url)])[0]
                    page_crumbs = (Folder(content_id, title or content_id),)
                self.archive.folder_dir(course, page_crumbs)
                if "/ultra/" in urlsplit(url).path:
                    self.warn("ultra_requires_adapter", url, "Ultra 仅扫描已渲染的只读链接；动态文档/折叠模块需按实际学校界面补充适配，当前覆盖不视为完整", course.id)
                for link in links:
                    if canonical_url(link.url) in document_urls:
                        continue  # In-page menus/anchors do not open another content page.
                    if is_file(link):
                        file_key = resource_key(course, link, page_crumbs)
                        if file_key in resources:
                            continue
                        resources.add(file_key)
                        try:
                            label = link.text or unquote(urlsplit(link.url).path.rsplit("/", 1)[-1])
                            if label.startswith(("https://", "http://")):
                                label = public_url(label)
                            print("  正在检查文件：" + label[:120], flush=True)
                            status = self.downloader.get(course, page_crumbs, link, url)
                            self.report["counts"][status] += 1
                            item["files"] += 1
                            names = {"downloaded": "已下载", "updated": "已更新", "unchanged": "未变化"}
                            print("  " + names[status] + "：" + label[:120], flush=True)
                        except (AuthRequired, SyncInterrupted):
                            raise
                        except Exception as exc:
                            self.report["counts"]["failed"] += 1
                            message = str(exc) if isinstance(exc, PartialRun) else error_label(exc)
                            self.warn("download_failed", link.url, message, course.id)
                            print("  下载失败：" + message, flush=True)
                        self.checkpoint()
                        time.sleep(self.cfg.request_delay_seconds)
                    elif is_navigation(link.url, course.id, self.cfg):
                        if link.scope == "breadcrumb":
                            continue
                        if canonical_url(link.url) not in visited:
                            q = parse_qs(urlsplit(link.url).query)
                            is_child = bool(q.get("content_id")) and q.get("content_id") != parse_qs(urlsplit(url).query).get("content_id")
                            label = " ".join(link.text.split())
                            identity = q.get("content_id", [canonical_url(link.url)])[0]
                            if link.scope == "course-menu":
                                child_crumbs = (Folder(identity, label or identity),)
                            elif is_child:
                                child_crumbs = page_crumbs + (Folder(identity, label or identity),)
                            else:
                                child_crumbs = page_crumbs
                            queue.append((link.url, child_crumbs))
                    elif link.kind in {"iframe", "frame"} and self.cfg.same_origin(link.url) and course_id(link.url) == course.id:
                        self.warn("unsupported_frame", link.url, "课程包含未适配的框架页面", course.id)
                    elif not self.cfg.same_origin(link.url):
                        external = {"course_id": course.id, "text": link.text, "url": public_url(link.url)}
                        if external not in self.report["external_links"]:
                            self.report["external_links"].append(external)
                    elif course_id(link.url) == course.id and not is_course_entry(link.url):
                        # Tests, discussion tools, and LTI links are indexed but never entered.
                        self.warn("unsupported_course_link", link.url, "已记录但未进入的课程工具/未知页面", course.id)
            except SyncInterrupted:
                item["status"] = "interrupted"
                raise
            except AuthRequired:
                item["status"] = "auth_required"
                raise
            except Exception as exc:
                self.warn("page_failed", url, str(exc) if isinstance(exc, PartialRun) else error_label(exc), course.id)
        item["status"] = "partial" if len(self.report["issues"]) > baseline else "synced"
        self.checkpoint(force=True)

    def run(self):
        try:
            self.report["selection_mode"] = self.cfg.course_selection_mode
            self.report["configured_course_ids"] = list(self.cfg.selected_course_ids)
            if self.cfg.course_selection_mode == "selected" and not self.cfg.selected_course_ids:
                self.report.update(status="paused", selected_courses=0, message="未勾选课程，本轮暂停同步")
                return self.report
            self.report["current_step"] = "读取课程清单"
            print("正在读取 Blackboard 课程清单……", flush=True)
            self.checkpoint(force=True)
            courses = self.discover_for_sync()
            if self.cfg.course_selection_mode == "selected":
                missing = set(self.cfg.selected_course_ids) - {course.id for course in courses}
                for cid in sorted(missing):
                    self.warn("selected_course_unavailable", "", "已选课程 " + cid + " 未出现在本轮课程列表中", cid)
            selected = [course for course in courses if self.selected(course)]
            self.report["discovered_courses"] = len(courses)
            self.report["selected_courses"] = len(selected)
            if not selected:
                raise PartialRun("课程筛选后为零门，未执行同步")
            for index, course in enumerate(selected, 1):
                self.report["current_step"] = f"同步课程 {index}/{len(selected)}"
                print(f"同步课程 [{index}/{len(selected)}]：" + course.name, flush=True)
                self.crawl(course)
            self.report["status"] = "partial" if self.report["issues"] else "synced"
            self.report["complete"] = not self.report["issues"]
        except SyncInterrupted as exc:
            self.report["status"] = "interrupted"
            self.warn("interrupted", "", str(exc))
        except AuthRequired as exc:
            self.report["status"] = "auth_required"
            self.warn("auth_required", "", str(exc))
        except Exception as exc:
            self.report["status"] = "failed"
            self.warn("run_failed", "", str(exc) if isinstance(exc, PartialRun) else error_label(exc))
        finally:
            self.report["finished_at"] = now()
            publish_report(self.root, self.report)
            self.archive.close()
        return self.report


def publish_report(root: Path, report: dict):
    reports = root / "reports"
    write_json(reports / "latest.json", report)
    if report.get("finished_at"):
        write_json(reports / (report["started_at"].replace(":", "-") + "__" + report["run_id"][:8] + ".json"), report)
    status_names = {"synced": "已完成当前支持页面的同步", "partial": "部分同步：请检查遗漏", "failed": "同步失败", "auth_required": "需要重新登录", "paused": "已暂停：没有勾选课程", "running": "正在同步", "not_connected": "系统已安装，等待连接 Blackboard"}
    status_names["interrupted"] = "同步中断：已保存完成文件，重新同步可继续"
    escape = html.escape
    rows = "".join(f'<tr><td>{escape(c["name"])}</td><td>{escape(c["status"])}</td><td>{c["pages"]}</td><td>{c["files"]}</td></tr>' for c in report["courses"])
    issues = "".join(f'<li><b>{escape(i["code"])}</b> · {escape(i["message"])}<br><small>{escape(i["url"])}</small></li>' for i in report["issues"])
    external = "".join(f'<li>{escape(i["text"])} <a href="{escape(i["url"], quote=True)}" rel="noreferrer">{escape(i["url"])}</a></li>' for i in report["external_links"])
    counts = report["counts"]
    refresh = '<meta http-equiv="refresh" content="5">' if report["status"] == "running" else ""
    progress = ''
    if report["status"] == "running":
        progress = '<p>当前步骤：' + escape(report.get("current_step", "准备同步")) + '<br>当前课程：' + escape(report.get("current_course", "正在读取清单")) + '<br><small>更新时间：' + escape(report.get("updated_at", report["started_at"])) + ' · 此页每 5 秒刷新</small></p>'
    doc = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'">
<title>Blackboard 自动同步 · 状态</title><style>body{{font:16px/1.7 system-ui,sans-serif;color:#142c42;background:#f2f6fa;margin:0}}main{{max-width:1000px;margin:40px auto;padding:28px;background:white;border-radius:18px}}h1{{margin:0;font-size:28px}}.status{{padding:16px;background:#edf4fc;border-radius:10px;font-size:20px}}.cards{{display:flex;flex-wrap:wrap;gap:14px;margin:24px 0}}.card{{background:#f5f8fb;padding:14px 24px;border-radius:10px}}.card strong{{display:block;font-size:28px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #e0e8ee;text-align:left;padding:10px}}small{{overflow-wrap:anywhere;color:#617182}}li{{margin:14px 0}}a{{color:#2169a8}}code{{overflow-wrap:anywhere}}@media(max-width:600px){{main{{margin:10px;padding:18px}}}}</style>
<main><h1>Blackboard 自动同步</h1><p>{escape(report["site"])} · {escape(report.get("finished_at") or report["started_at"])}</p>
<p class="status">{status_names.get(report["status"], escape(report["status"]))}</p>
{progress}
<div class="cards">{''.join(f'<div class="card">{label}<strong>{counts[key]}</strong></div>' for key,label in [("downloaded","新增文件"),("updated","更新文件"),("unchanged","未变化"),("failed","下载失败"),("pages","浏览页面")])}</div>
<p>原文件目录：<code>{escape(report["download_dir"])}</code></p>
<p>更新前的文件保存在对应目录的 <code>_history</code>；网页可见文字保存在 <code>_pages</code>。</p>
<h2>课程同步情况</h2><table><tr><th>课程</th><th>状态</th><th>页面</th><th>文件</th></tr>{rows}</table>
<h2>待处理事项</h2><ul>{issues or '<li>本轮没有发现待处理项。</li>'}</ul>
<h2>外部资源链接</h2><ul>{external or '<li>本轮没有记录外部链接。</li>'}</ul>
<p><small>覆盖范围：已发现课程中的已发布、可访问、可识别的只读内容目录及直接文件链接。未知工具、外部平台和 Ultra 动态内容会单独报告。synced 不代表学校所有历史/隐藏课程或外部平台均已备份。</small></p></main></html>'''
    reports.mkdir(parents=True, exist_ok=True)
    temp = reports / "latest.html.tmp"
    temp.write_text(doc, encoding="utf-8")
    os.replace(temp, reports / "latest.html")


@contextlib.contextmanager
def single_instance(root: Path):
    """OS-owned lock is released on crashes; never delete a live lock by guessing PID."""
    state = root / ".state"
    state.mkdir(parents=True, exist_ok=True)
    handle = (state / "run.lock").open("a+b")
    acquired = False
    try:
        # Windows locks make even reads fail in other processes. Initialize by size,
        # not by reading the locked byte, and keep all I/O inside the close guard.
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    except OSError as exc:
        if not acquired:
            raise RuntimeError("已有同步/登录任务在运行，请关闭专用登录窗口后重试") from exc
        raise
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def browser_context(playwright, cfg: Config, root: Path, headed: bool):
    opts = {"headless": not headed, "channel": cfg.browser_channel or "chromium", "accept_downloads": True, "viewport": {"width": 1440, "height": 1000},
            "locale": "zh-CN", "timeout": cfg.timeout_seconds * 1000}
    if cfg.browser_channel:
        opts["channel"] = cfg.browser_channel
    context = playwright.chromium.launch_persistent_context(str(root / ".state" / "browser-profile"), **opts)
    try:
        restore_browser_session(context, cfg, root)
    except Exception:
        context.close()
        raise
    return context


def protect_session(data: bytes, decrypt: bool = False) -> bytes:
    """Windows DPAPI, current-user scope; no machine-wide credential access."""
    if os.name != "nt":
        raise RuntimeError("本系统的会话加密需要 Windows")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                          ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    description = None if decrypt else "Blackboard AutoSync session"
    if not operation(ctypes.byref(source), description, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def save_browser_session(context, cfg: Config, root: Path):
    session_storage = {}
    for page in context.pages:
        if not cfg.same_origin(page.url) or AUTH_PATH.search(urlsplit(page.url).path):
            continue
        with contextlib.suppress(Exception):
            session_storage.update(page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage))"))
    data = json.dumps({"site": cfg.base_url, "saved_at": now(), "cookies": context.cookies(),
                       "session_storage": session_storage}, ensure_ascii=True).encode("utf-8")
    encrypted = protect_session(data)
    state = root / ".state"
    state.mkdir(parents=True, exist_ok=True)
    temp, target = state / "session.dpapi.tmp", state / "session.dpapi"
    with temp.open("wb") as handle:
        handle.write(encrypted)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def restore_browser_session(context, cfg: Config, root: Path):
    path = root / ".state" / "session.dpapi"
    if not path.exists():
        return
    try:
        payload = json.loads(protect_session(path.read_bytes(), decrypt=True))
    except Exception:
        # A moved/damaged cache is never trusted. Preserve it and let normal login proceed.
        print("加密会话缓存无法读取，将使用专用浏览器现有会话；需要时请重新登录。", flush=True)
        return
    if payload.get("site") != cfg.base_url:
        return
    cookies = payload.get("cookies", [])
    if cookies:
        context.add_cookies(cookies)
    entries = payload.get("session_storage", {})
    if entries:
        origin = urlsplit(cfg.base_url)
        actual_origin = f"{origin.scheme}://{origin.netloc}"
        script = "(() => { const data=" + json.dumps(entries, ensure_ascii=True) + "; if(location.origin===" + json.dumps(actual_origin) + ") { for(const [k,v] of Object.entries(data)) sessionStorage.setItem(k,v); } })();"
        context.add_init_script(script=script)


def login(context, cfg: Config, root: Path, sync_after: bool) -> int:
    page = context.pages[0] if context.pages else context.new_page()
    print("请在打开的专用浏览器完成学校登录和验证。程序只保存浏览器会话，不读取密码。", flush=True)
    try:
        page.goto(cfg.base_url, wait_until="domcontentloaded", timeout=cfg.timeout_seconds * 1000)
    except Exception as exc:
        print("登录入口暂时无法访问：" + error_label(exc), flush=True)
    deadline = time.monotonic() + cfg.login_timeout_seconds
    while time.monotonic() < deadline:
        try:
            if not context.pages:
                print("登录浏览器已关闭；会话未验证。", flush=True)
                return 3
            for tab in context.pages:
                if not cfg.same_origin(tab.url) or AUTH_PATH.search(urlsplit(tab.url).path):
                    continue
                record = tab.evaluate(SNAPSHOT_JS)
                has_course = any(is_course_entry(urljoin(tab.url, item["url"] or "")) for item in record["links"])
                if not record["password"] and (record["logged"] or has_course):
                    write_json(root / ".state" / "login-verified.json", {"verified_at": now(), "site": cfg.base_url})
                    print("已检测到登录成功。", flush=True)
                    if sync_after:
                        report = SyncEngine(context, cfg, root).run()
                        print_sync_result(report, root)
                        return exit_code(report["status"])
                    return 0
            page.wait_for_timeout(1500)
        except Exception:
            time.sleep(.5)
    print("等待登录超时，请重新运行 login；未宣称登录成功。", flush=True)
    return 3


def exit_code(status: str) -> int:
    return {"synced": 0, "paused": 0, "partial": 2, "auth_required": 3, "failed": 1}.get(status, 1)


def print_sync_result(report: dict, root: Path):
    names = {"synced": "同步完成", "partial": "部分同步完成，请检查待处理事项", "failed": "同步失败", "auth_required": "需要重新登录", "paused": "已暂停，未选择课程"}
    print("状态：" + names.get(report["status"], report["status"]), flush=True)
    reasons = {}
    for issue in report["issues"]:
        reasons[issue["message"]] = reasons.get(issue["message"], 0) + 1
    for message, count in reasons.items():
        print("原因：" + message + (f"（{count} 处；详情见报告）" if count > 1 else ""), flush=True)
    print(json.dumps({"status": report["status"], "counts": report["counts"], "report": str(root / "reports" / "latest.html")}, ensure_ascii=False, indent=2), flush=True)


def main(argv=None) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Blackboard 课程资料增量同步")
    parser.add_argument("command", choices=["login", "sync", "discover", "status", "doctor"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--sync-after-login", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = Config.load(args.config)
        if args.command == "status":
            path = ROOT / "reports" / "latest.json"
            print(path.read_text(encoding="utf-8") if path.exists() else "尚未运行同步。")
            return 0
        from playwright.sync_api import sync_playwright
        if args.command == "doctor":
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, channel=cfg.browser_channel or "chromium")
                page = browser.new_page()
                page.set_content("<title>Blackboard AutoSync runtime OK</title>")
                result = {"python": sys.version.split()[0], "browser": browser.version, "config_valid": True,
                          "base_url": cfg.base_url, "runtime_ok": page.title() == "Blackboard AutoSync runtime OK",
                          "login_verified": (ROOT / ".state" / "login-verified.json").exists(), "live_access_verified": False}
                browser.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        with single_instance(ROOT), sync_playwright() as pw:
            context = browser_context(pw, cfg, ROOT, args.command == "login" or args.headed or cfg.sync_headed)
            try:
                if args.command == "login":
                    return login(context, cfg, ROOT, args.sync_after_login)
                if args.command == "discover":
                    result = discover_and_cache(context, cfg, ROOT)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0 if result["complete"] else 2
                report = SyncEngine(context, cfg, ROOT).run()
                print_sync_result(report, ROOT)
                return exit_code(report["status"])
            finally:
                try:
                    save_browser_session(context, cfg, ROOT)
                except Exception as exc:
                    if type(exc).__name__ != "TargetClosedError":
                        raise
                finally:
                    with contextlib.suppress(Exception):
                        context.close()
    except AuthRequired as exc:
        print("需要登录：" + str(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        print("执行失败：" + (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else error_label(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
