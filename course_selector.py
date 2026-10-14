"""Local Windows course selection dialog. Never downloads files while discovering."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from bb_sync import Config, ROOT, load_course_catalog
from user_settings import save_settings


def parse_keywords(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in re.split(r"[,，;；\n]+", value) if part.strip()))


class CourseSelector:
    def __init__(self, window: tk.Tk, root: Path = ROOT):
        self.window, self.root = window, root
        self.config_path = root / "config.json"
        self.cfg = Config.load(self.config_path)
        self.catalog = load_course_catalog(root, self.cfg)
        self.course_vars: dict[str, tk.BooleanVar] = {}
        self.events = queue.Queue()
        self.busy = False
        window.title("Blackboard AutoSync · 设置")
        window.geometry("960x880")
        window.minsize(760, 820)
        window.configure(bg="#f3f6fa")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fa")
        style.configure("TLabel", background="#f3f6fa", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=7)
        style.configure("TRadiobutton", background="#f3f6fa", font=("Microsoft YaHei UI", 10))
        style.configure("TCheckbutton", background="white", font=("Microsoft YaHei UI", 10), padding=9)
        shell = ttk.Frame(window, padding=16)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="Blackboard AutoSync", font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w")
        ttk.Label(shell, text="课程资料自动同步 · 设置、保存位置与进度", foreground="#61718a").pack(anchor="w", pady=(2, 10))
        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill="both", expand=True)
        outer = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(outer, text="  同步课程  ")
        ttk.Label(outer, text="选择需要同步的课程", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        ttk.Label(outer, text="保存后，手动同步和 Windows 定时任务都使用同一份选择。").pack(anchor="w", pady=(6, 12))
        ttk.Label(outer, text="当前 Blackboard：" + self.cfg.base_url).pack(anchor="w")
        self.download_dir = tk.StringVar(value=str(self.cfg.output(root)))
        self.mode = tk.StringVar(value=self.cfg.course_selection_mode)
        ttk.Radiobutton(outer, text="全部课程（包括以后新增的课程）", variable=self.mode, value="all", command=self.all_mode).pack(anchor="w", pady=(12, 4))
        ttk.Radiobutton(outer, text="仅同步勾选的课程（新增课程不会自动加入）", variable=self.mode, value="selected", command=self.update_summary).pack(anchor="w")
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=12)
        self.refresh_button = ttk.Button(bar, text="读取课程清单", command=lambda: self.fetch(False))
        self.refresh_button.pack(side="left")
        self.login_button = ttk.Button(bar, text="首次登录并读取清单", command=lambda: self.fetch(True))
        self.login_button.pack(side="left", padx=8)
        ttk.Button(bar, text="全选当前清单", command=lambda: self.set_checks(True)).pack(side="right")
        ttk.Button(bar, text="清空选择 / 暂停", command=lambda: self.set_checks(False)).pack(side="right", padx=8)
        search_row = ttk.Frame(outer)
        search_row.pack(fill="x", pady=(0, 8))
        ttk.Label(search_row, text="搜索课程：").pack(side="left")
        self.search = tk.StringVar()
        ttk.Entry(search_row, textvariable=self.search).pack(side="left", fill="x", expand=True)
        self.search.trace_add("write", lambda *_: self.render_courses())
        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(list_frame, height=120, bg="white", highlightthickness=1, highlightbackground="#d8e1ec")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.rows = tk.Frame(self.canvas, bg="white")
        self.row_window = self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.row_window, width=event.width))
        self.canvas.bind("<MouseWheel>", self.scroll)
        self.summary = tk.StringVar()
        ttk.Label(outer, textvariable=self.summary).pack(anchor="w", pady=(8, 3))
        self.catalog_label = tk.StringVar()
        ttk.Label(outer, textvariable=self.catalog_label, foreground="#61718a", wraplength=880).pack(anchor="w")
        filters = ttk.LabelFrame(outer, text="可选：按课程名称关键词筛选（逗号分隔）", padding=10)
        filters.pack(fill="x", pady=12)
        self.include = tk.StringVar(value=", ".join(self.cfg.include_courses))
        self.exclude = tk.StringVar(value=", ".join(self.cfg.exclude_courses))
        ttk.Label(filters, text="只包含：").grid(row=0, column=0, sticky="w")
        ttk.Entry(filters, textvariable=self.include).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(filters, text="排除：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(filters, textvariable=self.exclude).grid(row=1, column=1, sticky="ew", padx=8, pady=(6, 0))
        filters.columnconfigure(1, weight=1)
        ttk.Label(outer, text="关键词会进一步限制勾选结果；留空则只按上面的课程选择同步。", foreground="#61718a").pack(anchor="w")
        self.build_preferences()
        footer = ttk.Frame(shell)
        footer.pack(fill="x", pady=(14, 0))
        self.notice = tk.StringVar(value="请选择课程，然后保存。读取清单只浏览课程列表，不下载课件。")
        ttk.Label(footer, textvariable=self.notice, wraplength=620).pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="保存全部设置", command=self.save).pack(side="right")
        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="打开资料文件夹", command=self.open_downloads).pack(side="left")
        ttk.Button(actions, text="查看同步进度", command=self.open_progress).pack(side="left", padx=8)
        ttk.Button(actions, text="立即同步", command=self.start_sync).pack(side="right")
        self.tabs.pack_forget()
        footer.pack_forget()
        actions.pack_forget()
        actions.pack(side='bottom', fill='x', pady=(10, 0))
        footer.pack(side='bottom', fill='x', pady=(14, 0))
        self.tabs.pack(fill='both', expand=True)
        self.reset_variables()
        self.render_courses()
        window.after(150, self.poll)

    def build_preferences(self):
        general, advanced = (ttk.Frame(self.tabs, padding=20) for _ in range(2))
        self.tabs.add(general, text='  保存与同步  ')
        self.tabs.add(advanced, text='  连接与下载  ')
        self.settings = {}
        for panel in (general, advanced):
            panel.columnconfigure(1, weight=1)
        def field(parent, row, name, label, value):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', pady=10)
            variable = tk.StringVar(value=str(value))
            self.settings[name] = variable
            ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky='ew', padx=(12, 0), pady=10)
        def toggle(parent, row, name, label, value):
            variable = tk.BooleanVar(value=value)
            self.settings[name] = variable
            ttk.Checkbutton(parent, text=label, variable=variable).grid(row=row, column=0, columnspan=2, sticky='w', pady=8)
        ttk.Label(general, text='资料保存与自动同步', font=('Microsoft YaHei UI', 17, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
        ttk.Label(general, text='保存位置').grid(row=1, column=0, sticky='w')
        destination = ttk.Frame(general)
        destination.grid(row=1, column=1, sticky='ew', padx=(12, 0))
        ttk.Entry(destination, textvariable=self.download_dir).pack(side='left', fill='x', expand=True)
        ttk.Button(destination, text='浏览…', command=self.pick_directory).pack(side='right', padx=(6, 0))
        self.copy_existing = tk.BooleanVar(value=False)
        ttk.Checkbutton(general, text='更换位置时复制已有同步资料（保留旧位置）', variable=self.copy_existing).grid(row=2, column=0, columnspan=2, sticky='w', pady=12)
        ttk.Label(general, text='按课程保存，保持 Blackboard 文件夹结构；同名不同内容的文件不会被覆盖。', wraplength=720, foreground='#61718a').grid(row=3, column=0, columnspan=2, sticky='w', pady=(0, 14))
        toggle(general, 4, 'schedule_enabled', '启用 Windows 自动同步（开机且当前用户已登录时运行）', self.cfg.schedule_enabled)
        field(general, 5, 'schedule_every_hours', '同步间隔（1–24 小时）', self.cfg.schedule_every_hours)
        toggle(general, 6, 'sync_headed', '显示专用浏览器（学校不兼容后台模式时勾选）', self.cfg.sync_headed)
        toggle(general, 7, 'save_page_text', '同时保存网页可见文字到 _pages 文件夹', self.cfg.save_page_text)
        ttk.Label(general, text='关闭自动同步会停用本项目的计划任务，仍可手动同步。\n首次登录请使用课程页的登录按钮，密码只在学校页面输入。', wraplength=720, foreground='#61718a').grid(row=8, column=0, columnspan=2, sticky='w', pady=14)
        ttk.Label(advanced, text='学校入口与下载选项', font=('Microsoft YaHei UI', 17, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
        for row, name, label in [(1, 'base_url', 'Blackboard 根网址（HTTPS）'), (2, 'courses_url', '课程列表网址（可留空）'), (3, 'request_delay_seconds', '请求间隔（秒）'), (4, 'timeout_seconds', '网络超时（秒）'), (5, 'retries', '网络重试次数'), (6, 'max_file_mb', '单文件大小上限（MB）')]:
            field(advanced, row, name, label, getattr(self.cfg, name))
        field(advanced, 7, 'allowed_file_hosts', '外部文件域名（逗号分隔）', ', '.join(self.cfg.allowed_file_hosts))
        ttk.Label(advanced, text='更换学校入口会清空课程选择，请重新登录并读取清单。\n外部域名只填主机名，例如 files.example.edu；无需填写账号或密码。', wraplength=720, foreground='#61718a').grid(row=8, column=0, columnspan=2, sticky='w', pady=16)

    def open_downloads(self):
        path = self.cfg.output(self.root)
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def open_progress(self):
        path = self.root / 'reports' / 'latest.html'
        if path.exists():
            os.startfile(str(path))
        else:
            messagebox.showinfo('暂无进度', '首次同步后即可查看进度。', parent=self.window)

    def start_sync(self):
        if self.save():
            subprocess.Popen(['powershell.exe', '-NoProfile', '-File', str(self.root / 'run.ps1'), '-Mode', 'sync'], cwd=self.root, creationflags=subprocess.CREATE_NEW_CONSOLE)

    def scroll(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def pick_directory(self):
        chosen = filedialog.askdirectory(parent=self.window, title="选择固定资料根目录", initialdir=self.download_dir.get())
        if chosen:
            self.download_dir.set(str(Path(chosen).resolve()))

    def courses(self) -> list[dict]:
        records = {record["id"]: record for record in self.catalog["courses"]}
        # Keep unavailable selected IDs, even after a partial or changed course listing.
        preserved_ids = set(self.cfg.selected_course_ids)
        if self.mode.get() == "selected":
            preserved_ids.update(cid for cid, variable in self.course_vars.items() if variable.get())
        for cid in sorted(preserved_ids):
            records.setdefault(cid, {"id": cid, "name": "[清单中暂未显示的已选课程] " + cid, "url": ""})
        return list(records.values())

    def reset_variables(self):
        for course in self.courses():
            cid = course["id"]
            if cid not in self.course_vars:
                self.course_vars[cid] = tk.BooleanVar(value=self.mode.get() == "all" or cid in self.cfg.selected_course_ids)

    def render_courses(self):
        for widget in self.rows.winfo_children():
            widget.destroy()
        query = self.search.get().casefold().strip()
        records = self.courses()
        if not records:
            text = "尚未读取到课程清单。\n\n已登录时点击“读取课程清单”；首次使用点击“首次登录并读取清单”。\n也可以选择“全部课程”，在下方“只包含”填写课程简称，保存后按关键词筛选。\n\n若学校入口不同，请先修改 config.json 中的 base_url。"
            tk.Label(self.rows, text=text, bg="white", fg="#61718a", justify="left", font=("Microsoft YaHei UI", 11), wraplength=780, padx=20, pady=24).pack(anchor="w")
        for course in records:
            if query and query not in (course["name"] + " " + course["id"]).casefold():
                continue
            checkbox = ttk.Checkbutton(self.rows, text=course["name"] + "    [" + course["id"] + "]",
                                       variable=self.course_vars[course["id"]], command=self.check_changed)
            checkbox.pack(fill="x", anchor="w")
            checkbox.bind("<MouseWheel>", self.scroll)
        stamp = self.catalog.get("discovered_at") or "尚未读取"
        suffix = "；清单读取可能不完整，原有选择已保留。" if self.catalog["courses"] and not self.catalog.get("complete") else ""
        self.catalog_label.set("课程清单时间：" + stamp + suffix)
        self.update_summary()

    def update_summary(self):
        count = sum(variable.get() for variable in self.course_vars.values())
        if self.mode.get() == "all":
            text = "模式：全部已发现课程；包含今后新增课程。"
        else:
            text = f"已勾选 {count} 门课程。" + (" 未勾选课程时暂停同步，不会自动改成全部课程。" if count == 0 else " 搜索不会清除其他已勾选课程。")
        self.summary.set(text)

    def check_changed(self):
        self.mode.set("selected")
        self.update_summary()

    def all_mode(self):
        for variable in self.course_vars.values():
            variable.set(True)
        self.update_summary()

    def set_checks(self, value: bool):
        self.mode.set("selected")
        for cid, variable in self.course_vars.items():
            if value and cid not in {course["id"] for course in self.catalog["courses"]}:
                continue
            variable.set(value)
        self.update_summary()

    def save(self):
        try:
            if self.busy:
                raise ValueError('正在读取课程清单，请稍后再保存')
            fresh = Config.load(self.config_path)
            if fresh.base_url != self.cfg.base_url:
                raise ValueError("Blackboard 入口在窗口打开后已更改，请重新打开课程选择器。")
            ids = [cid for cid, variable in self.course_vars.items() if variable.get()]
            values = {name: variable.get() for name, variable in self.settings.items()}
            for name in ('schedule_every_hours', 'timeout_seconds', 'retries', 'max_file_mb'):
                try:
                    values[name] = int(values[name])
                except ValueError:
                    raise ValueError('小时、超时、重试和大小上限需填写整数') from None
            values['request_delay_seconds'] = float(values['request_delay_seconds'])
            values['allowed_file_hosts'] = [host.lower() for host in parse_keywords(values['allowed_file_hosts'])]
            values.update(download_dir=self.download_dir.get(), course_selection_mode=self.mode.get(),
                          selected_course_ids=ids if self.mode.get() == 'selected' else [],
                          include_courses=parse_keywords(self.include.get()), exclude_courses=parse_keywords(self.exclude.get()))
            self.notice.set('正在保存设置并校验资料…')
            self.window.update_idletasks()
            previous_site = self.cfg.base_url
            self.cfg, copied = save_settings(self.config_path, values, copy_existing=self.copy_existing.get(), expected_site=previous_site)
            if self.cfg.base_url != previous_site:
                self.catalog = load_course_catalog(self.root, self.cfg)
                self.course_vars.clear()
                self.mode.set('selected')
                self.reset_variables()
                self.render_courses()
            self.copy_existing.set(False)
            self.notice.set('已保存。' + (f'已校验复制 {copied} 个文件，旧位置保留。' if copied else '下次同步生效，已有资料保留。'))
            script = self.root / 'schedule.ps1'
            if script.exists():
                result = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(script), '-Action', 'apply'], capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    self.notice.set('设置已保存；自动任务未更新，请查看 Windows 任务权限。')
            self.update_summary()
            return True
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.window)
            return False

    def fetch(self, login_first: bool):
        if self.busy:
            return
        self.busy = True
        self.refresh_button.configure(state="disabled")
        self.login_button.configure(state="disabled")
        self.notice.set("请在专用浏览器完成学校登录，之后自动读取课程清单。" if login_first else "正在读取课程清单，不下载文件……")
        def worker():
            try:
                env = dict(os.environ, PYTHONUTF8="1")
                python = str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable
                commands = [[python, str(self.root / "bb_sync.py"), command, "--config", str(self.config_path)]
                            for command in (["login", "discover"] if login_first else ["discover"])]
                for command in commands:
                    result = subprocess.run(command, cwd=self.root, env=env, capture_output=True, text=True,
                                            encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    if result.returncode not in {0, 2}:
                        detail = (result.stderr or result.stdout).strip()
                        raise RuntimeError(detail or "读取课程失败，请检查入口、登录状态和网络。")
                self.events.put((True, "课程清单已更新。勾选课程后点击“保存选择”。"))
            except Exception as exc:
                self.events.put((False, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            success, message = self.events.get_nowait()
            self.busy = False
            self.refresh_button.configure(state="normal")
            self.login_button.configure(state="normal")
            if success:
                self.catalog = load_course_catalog(self.root, self.cfg)
                self.reset_variables()
                self.render_courses()
            self.notice.set(message)
            if not success:
                messagebox.showerror("课程清单读取失败", message, parent=self.window)
        except queue.Empty:
            pass
        self.window.after(150, self.poll)


def main():
    window = tk.Tk()
    try:
        CourseSelector(window)
    except Exception as exc:
        messagebox.showerror("配置无法读取", str(exc), parent=window)
        window.destroy()
        return
    window.mainloop()


if __name__ == "__main__":
    main()
