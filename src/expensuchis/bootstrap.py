"""Create the structural skeleton of a ledger, never the household's account names.

``bootstrap`` writes only what the model needs structurally:

- ``main.beancount`` — the options and the two ``include`` lines ``bean-check`` runs on.
- ``accounts.beancount`` — ``open`` directives for the equity accounts and the technical
  transfer-clearing account only.

It deliberately does **not** generate person, entity or card accounts. Those name the
household's real accounts, which are personal data the tool cannot derive; they are
named by hand in the live ledger. The generated ``accounts.beancount`` states that rule
in a comment so the boundary travels with the data.

It also refuses to overwrite an existing file: re-running ``bootstrap`` on a live ledger
must never clobber the user's accounts.

**Why a placeholder transaction file, and a finding worth recording.** beancount treats
an ``include`` glob that matches no file as an error:

    <load>:0: File glob "transactions/*.beancount" does not match any files

so a freshly bootstrapped ledger — empty ``transactions/`` directory and all — would fail
``bean-check`` and, worse, deadlock the append gate: ``append`` refuses to write onto a
dirty ledger, and the first append is the only thing that could create the first
transaction file. ``bootstrap`` therefore seeds ``transactions/placeholder.beancount``
with a comment when no ``*.beancount`` file exists yet, so the ledger is clean from the
first command.
"""

from __future__ import annotations

from pathlib import Path

from .paths import LedgerPaths

__all__ = ["ACCOUNTS_CONTENT", "BOOTSTRAP_EXISTS", "MAIN_CONTENT", "BootstrapError", "bootstrap"]

BOOTSTRAP_EXISTS = "bootstrap-exists"
BOOTSTRAP_FAILED = "bootstrap-failed"

PLACEHOLDER_NAME = "placeholder.beancount"

MAIN_CONTENT = """\
;; Expensuchis ledger entry point. bean-check runs on this file.
;; The account tree is deliberately not generated here: it names the household's
;; own accounts, which are personal data. Open your accounts by hand in
;; accounts.beancount before importing.
option "title" "Expensuchis household ledger"
option "operating_currency" "ARS"
;; T-N+2: proves a card settlement never also creates an expense. Runs on every
;; bean-check; see src/expensuchis/doublecount.py for the invariant it checks.
plugin "expensuchis.doublecount"
include "accounts.beancount"
include "transactions/*.beancount"
"""

ACCOUNTS_CONTENT = """\
;; Expensuchis starter accounts.
;;
;; This file is deliberately NOT the household's account tree. Person, entity and
;; card accounts name the household's real accounts and are personal data, so the
;; tool never invents them; they are named by hand in the live ledger. Only the
;; instrument accounts the accounting model needs structurally are opened here:
;; the equity account that absorbs opening balances, and the technical transfer
;; clearing account that must always return to zero.

2000-01-01 open Equity:Opening-Balances
2000-01-01 open Assets:TransferenciaEnTransito
"""

PLACEHOLDER_CONTENT = """\
;; This file exists so the `include "transactions/*.beancount"` glob in
;; main.beancount matches at least one file. Beancount rejects an include glob
;; that matches nothing, so a freshly bootstrapped ledger would not pass
;; bean-check without it. It carries no entries and stays beside the real month
;; files created by `expensuchis append`.
"""


class BootstrapError(RuntimeError):
    """A refused bootstrap, carrying a stable ``reason`` code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def bootstrap(paths: LedgerPaths) -> list[Path]:
    """Create the ledger layout and the skeleton files, refusing to overwrite.

    Returns the paths created, in order.

    Raises:
        BootstrapError: with :data:`BOOTSTRAP_EXISTS` when any target already exists
            (nothing is written then), or :data:`BOOTSTRAP_FAILED` when a write fails
            (every file bootstrap created is removed).
    """
    paths.ensure()
    targets = [paths.main(), paths.accounts()]
    transactions = paths.transactions_dir()
    if not list(transactions.glob("*.beancount")):
        targets.append(transactions / PLACEHOLDER_NAME)
    if any(target.exists() for target in targets):
        raise BootstrapError(
            BOOTSTRAP_EXISTS,
            f"{paths.main()} or {paths.accounts()} already exists; refusing to overwrite "
            f"a live ledger.",
        )

    contents = [MAIN_CONTENT, ACCOUNTS_CONTENT, PLACEHOLDER_CONTENT]
    written: list[Path] = []
    try:
        for target, content in zip(targets, contents):
            target.write_text(content, encoding="utf-8")
            written.append(target)
    except OSError as exc:
        for target in written:
            target.unlink(missing_ok=True)
        raise BootstrapError(
            BOOTSTRAP_FAILED,
            f"bootstrap failed while writing ({exc}); every file it created was removed.",
        ) from exc
    return written
