# Blackboard AutoSync

Windows 本地 Blackboard 课程资料同步工具。提供中文设置界面，按课程保存文件并保留 Blackboard 文件夹层级，支持增量检查、历史版本和实时进度报告。

## 开始使用

需要 Windows 10/11、Python 3.11 或以上（安装时包含 Tcl/Tk 并勾选加入 PATH）。当前本地验证环境为 Python 3.12。

1. 下载 Release 中的源码包或克隆仓库，解压到固定目录。
2. 双击 `SETUP.cmd` 安装依赖和 Chromium。首次安装会从 `config.example.json` 创建本机 `config.json`。
3. 双击 `CONFIGURE.cmd`，在“连接与下载”填写学校 Blackboard 的 HTTPS 根网址；在“保存与同步”选择资料保存位置，保存。
4. 在“同步课程”点击“首次登录并读取清单”，在学校页面完成登录；勾选课程并保存。
5. 点击“立即同步”，或双击 `SYNC.cmd`。点击“查看同步进度”或双击 `STATUS.cmd` 查看结果。

账号、密码和 MFA 只在学校页面输入。学校要求再次验证时，需要人工完成验证，之后恢复同步。没有勾选课程时暂停同步，不会自动改成同步全部课程。

## 可配置设置

| 页面 | 可以设置 |
| --- | --- |
| 同步课程 | 全部课程或指定课程、搜索、包含/排除关键词 |
| 保存与同步 | 保存文件夹、复制已有同步资料、自动同步开关和 1–24 小时间隔、显示专用浏览器、保存网页文字 |
| 连接与下载 | Blackboard 根网址、课程列表网址、请求间隔、超时、重试次数、单文件大小上限、外部文件域名 |

更换保存位置时，可勾选“复制已有同步资料”：只复制来源索引登记的课程目录，校验文件内容，保留旧位置。新位置存在不同内容的同名文件时停止，不覆盖。未勾选时已有文件仍留在旧位置，下次同步写入新位置。课程目录内部的 BB 层级保持不变。

设置保存前会验证参数并备份原配置。正在同步时无法保存设置；请等待完成。更换学校入口会清空旧课程选择，需要重新登录并读取清单。高级配置还可直接编辑本机 `config.json`，字段见 `bb_sync.py` 中的 `Config`。

## 文件与进度

```text
选择的资料根目录/
  课程目录/
    Blackboard 原目录/原文件名.pdf
    原目录/_history/更新前的版本
    _pages/网页可见文字.txt
项目目录/
  config.json                  本机设置，不提交 Git
  .state/                      来源索引、加密会话、配置备份和日志
  reports/latest.html          当前进度和结果
  reports/latest.json          当前机器可读结果
```

运行时 HTML 进度页每 5 秒刷新，显示当前课程和文件计数。大附件在完成前可能暂时没有新增计数。中断后重新执行 `SYNC.cmd`：重新遍历课程，校验已有文件并使用服务器缓存；未完成附件重新获取，不支持单个大文件的断点续传。

原文件保持原内容，不做 OCR 或格式转换。同目录重名冲突、Windows 不允许的字符和超长文件名会作必要处理；完整目录深度保留。文件更新时保存旧版本，学校删除资料不会删除本地文件。同一附件出现在多个 BB 目录时，各目录保留对应副本。

## 自动同步

在“保存与同步”开启自动同步并选择间隔，保存后更新本项目的 Windows 计划任务。首次成功连接并同步课程后才创建任务；之后按间隔及用户登录运行。关闭选项会停用本项目任务，手动同步仍可用。

电脑需开机、当前用户已登录，且校园网络或 VPN 可用。会话失效时，请重新登录。学校若不兼容后台浏览器，可勾选“显示专用浏览器”；同步时不要关闭该窗口。

```powershell
.\schedule.ps1 -Action status
.\schedule.ps1 -Action apply
.\schedule.ps1 -Action disable
.\schedule.ps1 -Action remove
```

移除任务不会删除资料或登录会话。项目目录变更会产生不同任务名；移动项目前请移除旧任务。

## 支持范围

- Blackboard Original：课程菜单、内容文件夹、分页、可识别的只读公告及直接附件链接。
- Blackboard Ultra：当前只识别已渲染的只读链接；折叠模块、动态文档及无链接卡片可能需要学校专用适配，会报告覆盖不完整。
- 未进入的课程工具、外部平台和未知页面在报告中列出。不会进入考试、作业提交、编辑、讨论发言或成绩工具。
- 外部文件仅在允许的 HTTPS 文件域名中下载；外部视频平台、LTI、DRM 和第三方云盘不保证支持。
- 文件下载使用 Playwright API 请求，响应在内存中读取；大文件耗时和内存占用较高，可降低单文件上限。

`partial` 表示存在待核查事项，即使附件下载没有失败也可能是未适配工具。`synced` 只表示当前支持且可访问的范围完成，不代表隐藏内容、未发布文件或外部平台均已备份。模拟测试通过不能替代真实学校页面验证。

## 隐私与发布

登录会话使用当前 Windows 用户的 DPAPI 加密，专用浏览器配置留在本机。配置、会话、来源索引、课程清单、报告、日志和下载资料均被 Git 忽略；仓库和 Release 只分发程序、测试、说明及通用配置模板。请勿手动上传 `.state`、`config.json` 或课程资料。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe bb_sync.py doctor
.\.venv\Scripts\python.exe bb_sync.py sync
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用本地模拟 Blackboard 和真实 Chromium，覆盖目录层级、版本、损坏修复、会话失效、只读导航、配置验证、复制冲突及课程选择。Windows 设置窗口使用 Tkinter；完整测试应在 Windows 运行，包含 GUI 和 DPAPI 专属检查。

退出码：`0` 已同步或暂停；`1` 失败/中断；`2` 部分同步；`3` 需要登录。操作系统锁阻止手动任务与定时任务同时写入。

技术参考：[Playwright 登录会话](https://playwright.dev/python/docs/auth)、[持久浏览器配置](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[请求 API](https://playwright.dev/python/docs/api/class-apirequestcontext)。
