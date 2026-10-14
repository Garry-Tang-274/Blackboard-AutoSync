"""Validated local preferences and optional archive copying; no credentials UI."""
from __future__ import annotations
import json
import contextlib
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import uuid
from datetime import datetime
import bb_sync as bb

EDITABLE = {'base_url', 'courses_url', 'download_dir', 'course_selection_mode', 'selected_course_ids',
            'include_courses', 'exclude_courses', 'request_delay_seconds', 'timeout_seconds', 'retries',
            'max_file_mb', 'save_page_text', 'sync_headed', 'schedule_enabled', 'schedule_every_hours',
            'allowed_file_hosts'}

def sha(path: Path):
    with bb.filesystem_path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()

def copy_archive(root: Path, old: Path, new: Path):
    if old == new:
        return 0
    manifest = root / '.state' / 'manifest.sqlite3'
    if not manifest.exists():
        return 0
    with contextlib.closing(sqlite3.connect(manifest)) as db:
        folders = [row[0] for row in db.execute('SELECT relative_path FROM courses')]
    plan, dirs = [], []
    for relative in folders:
        source, target = (old / relative).resolve(), (new / relative).resolve()
        if not source.is_relative_to(old) or not target.is_relative_to(new):
            raise ValueError('课程目录超出资料根目录')
        if source.is_relative_to(target) or target.is_relative_to(source):
            raise ValueError('新的保存位置不能嵌套在原课程目录内')
        if not source.exists():
            continue
        if (old / relative).is_symlink():
            raise ValueError('课程目录包含链接，请选择其他位置')
        dirs.append(target)
        for path in source.rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                raise ValueError('课程目录包含链接，复制已停止')
            destination = target / path.relative_to(source)
            if path.is_dir():
                if destination.exists() and not destination.is_dir():
                    raise ValueError('新位置存在同名文件：' + str(destination))
                dirs.append(destination)
            elif path.is_file():
                fingerprint = sha(path)
                if destination.exists() and (not destination.is_file() or sha(destination) != fingerprint):
                    raise ValueError('新位置存在不同内容的同名文件，未覆盖：' + str(destination))
                if not destination.resolve().is_relative_to(new):
                    raise ValueError('目标文件超出资料根目录')
                plan.append((path, destination, fingerprint))
    for directory in dirs:
        if not directory.resolve().is_relative_to(new):
            raise ValueError('目标目录超出资料根目录')
        bb.filesystem_path(directory).mkdir(parents=True, exist_ok=True)
    for source, target, fingerprint in plan:
        if not target.exists():
            temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                shutil.copy2(bb.filesystem_path(source), bb.filesystem_path(temp))
                if sha(temp) != fingerprint:
                    raise RuntimeError('复制校验失败，设置尚未更改')
                os.replace(bb.filesystem_path(temp), bb.filesystem_path(target))
            finally:
                bb.filesystem_path(temp).unlink(missing_ok=True)
    return len(plan)

def save_settings(path: Path, values: dict, *, copy_existing=False, expected_site=None):
    unknown = set(values) - EDITABLE
    if unknown:
        raise ValueError('未知设置：' + ', '.join(sorted(unknown)))
    root = path.parent
    with bb.single_instance(root):
        old = bb.Config.load(path)
        if expected_site is not None and old.base_url != expected_site:
            raise ValueError('入口已被其他窗口更改，请重新打开设置')
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        data.update(values)
        data.setdefault('download_dir', old.download_dir)
        if not str(data['download_dir']).strip():
            raise ValueError('资料保存位置不能为空')
        directory = Path(str(data['download_dir']).strip()).expanduser()
        data['download_dir'] = str(directory.resolve() if directory.is_absolute() else (root / directory).resolve())
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            bb.write_json(temp, data)
            cfg = bb.Config.load(temp)
            if cfg.base_url != old.base_url:
                if copy_existing:
                    raise ValueError('更换学校入口时，请先保存入口，再单独更改保存位置')
                data.update(selected_course_ids=[], course_selection_mode='selected', extra_course_urls=[])
                bb.write_json(temp, data)
                cfg = bb.Config.load(temp)
            copied = copy_archive(root, old.output(root), cfg.output(root)) if copy_existing else 0
            bb.filesystem_path(cfg.output(root)).mkdir(parents=True, exist_ok=True)
            history = root / '.state' / 'config-history'
            history.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, history / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json'))
            os.replace(temp, path)
            return cfg, copied
        finally:
            temp.unlink(missing_ok=True)
