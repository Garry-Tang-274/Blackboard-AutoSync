# Changelog

## Unreleased

- Add same-origin INTL SSO entry detection and an optional one-attempt session-reuse step; passwords, MFA and CAPTCHA remain manual.
- Publish the locally used study queue, timetable window and summary-routing modules with synthetic regression tests.
- Review previously downloaded material when its class enters the current three-calendar-day window, even if no new download occurs.
- Distinguish dispatch, delivery and independently verified archive completion; retain paused and skipped work.
- Add the optional 20:00/logon catch-up scheduler without changing the standard interval-sync entry point.

## 1.0.0

- Chinese settings interface with course selection, folder picker, school URL, browser mode, request interval, retries, file size limit and page text options.
- Configurable Windows automatic synchronization interval and enable/disable switch.
- Optional verified copying of previously synchronized files when changing the storage root, retaining the old location and refusing conflicting files.
- Nested Blackboard folders, original filenames, SHA-256 verification, incremental updates and version history.
- Live progress report, interruption handling and encrypted local browser session reuse.
- Generic configuration template; personal configuration, reports, sessions and course downloads are excluded from source distributions.
