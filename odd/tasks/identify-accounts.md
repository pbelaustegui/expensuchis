# Feature: identify-accounts

ODD tracker. Engram mirror: `odd/identify-accounts/tasks`.
Branch: `feat/identify-accounts`. Started: 2026-09-30.

## Objective

When a new statement arrives, `expensuchis identify --accounts <file>` reports which
importer claims it, which accounts the statement would post to, and which of those are not
yet opened in the ledger, with a ready-to-paste `open` line for each.

## Problem / Why

`identify` prints only `path<TAB>importer`. To learn which accounts a statement touches,
the user has to `extract`, stage a batch, and run a `bean-check` scratch-file trick
(docs/import-workflow.md, "Opening accounts"). The tool must keep refusing to open
accounts on its own; it only reports.

## Scope (authorized)

- New pure-ish seam `pipeline.diagnose` (identify + in-memory extract + compare against
  `open` directives loaded from `main.beancount`). No writes to `staging/` or the ledger.
- CLI flag `identify --accounts`; plain `identify` output unchanged.
- Docs update in `docs/import-workflow.md`.
- Out of scope: opening accounts automatically, unknown-statement diagnostics, folder
  inbox, person/counterparty automation.

## Constraints

- Private data stays out of output beyond what `extract`'s report already shows: account
  names only, never counterparty names or statement basenames' holder names.
- Reuse `IMPORTER_RAISED` handling semantics from `pipeline.extract`.
- Missing `main.beancount` refuses with `MAIN_MISSING`.
- Advisory ~400 authored changed lines; forecast well under it.

## TDD

Mode: enabled. Source: session config (Strict TDD Mode). Runner: `pytest` (`uv run pytest`
or `pytest`, see pyproject). Lint: `ruff check` and `ruff format --check`, line length 100.

## Tasks

- [x] T1 `pipeline.diagnose` + `Diagnosis` dataclass, tests first. Route: delegated writer. Commit 98b3bdc.
- [x] T2 CLI `identify --accounts` output and tests in `tests/test_cli.py`. Route: delegated writer (same writer as T1). Commit 106a974.
- [x] T3 Docs in `docs/import-workflow.md`. Route: delegated writer (same writer). Commit bb59580.

## Acceptance criteria

- Known statement with all accounts open: importer name, accounts listed, "missing: none".
- Known statement with unopened accounts: those are listed with `open` lines.
- Unknown/ambiguous statement: existing `NO_IMPORTER` / `AMBIGUOUS_IMPORTER` refusals.
- Nothing is written under the ledger directory (asserted in tests).
- `pytest`, `ruff check`, `ruff format --check` pass.

## Delivery

Strategy: ask-on-risk. Forecast ~200 authored lines: single PR.

## Progress / Evidence

- T1 RED: `AttributeError: module 'expensuchis.pipeline' has no attribute 'diagnose'` (7 failed). GREEN after implementation.
- T2 RED: `unrecognized arguments: --accounts` (4 failed, 2 passed). GREEN after implementation.
- Parent spot check (after all commits): `uv run pytest -q`: 1027 passed, 7 skipped; `uv run ruff check .`: all checks passed; `uv run ruff format --check .`: 72 files already formatted.
- Size: 277 insertions, 19 deletions across 5 files (under the ~400 heuristic). Single PR.
- Decisions: `Diagnosis.first_dates` gives suggested `open` lines the earliest entry date per missing account instead of a placeholder; shared `_run_importer` helper keeps `extract` behavior unchanged; CLI uses default `get_importers()` (no `reveal`).
- Native review: assessment was `unassessable` while this file was untracked; treated as high/due, review preflight run after committing this file.

## Next step

Native review preflight on the committed range vs main, then open a PR (user decision).
