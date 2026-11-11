# 下载之后，按课次交接

下载选择与学习整理范围分开配置：可以同步全部课程，但只把指定课程和课型交给学习整理。队列只保存来源索引和处理状态，不把学校材料上传到 GitHub。

## 三个自然日，不是三个上课日

`study_window.py` 可将交接限制在北京时间今天及未来两天。文件必须对应已核实课表中的课程和课次；取消、暂定或来源已变化的课次不进入待派发范围。提前下载的文件到上课窗口时仍会进入复核，不因当天下载数为零而漏掉。

课表、来源映射和路由均保存在本机 `.state/study-handoff/`。未配置课表门时保持原有队列行为；启用 `study_window.require_timetable` 后必须提供真实来源，不猜测日期。

## 常用命令

在项目目录运行。`init` 首次运行时将已有资料记为基线；已有队列时不会覆盖。之后用 `scan` 查找新增或更新来源。

```powershell
python study_queue.py init
python study_queue.py scan
python study_queue.py review
python study_queue.py status
```

`review` 包括可交接来源、缺少映射的来源、每个课次的覆盖情况及排除的课型。`summary_policy` 可以分别选择 Lecture 与 Practical 的课程列表，也可以让某门课程始终按理论课处理。未启用该策略时沿用既有分类；下载课程选择不会因此改变。

### 配置可选课表门

在 `.state/study-handoff/routing.json` 中配置下面的结构。`course-a`、`course-b` 应替换为本机课程 ID；启用课程/课型策略时同时启用课表门并完成来源映射。

```json
{
  "study_window": {"require_timetable": true},
  "summary_policy": {
    "enabled": true,
    "lecture_course_ids": ["course-a", "course-b"],
    "guidance_practical_course_ids": ["course-b"],
    "theory_only_course_ids": []
  }
}
```

`timetable.json` 使用 `{"events": [...]}`，每个课次包含 `id`、`course_id`、`date`（`YYYY-MM-DD`）、`kind`（如 `Lecture` 或 `Practical`），以及 `source` 的 `path`、`sha256`、`reference`。后者分别指本机保存的官方课表、该文件摘要和课次所在页/行；课表变化后应重新核对来源。`cancelled` 和 `tentative` 可排除取消或暂定课次。

核对完资料与课次的对应关系后，用 `map --ids SOURCE_ID --event-ids EVENT_ID` 保存映射，再运行 `review`。这些索引需要由使用者或已有协调流程提供，本程序不会自行抓取课表或猜测日期。

使用 `batch --lane lecture|guidance --ids ...` 建批；`sending` 应在发送前记录，确认目标已接收后才标 `sent`。程序准备批次，不会自行创建聊天或替用户发送消息。接收方和发送方式须由实际运行环境配置。

## 收到不等于完成

`sent` 只表示交接已送达。`completed` 需要匹配批次与来源的成果回执、PDF 校对记录，以及协调方独立取得的 Drive 文件和父目录读回证据。Drive 元数据能核对归档存在与位置，不能证明远端文件内容与本地逐字节相同。

被用户暂停、跳过或已发送的条目保留原状态，不因每日复核而重新派发。课表来源更新后重新核对受影响映射，不重建整条队列。

## 可选每日入口

`daily_sync.py --dry-run` 仅查看当天是否到期。`install-daily-catchup.ps1` 是可选的每日 20:00（本机计划任务时间）和登录补跑入口，队列窗口使用北京时间。它会改变 Windows 计划任务，只有需要这套时间安排时才运行；普通间隔同步仍由原设置界面管理。

`daily_sync.py` 在当天无需重新下载时仍复核已进入课次窗口的材料。需要密码、MFA 或验证码时在学校页面人工完成；程序不会读取或代填凭据。

## 公开分发范围

只分发程序、模拟测试和通用配置。不要提交 `config.json`、`.state/`、课表、真实聊天/日历/Drive ID、下载文件、日志或成果回执。

## English overview

The study queue separates downloaded course selection from summary routing. Its optional timetable gate uses today plus the next two Shanghai calendar days, revisits early downloads when their classes enter that window, and leaves cancelled or unverifiable classes out of dispatch. `sent` means delivered, not completed; completion needs output and independent archive evidence. The queue prepares batches but does not send messages by itself. The optional daily scheduler changes local Windows tasks only when explicitly installed. All accounts, routing IDs, timetables and course files stay local.
