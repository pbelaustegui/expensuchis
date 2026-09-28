"""The monthly Expenses-in-USD-at-date summary (T-N+1, first deliverable).

**Deflation is a view, never a stored value** (see ``odd/tasks/family-ledger.md``,
Decisions): this module reads the ledger and the cached FX series and computes a
USD figure at read time. Nothing here writes to the ledger.

Two shapes for an ``Expenses:*`` posting, both settled in ``docs/accounting-model.md``:

* **Already USD.** A posting whose ``units.currency`` is ``USD`` already carries the
  real dollar amount, whether or not it also carries an ``@`` price. That price is
  the *administrative* rate the statement used to book the ARS liability leg (an
  Argentine card's USD purchase composes tax and perception components that a
  market rate does not reproduce); it is never reapplied here.
* **ARS (or any other non-USD currency).** Converted through the historical FX
  series (:mod:`expensuchis.fx`) at the transaction's own date, using the *sell*
  ("venta") side of the quote: venta is what it would have cost to acquire that
  many US dollars on that date, which is the plain reading of "this ARS expense
  equals N hard-currency dollars."

A date the cached series cannot answer for (no cache at all, or no quote within
:data:`expensuchis.fx.MAX_STALENESS_DAYS`) refuses the whole summary rather than
silently dropping or estimating one posting -- the same fail-closed discipline as
the reconciliation checks elsewhere in this project. The same discipline covers
the ledger load itself: a beancount error on ``main.beancount`` -- an unbalanced
transaction, an unopened account, or a registered plugin such as
:mod:`expensuchis.doublecount` flagging its own invariant -- refuses the summary
too, rather than totalling postings from data ``bean-check`` would itself refuse.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from decimal import Decimal

from beancount import loader
from beancount.core import data

from . import fx
from .paths import LedgerPaths

__all__ = [
    "FX_CACHE_MISSING",
    "FX_RATE_NOT_FOUND",
    "LEDGER_INVALID",
    "MAIN_MISSING",
    "MONTH_INVALID",
    "RANGE_INVALID",
    "MonthlySummary",
    "SummaryError",
    "summarize",
    "summarize_range",
]

#: Stable, greppable refusal reason codes. Never renumber or rename silently.
MAIN_MISSING = "main-missing"
MONTH_INVALID = "month-invalid"
RANGE_INVALID = "range-invalid"
LEDGER_INVALID = "ledger-invalid"
FX_CACHE_MISSING = "fx-cache-missing"
FX_RATE_NOT_FOUND = "fx-rate-not-found"

_EXPENSES_PREFIX = "Expenses:"
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


class SummaryError(RuntimeError):
    """A refused summary, carrying a stable ``reason`` code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclasses.dataclass(frozen=True)
class MonthlySummary:
    """One month's ``Expenses:*`` totals in USD, by full account name."""

    month: str
    by_account: dict[str, Decimal]
    total: Decimal


def _parse_month(month: str) -> tuple[int, int]:
    match = _MONTH_RE.match(month)
    if match is None:
        raise SummaryError(MONTH_INVALID, f"{month!r} is not a valid YYYY-MM month.")
    year, month_number = int(match.group(1)), int(match.group(2))
    if not 1 <= month_number <= 12:
        raise SummaryError(MONTH_INVALID, f"{month!r} is not a valid YYYY-MM month.")
    return year, month_number


def _load_entries(paths: LedgerPaths) -> list:
    main = paths.main()
    if not main.is_file():
        raise SummaryError(MAIN_MISSING, f"{main} does not exist.")
    entries, errors, _options = loader.load_file(main)
    if errors:
        raise SummaryError(
            LEDGER_INVALID,
            f"the ledger failed {len(errors)} beancount check(s) (including any plugin, "
            f"such as the double-counting guard); run bean-check for details before "
            f"trusting a summary computed over it.",
        )
    return list(entries)


def _usd_amount(
    posting: data.Posting, series: fx.Series, cache: fx.FxCache, when: dt.date
) -> Decimal:
    if posting.units.currency == "USD":
        return posting.units.number

    try:
        cached = cache.load(series)
    except fx.CacheError as exc:
        raise SummaryError(FX_CACHE_MISSING, str(exc)) from exc

    try:
        lookup = cached.rate_at(when)
    except fx.RateNotFoundError as exc:
        raise SummaryError(FX_RATE_NOT_FOUND, str(exc)) from exc

    return posting.units.number / lookup.used.sell


def _month_total(
    entries: list, year: int, month_number: int, series: fx.Series, cache: fx.FxCache
) -> dict[str, Decimal]:
    by_account: dict[str, Decimal] = {}
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        if entry.date.year != year or entry.date.month != month_number:
            continue
        for posting in entry.postings:
            if not posting.account.startswith(_EXPENSES_PREFIX):
                continue
            usd = _usd_amount(posting, series, cache, entry.date)
            by_account[posting.account] = by_account.get(posting.account, Decimal(0)) + usd
    return by_account


def _month_key(year: int, month_number: int) -> str:
    return f"{year:04d}-{month_number:02d}"


def _next_month(year: int, month_number: int) -> tuple[int, int]:
    return (year + 1, 1) if month_number == 12 else (year, month_number + 1)


def summarize(paths: LedgerPaths, month: str, series: fx.Series) -> MonthlySummary:
    """Return the ``month`` (``YYYY-MM``) total of every ``Expenses:*`` account, in USD.

    Raises:
        SummaryError: ``MONTH_INVALID`` for a malformed month, ``MAIN_MISSING`` when
            the ledger was never bootstrapped, ``LEDGER_INVALID`` when the ledger
            fails a beancount check (including any registered plugin, such as the
            double-counting guard) -- a summary is never computed over data
            beancount itself would refuse, ``FX_CACHE_MISSING`` when ``series``
            has no local cache yet, or ``FX_RATE_NOT_FOUND`` when a non-USD posting's
            date falls outside the cached series' staleness bound.
    """
    year, month_number = _parse_month(month)
    entries = _load_entries(paths)
    cache = fx.FxCache(paths)
    by_account = _month_total(entries, year, month_number, series, cache)
    total = sum(by_account.values(), Decimal(0))
    return MonthlySummary(month=month, by_account=dict(sorted(by_account.items())), total=total)


def summarize_range(
    paths: LedgerPaths, from_month: str, to_month: str, series: fx.Series
) -> list[MonthlySummary]:
    """Return one :class:`MonthlySummary` per month from ``from_month`` to ``to_month``.

    Both boundaries are ``YYYY-MM`` and inclusive. The ledger is loaded once for
    the whole range, not once per month.

    Raises:
        SummaryError: ``MONTH_INVALID`` for a malformed boundary, ``RANGE_INVALID``
            when ``from_month`` is after ``to_month``, or any reason ``summarize``
            itself raises (``MAIN_MISSING``, ``LEDGER_INVALID``,
            ``FX_CACHE_MISSING``, ``FX_RATE_NOT_FOUND``).
    """
    from_year, from_month_number = _parse_month(from_month)
    to_year, to_month_number = _parse_month(to_month)
    if (from_year, from_month_number) > (to_year, to_month_number):
        raise SummaryError(
            RANGE_INVALID, f"{from_month!r} is after {to_month!r}; --from must not be after --to."
        )

    entries = _load_entries(paths)
    cache = fx.FxCache(paths)

    results = []
    year, month_number = from_year, from_month_number
    while (year, month_number) <= (to_year, to_month_number):
        by_account = _month_total(entries, year, month_number, series, cache)
        total = sum(by_account.values(), Decimal(0))
        results.append(
            MonthlySummary(
                month=_month_key(year, month_number),
                by_account=dict(sorted(by_account.items())),
                total=total,
            )
        )
        year, month_number = _next_month(year, month_number)
    return results
