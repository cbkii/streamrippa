# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [2026.04.09] - 2026-04-09

### Added
- Continue-on-error: one failed track or album no longer aborts the whole run; failures are logged and the run continues
- Bounded retry with exponential backoff: configurable retry count and delay per track download
- FLAC integrity validation: corrupt FLAC files are detected with `mutagen`, removed, and recorded for replay
- Persistent failed-item tracking: every failure is written to the SQLite failed-downloads database and an auditable CSV log
- `rip repair` command: replay all previously failed items without redoing successful ones
- Session summary: every run ends with a concise table showing succeeded / failed / skipped / retried / validation-failures
- Non-zero exit codes: the process exits with code 1 when any item fails, making scripted/CI use reliable
- `--fail-fast` flag: stop after the first failure for strict pipelines
- Release workflow: tag-push and workflow_dispatch triggers, CHANGELOG-based release notes, GitHub CLI publishing

### Changed
- Forked from [nathom/streamrip](https://github.com/nathom/streamrip) with reliability-focused enhancements
- Switched to date-based versioning (CalVer YYYY.MM.DD)

## [2.2.1]

### Changed
- Initial fork release based on upstream streamrip 2.2.1
