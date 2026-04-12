---
description: "Rules for Python source and tests in streamrippa"
applyTo: "streamrip/**/*.py,tests/**/*.py"
---

# Python rules for streamrippa

Apply these rules when editing Python source or tests.

## Architecture and scope

- Prefer small adaptations of existing classes/functions over new subsystems.
- Reuse existing async patterns, batching style, logging style, and test fixtures.
- Preserve existing public behaviour unless the task explicitly changes it.

## Async and reliability

- Do not create unbounded task fan-out for large lists or search flows.
- Use bounded batching or a semaphore where appropriate.
- Prefer existing `asyncio.gather(..., return_exceptions=True)`-style patterns already used in the repo.
- Respect existing fail-fast and retry semantics where relevant.

## Config and state

- Reuse existing config keys before adding new ones.
- Keep DB changes backwards-aware and narrow.
- Avoid source-agnostic ID bookkeeping when source-aware handling is required.

## Metadata and tagging

- Metadata/tagging must be best-effort and non-fatal.
- Extend the existing metadata/tagger pipeline instead of writing tags in a separate layer.
- Unknown/custom tags should use container-appropriate custom tag mechanisms.

## Tests

- Add or update focused tests for changed behaviour.
- Add regression coverage for adjacent existing flows.
- Avoid changing tests only to fit a buggy implementation; fix the implementation or update tests only when behaviour intentionally changed.

## Style

- Match nearby code style and naming.
- Keep imports organised and compatible with Ruff.
- Prefer straightforward code and small helpers over deep abstraction.
