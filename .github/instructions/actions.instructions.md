---
description: "Rules for GitHub Actions workflows in this repo"
applyTo: ".github/workflows/**/*.yml,.github/workflows/**/*.yaml"
---

# GitHub Actions rules

Apply these rules when editing GitHub Actions workflows.

## Workflow style

- Match the current workflow style already used in this repository.
- Keep workflow changes minimal and readable.
- Prefer official actions or well-established actions already in use here.

## Validation

- Keep workflow syntax valid.
- Preserve current triggers unless the task explicitly changes them.
- Do not silently broaden permissions.
- Use the smallest necessary permissions for each job.

## Python / Poetry jobs

- Follow the repo’s current Poetry-based setup pattern unless the task requires otherwise.
- Keep CI commands aligned with local developer commands where practical:
  - `poetry install`
  - `poetry run pytest`
  - `poetry run ruff check .`
  - `poetry run ruff format --check .`

## Release workflows

- Be careful with version bumping, tagging, release notes, and asset naming.
- Preserve existing release behaviour unless the task explicitly changes it.
