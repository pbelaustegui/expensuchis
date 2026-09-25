# expensuchis

A beancount-based family ledger with a custom ingest pipeline.

The household's money lives across several Argentine issuers — credit cards, bank
accounts and fintechs. This project consolidates their statements into a single
plain-text [beancount](https://beancount.github.io/) ledger so that each amount keeps
its meaning over time, without double counting the same purchase when it appears as a
card purchase and again as the bank settlement that pays the card.

Ingest is file-driven: one importer per source, over the `beangulp` API, with a
mandatory human review step before anything is appended to the ledger.

## Why the ledger lives outside this repository

`expensuchis` is **public on GitHub**. It contains code and synthetic fixtures only.
The real ledger and every real statement live outside this tree, in a directory with
no remote, and never enter it. Two guards enforce that:

1. **Ledger resolution guard** (`src/expensuchis/ledger.py`). The ledger directory is
   read from the `EXPENSUCHIS_LEDGER_DIR` environment variable, and the resolved path
   must exist, be a directory, and fall outside every repository this code can
   discover. Discovery walks up from this module's own directory and from the current
   working directory, looking for the nearest `.git` or `pyproject.toml`; `.git` wins
   when the two sit at different levels.
2. **Privacy guard test** (`src/expensuchis/privacy.py`, exercised by
   `tests/test_privacy_guards.py`). It walks the working tree — untracked files
   included — and fails if any data-bearing file
   (`.pdf .xls .xlsx .csv .ofx .qif .beancount .bean .ledger`) appears outside the
   allowlisted `tests/fixtures/**` and `sample/**` directories. An untracked statement
   is one `git add .` away from being published, so tracked-only scanning is not enough.

Guard #1's protection depends on being able to *find* the repository, so it has one
boundary worth naming. Discovery needs at least one start point inside the checkout. That
always holds for the editable install this project uses (`uv run` from the checkout, from
any working directory), and it holds for a wheel install invoked from inside the checkout.
It does **not** hold for a non-editable install invoked from an unrelated directory: in
that combination neither start point reveals the repository, and a repo-internal ledger
would be accepted. That combination has no trigger in this project's usage. The guard is
deliberately not tightened further, because the stricter rule — refusing any ledger inside
*any* git worktree — would block legitimate setups such as versioning the ledger in a
private repository of its own.

Guard #2 is a **name-based tripwire, not a wall**, and the README will not pretend
otherwise. A statement renamed to `.txt`/`.json`/`.dat`, a hardlink with a benign
name, or a file inside an excluded directory (`.venv/`, `__pycache__/`, …) all pass it.
The structural mitigations are what actually hold: the ledger and every statement
never live inside this tree (guard #1), and this check is enforced in CI on every
push and pull request, so a bad file cannot reach the default branch by being merged.

`sample/smoke.beancount` is a tiny synthetic ledger used only by the smoke test; it
is not the account model. The account model itself lives in
`sample/family-model.beancount`, the executable proof validated by `bean-check` and by
`tests/test_family_model.py`, which asserts its load-bearing properties (the card
liability returns to exactly zero after settlement, and each purchase is counted as an
expense exactly once).

## Usage

The environment is managed by [`uv`](https://docs.astral.sh/uv/). The **system**
Python 3.12 is PEP 668 `EXTERNALLY-MANAGED`, so `pip` and `venv` are intentionally
not used against the system interpreter; `uv sync` creates the project's own
`.venv/`, which is what every `uv run` command uses.

```bash
export EXPENSUCHIS_LEDGER_DIR="$HOME/expensuchis-ledger"   # outside this repo

uv sync                                                    # create .venv from uv.lock
uv run bean-check sample/smoke.beancount                   # validate the synthetic sample
uv run pytest -q                                           # tests, including both guards
uv run ruff check .                                        # lint
```

To validate the real ledger, point `bean-check` at it through the environment variable
without ever copying it into this repository.

## Local pre-commit hook (opt-in)

CI is the enforcement of record, but you can also catch a stray data file before it is
committed. The hook lives at `.githooks/pre-commit` and is **not activated by default**;
turn it on yourself from the repository root:

```bash
git config core.hooksPath .githooks
```

The hook runs the same scan and refuses the commit when it finds offenders. It does not
need an activated virtualenv: it uses `uv` when available and falls back to
`PYTHONPATH=src python3 -m expensuchis.privacy`. You can bypass it with
`git commit --no-verify`, but CI cannot be bypassed that way, so a bypassed offender
still fails on push/PR.
