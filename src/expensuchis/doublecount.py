"""A beancount plugin that proves a card settlement never creates an expense.

Registered in ``main.beancount`` as ``plugin "expensuchis.doublecount"``, this runs
inside ``bean-check`` itself: the "provably free of double counting" acceptance
criterion (see ``odd/tasks/family-ledger.md``) is otherwise only checked by hand, on
the synthetic sample, for transactions carrying a test-only ``#card-settlement`` tag.
Real imported data carries no such tag, so the invariant needs a general check that
runs on every ``bean-check``, not a test fixture.

**The invariant, and the sign convention it relies on** (pinned in
``odd/tasks/family-ledger.md``, Decisions: "a card purchase credits the liability
negative; the statement payment debits the liability positive back to zero"): a
posting to any ``Liabilities:*`` account with a *positive* amount is, under this
model, always a settlement (a payment reducing the debt) and never a purchase. A
settlement is cash leaving an ``Assets:*`` account to cancel the card debt; it must
never also carry an ``Expenses:*`` posting in the same transaction, or the same
spending would be counted once as the original purchase and again as if the payment
itself were new spending.

So: for every transaction, a ``Liabilities:*`` posting with ``units.number > 0``
forbids any ``Expenses:*`` posting elsewhere in that same transaction. Checked
generically against the ``Liabilities:`` prefix, not ``:Visa``/``:Mastercard``
specifically, so a future card type is covered for free.

**Known limitation, not solved here.** This treats *every* debt-reducing
``Liabilities:*`` posting as a settlement. A card purchase refund or credit note
would also reduce the liability, and would legitimately pair with a negative
``Expenses:*`` posting (reversing the original spend) -- this plugin cannot yet
tell that apart from a real settlement, because no refund importer or model
decision exists yet. Revisit this plugin when one is built.

The error message never echoes a transaction's narration or payee -- only its date
and the offending account -- consistent with this project's rule that real
statement content must never leak into a diagnostic string.
"""

from __future__ import annotations

import collections
import datetime as dt

from beancount.core import data

__all__ = ["DoublecountError", "doublecount"]

#: Beancount's plugin loader looks for this exact attribute; a module without it
#: is silently skipped (no error, no warning), so its absence would be exactly
#: the kind of quiet failure this project refuses to allow elsewhere.
__plugins__ = ("doublecount",)

DoublecountError = collections.namedtuple("DoublecountError", "source message entry")

_LIABILITIES_PREFIX = "Liabilities:"
_EXPENSES_PREFIX = "Expenses:"


def _is_settlement_posting(posting: data.Posting) -> bool:
    return posting.account.startswith(_LIABILITIES_PREFIX) and posting.units.number > 0


def _has_expense_posting(entry: data.Transaction) -> bool:
    return any(posting.account.startswith(_EXPENSES_PREFIX) for posting in entry.postings)


def _violation_message(entry_date: dt.date, account: str) -> str:
    return (
        f"{entry_date}: settlement posting to {account} coexists with an "
        f"Expenses:* posting in the same transaction"
    )


def doublecount(
    entries: data.Directives, options_map: object
) -> tuple[data.Directives, list[DoublecountError]]:
    """Flag any transaction where a card settlement also posts to Expenses:*.

    Entries are returned unchanged; this plugin only validates, it never rewrites
    the ledger.
    """
    errors: list[DoublecountError] = []
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        if not _has_expense_posting(entry):
            continue
        for posting in entry.postings:
            if _is_settlement_posting(posting):
                errors.append(
                    DoublecountError(
                        entry.meta,
                        _violation_message(entry.date, posting.account),
                        entry,
                    )
                )
    return entries, errors
