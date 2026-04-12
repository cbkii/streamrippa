# streamrippa Copilot Instructions

Follow `AGENTS.md` in the repository root as the primary engineering guide for all tasks in this repo.

## Non-negotiable working rules

- Keep changes minimal and local.
- Reuse existing code paths, config variables, naming, and test patterns before adding new abstractions.
- Preserve current behaviour unless the task explicitly requires a change.
- Use Poetry for install, test, lint, and formatting.
- Do not introduce new config knobs if an existing TOML variable already fits.
- Large-batch work must use bounded concurrency or batching.
- Metadata/tagging work is always best-effort and must never fail a successful track or batch.
- Be source-aware for download/failure tracking so numeric IDs from different services do not collide.
- Update tests and docs for any user-visible behaviour change.

## Repository commands

```bash
poetry install
poetry run pytest
poetry run ruff check .
poetry run ruff format --check .
```

## Repo-specific expectations

- `streamrip/config.toml` is part of the user-facing interface; keep comments clear and accurate.
- Prefer extending `rip file` and current orchestration over adding new top-level commands.
- Prefer extending the existing metadata/tagger pipeline rather than writing metadata in a second place.
- For service-specific edge cases, preserve default behaviour for existing non-targeted flows.
- For repair-only advanced behaviour, it is acceptable to keep heavier logic confined to repair paths.

## Before finishing

Confirm:
- relevant tests added or updated
- no obvious regression in adjacent flows
- README/config comments updated if behaviour changed
- summary explains what was reused and any deferred follow-up work
