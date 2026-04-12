---
description: "Rules for README, config.toml, and user-facing documentation"
applyTo: "README.md,streamrip/config.toml,tests/test_config*.toml"
---

# Documentation and config rules

Apply these rules when editing README or configuration files.

## Config comments

- `streamrip/config.toml` is user-facing documentation.
- Any new option must include:
  - what it does
  - valid values or format
  - safe default
  - an example if the format is not obvious
- Reuse an existing section before creating a new section.

## README updates

- Keep README concise and practical.
- Prefer copy/paste-ready commands.
- Document user-visible changes, not internal refactors.
- Avoid repeating the same instructions in multiple places.

## Behaviour notes

- If a feature is best-effort or non-fatal, say so explicitly.
- If rerunning is safe/idempotent, say so explicitly.
- If a setting reuses another config section intentionally, explain that briefly.

## Examples

- Keep examples realistic and aligned with actual CLI/config behaviour.
- Do not document options or defaults that are not implemented.
