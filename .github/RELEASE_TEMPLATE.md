# Release notes template

<!-- This file documents the expected CHANGELOG.md format used by the release workflow. -->
<!-- The workflow (release-assets.yml) extracts release notes automatically from CHANGELOG.md. -->

## How releases are generated

1. Update `VERSION` and add an entry to `CHANGELOG.md` using the format below.
2. Commit the changes to the default branch.
3. Either push a tag (`vYYYY.MM.DD`) or trigger the workflow manually with the target version.

## CHANGELOG.md heading format

The workflow recognises the following heading styles:

```markdown
## [YYYY.MM.DD]
## [YYYY.MM.DD] - YYYY-MM-DD
```

Pre-release tags (e.g. `vYYYY.MM.DD-beta`) map to the base date heading (`## [YYYY.MM.DD]`).

## Example CHANGELOG entry

```markdown
## [2026.04.09] - 2026-04-09

### Added
- New feature description

### Fixed
- Bug fix description

### Changed
- Changed behaviour description
```

## Fallback

If no matching heading is found in `CHANGELOG.md`, the release notes will contain only a link
to `CHANGELOG.md` at the release tag.
