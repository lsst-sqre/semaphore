---
name: project-mechanics
description: Project-specific build/test/lint/typing commands for this repo. Read this skill at the start of any phase that runs validation (`stoker-work`, `stoker-fixup`, `stoker-rebase`).
---

# Project mechanics

This file is the source of truth for how this repo runs tests, lint,
and type-checking. Profile-shipped phase skills read it at the start
of each phase and use the named commands verbatim.

## Test commands

- `focused_test`: `uv run --only-group=nox nox -s test -- tests/path/file_test.py::test_name`
- `complete_test`: `uv run --only-group=nox nox -s test`

The `test` session starts a PostgreSQL container through
testcontainers, so a Docker daemon must be running.

## Lint

- `lint_touched`: `uv run --only-group=lint prek run --files {files}`
- `lint_all`: `uv run --only-group=nox nox -s lint`

## Typing

- `typing`: `uv run --only-group=nox nox -s typing`

## Final validation

End-of-task validation runs `complete_test` + `lint_all` + `typing`
in that order, in the foreground, plus
`uv run --only-group=nox nox -s docs` for docs changes. Coverage
reporting and the docs link check are CI's responsibility, not the
in-iteration gate.

<!-- stoker-onboarded-from: github.com/lsst-sqre/rubin-stoker//profile@main
     prompt-hash: 348ec538f8f7f6fa42da3569d855eab629174668ef28ea225f8b37511daac9d4
     onboarded-at: 2026-11-23T16:40:01Z -->
