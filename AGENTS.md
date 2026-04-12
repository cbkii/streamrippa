# AGENTS.md

## Purpose

This file is the authoritative engineering guide for AI coding agents working in this repository.

Follow it for all tasks unless the user explicitly overrides a specific point in the prompt. When this file conflicts with older comments or ad hoc prompt habits, prefer this file.

---

## Project snapshot

- Project: `streamrippa` / `streamrip`
- Language: Python
- Packaging: Poetry
- CLI entry point: `rip`
- Key areas:
  - `streamrip/rip/` CLI and orchestration
  - `streamrip/client/` service clients and downloadability logic
  - `streamrip/media/` pending media orchestration and batching
  - `streamrip/metadata/` metadata models and file tagging
  - `streamrip/db.py` download and failure tracking
  - `streamrip/config.toml` default config schema and user-facing comments
  - `tests/` pytest suite
  - `.github/workflows/` CI and release automation

---

## Core working rules

1. Keep diffs tight.
   - Prefer extracting or adapting existing code over adding parallel systems.
   - Reuse existing classes, helpers, config sections, patterns, and naming wherever practical.

2. Preserve current behaviour unless the task explicitly requires a change.
   - New features must be additive where possible.
   - Existing CLI flows, config semantics, and service behaviour must not regress.

3. Reuse existing TOML config variables first.
   - Do not invent new config knobs if an existing one already fits the semantics.
   - If a new config knob is truly necessary, keep it small, well-commented, and placed in the most relevant existing section.

4. Reliability matters.
   - Large-batch operations must use bounded concurrency or batching.
   - Never schedule unbounded resolver/download coroutines for large lists.
   - Prefer existing retry, backoff, failure logging, and progress-reporting patterns.

5. Metadata work is best-effort only.
   - Tagging and metadata enrichment must never fail an otherwise successful download.
   - Log metadata failures clearly and continue.

6. Be source-aware.
   - Deezer and Qobuz IDs can collide numerically.
   - Downloaded/failure bookkeeping must not create false skips across sources.

7. Favour deterministic behaviour.
   - Stable ordering, stable dedupe, stable ranking, stable logs.
   - Do not use unordered `set()` behaviour where result ordering or reproducibility matters.

8. Match repo style.
   - Use plain Python and the repo’s existing async patterns.
   - Keep imports, naming, typing, logging, and control flow consistent with nearby code.

---

## Build, test, lint

Use Poetry.

### Setup

```bash
poetry install
```

### Run tests

```bash
poetry run pytest
```

### Run lint and format checks

```bash
poetry run ruff check .
poetry run ruff format --check .
```

### If you changed formatting or lint-sensitive files

```bash
poetry run ruff check . --fix
poetry run ruff format .
```

Do not claim work is complete unless the relevant tests pass, or you explicitly state what could not be run.

---

## Validation requirements

For any meaningful code change, validate the smallest relevant set below:

- Unit tests for touched modules
- Regression tests for adjacent existing behaviour
- `poetry run pytest`
- `poetry run ruff check .`
- `poetry run ruff format --check .`

When changing:
- config parsing: add/update config tests
- metadata/tagging: add/update tagger/meta tests
- download reliability / failure handling: add/update reliability tests
- service client logic: add/update service-specific tests
- `rip file` / CLI mode handling: add/update CLI or orchestration tests
- GitHub Actions: keep workflow syntax valid and aligned with current workflow style

---

## Change-control rules

### CLI and orchestration
- Keep CLI UX consistent with existing command naming and option style.
- Prefer extending `rip file` and existing orchestration over adding new top-level commands.
- Do not silently change defaults unless required and documented.

### Config and comments
- `streamrip/config.toml` is user-facing documentation.
- Any new config option must include:
  - concise purpose
  - valid values or format
  - examples if non-obvious
  - safe default
- Reuse existing config sections before adding new sections.

### Async and batching
- Prefer the project’s current batching / `asyncio.gather(..., return_exceptions=True)` style where it already exists.
- Cap concurrency for large batch resolution/search flows.
- Honour existing fail-fast semantics where relevant.

### Database and failure handling
- Keep DB changes minimal and backwards-aware.
- Avoid destructive DB migrations unless absolutely necessary.
- Preserve existing failed-download DB flow for provider-backed retries.
- Query-resolution failures should be logged clearly even if they are not yet repairable.

### Metadata and tagging
- Extend the existing metadata/tagger pipeline instead of bolting on a second metadata writer.
- For custom tags:
  - FLAC: Vorbis comments
  - MP3: TXXX
  - MP4/M4A: freeform atoms
- Tagging must be isolated so one failed tag does not block the rest.

### Service logic
- Preserve existing non-targeted service behaviour.
- If a task requires narrower service behaviour for one flow, implement it as a local or opt-in path rather than changing the default for every flow.

---

## Coding conventions for this repo

- Python 3.10+
- Use type hints where surrounding code already does
- Keep functions focused and small where practical
- Prefer explicit names over clever abstractions
- Avoid adding heavy dependencies unless the task explicitly justifies them
- For repair-only or fallback-only advanced behaviour, keep the heavier logic confined to that path
- Use existing logging/progress conventions instead of introducing new output styles

---

## Documentation update rules

Update documentation when behaviour changes.

Usually this means touching one or more of:
- `README.md`
- `streamrip/config.toml`
- relevant inline docstrings/comments
- tests that document behaviour by example

Do not add long prose when a short config comment or concise README example is enough.

---

## What to do before finishing a task

1. Re-read the prompt and confirm every requirement is implemented.
2. Check for regressions in adjacent flows.
3. Run or reason through the relevant validation commands.
4. Summarise:
   - what changed
   - what was reused
   - what tests were added/updated
   - any known limitations or deferred follow-up work

---

## Preferred references

Use official docs first, then battle-tested upstream repos and examples.

### Official docs
- GitHub Copilot custom instructions:
  - https://docs.github.com/copilot/customizing-copilot/adding-custom-instructions-for-github-copilot
- GitHub Copilot coding agent task guidance:
  - https://docs.github.com/copilot/how-tos/agents/copilot-coding-agent/best-practices-for-using-copilot-to-work-on-tasks
- VS Code Copilot custom instructions:
  - https://code.visualstudio.com/docs/copilot/customization/custom-instructions
- Python `asyncio`:
  - https://docs.python.org/3/library/asyncio.html
- Python `csv`:
  - https://docs.python.org/3/library/csv.html
- Python `sqlite3`:
  - https://docs.python.org/3/library/sqlite3.html
- Mutagen docs:
  - https://mutagen.readthedocs.io/
- aiohttp docs:
  - https://docs.aiohttp.org/
- pytest docs:
  - https://docs.pytest.org/
- Ruff docs:
  - https://docs.astral.sh/ruff/
- Poetry docs:
  - https://python-poetry.org/docs/
- GitHub Actions workflow syntax:
  - https://docs.github.com/actions/using-workflows/workflow-syntax-for-github-actions

### Upstream / battle-tested repos
- Upstream streamrip:
  - https://github.com/nathom/streamrip
- Mutagen source:
  - https://github.com/quodlibet/mutagen
- beets:
  - https://github.com/beetbox/beets
- yt-dlp:
  - https://github.com/yt-dlp/yt-dlp
- aiohttp:
  - https://github.com/aio-libs/aiohttp
- GitHub Awesome Copilot examples:
  - https://github.com/github/awesome-copilot

When examples conflict:
1. Prefer this repo’s current patterns.
2. Then prefer official docs.
3. Then prefer upstream `nathom/streamrip`.
4. Only then borrow from other battle-tested repos.
