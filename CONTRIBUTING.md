# Contributing to streamrippa

Thanks for contributing.

This repository name is `streamrippa`, while the installable package and CLI remain `streamrip` / `rip`.

## 1) Local development quickstart

## Prerequisites

- Python 3.10+
- Poetry
- `ffmpeg` (recommended for conversion/video-related flows)

On Debian / Raspberry Pi OS:

```bash
sudo apt update
sudo apt install -y python3-full python3-venv ffmpeg
```

## Setup

```bash
git clone https://github.com/cbkii/streamrippa.git
cd streamrippa
poetry install
```

Run the CLI from the project virtualenv:

```bash
poetry run rip --help
```

## 2) Validation commands (before opening a PR)

Run the same checks used in repository guidance:

```bash
poetry run pytest
poetry run ruff check .
poetry run ruff format --check .
```

If you intentionally fixed formatting/lint issues:

```bash
poetry run ruff check . --fix
poetry run ruff format .
```

## 3) Runtime paths and generated artifacts

`streamrip` writes user data outside the repository by default.

Typical Linux paths:

- Config: `~/.config/streamrip/config.toml`
- Downloads DB: `~/.config/streamrip/downloads.db`
- Failed downloads DB: `~/.config/streamrip/failed_downloads.db`
- Failed downloads CSV log: `~/.config/streamrip/failed_downloads.csv`
- Default downloads folder: `~/StreamripDownloads`

For safe local development/testing, prefer overriding locations via `--config-path` and config values (`[downloads].folder`, `[database].*path`) so you do not mix test runs with real media libraries.

## 4) AI-agent and contributor guardrails

- Keep diffs tight and reuse existing patterns/classes.
- Preserve existing CLI/config behavior unless the task explicitly changes it.
- Keep metadata/tagging best-effort: failures should not fail successful downloads.
- Avoid unbounded concurrency for large lists; follow existing batching/semaphore patterns.

See [AGENTS.md](./AGENTS.md) for full engineering policy and change-control rules.

## 5) Troubleshooting quick notes

- SSL verification errors: check system CA certs first; only disable verification temporarily with `--no-ssl-verify` if needed.
- Provider auth/token failures: refresh credentials using existing `rip config` flows and verify relevant config sections.
- Slow/unstable networks: tune `[reliability]` retry/backoff and `[downloads]` concurrency/timeouts.
- Conversion issues: verify `ffmpeg` is installed and available in `PATH`.
