"""Tests for the monthly Expenses-in-USD-at-date summary (T-N+1, first deliverable).

No test touches the network: every FX series is populated locally through an
injected :class:`~expensuchis.fx.FxSource` fake, mirroring ``tests/test_fx.py``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from expensuchis import summary
from expensuchis.fx import FxCache, Quote, Series
from expensuchis.ledger import ENV_VAR
from expensuchis.paths import LedgerPaths
from expensuchis.summary import MonthlySummary, SummaryError

ACCOUNTS = (
    "2020-01-01 open Equity:Opening-Balances\n"
    "2020-01-01 open Assets:TransferenciaEnTransito\n"
    "2020-01-01 open Assets:Test:Caja\n"
    "2020-01-01 open Liabilities:Test:Visa\n"
    "2020-01-01 open Expenses:Compras\n"
    "2020-01-01 open Expenses:ComidaFuera\n"
)


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LedgerPaths:
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))
    paths = LedgerPaths()
    paths.ensure()
    paths.main().write_text('include "accounts.beancount"\n', encoding="utf-8")
    paths.accounts().write_text(ACCOUNTS, encoding="utf-8")
    return paths


class _FakeSource:
    source_id = "fake"

    def __init__(self, quotes: dict[Series, list[Quote]]) -> None:
        self._quotes = quotes

    def url_for(self, series: Series) -> str:
        return f"fake://{series.value}"

    def fetch(self, series: Series) -> list[Quote]:
        return self._quotes[series]


def _seed_fx(paths: LedgerPaths, series: Series, quotes: list[Quote]) -> None:
    FxCache(paths, source=_FakeSource({series: quotes})).refresh(series)


def _write_month(paths: LedgerPaths, month: str, body: str) -> None:
    path = paths.transaction_file(dt.date.fromisoformat(f"{month}-01"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    include_line = f'include "transactions/{month}.beancount"\n'
    main = paths.main()
    if include_line not in main.read_text(encoding="utf-8"):
        main.write_text(main.read_text(encoding="utf-8") + include_line, encoding="utf-8")


def test_pure_ars_month_converts_at_the_sell_rate_for_each_date(ledger: LedgerPaths) -> None:
    _seed_fx(
        ledger,
        Series.MEP,
        [
            Quote(dt.date(2026, 3, 1), Decimal("1000.00"), Decimal("1010.00")),
            Quote(dt.date(2026, 3, 15), Decimal("1020.00"), Decimal("1030.00")),
        ],
    )
    _write_month(
        ledger,
        "2026-03",
        '2026-03-01 * "Store" "Groceries"\n'
        '  key: "k1"\n'
        "  Expenses:Compras       10100.00 ARS\n"
        "  Assets:Test:Caja      -10100.00 ARS\n"
        "\n"
        '2026-03-15 * "Diner" "Lunch"\n'
        '  key: "k2"\n'
        "  Expenses:ComidaFuera    5150.00 ARS\n"
        "  Assets:Test:Caja       -5150.00 ARS\n",
    )

    result = summary.summarize(ledger, "2026-03", Series.MEP)

    assert result.by_account["Expenses:Compras"] == Decimal(10)
    assert result.by_account["Expenses:ComidaFuera"] == Decimal(5)
    assert result.total == Decimal(15)


def test_usd_posting_with_at_price_uses_the_raw_usd_amount(ledger: LedgerPaths) -> None:
    _seed_fx(ledger, Series.MEP, [Quote(dt.date(2026, 3, 1), Decimal(1), Decimal(1))])
    _write_month(
        ledger,
        "2026-03",
        '2026-03-02 * "Shop" "USD purchase on ARS card"\n'
        '  key: "k1"\n'
        "  Expenses:Compras       15.00 USD @ 1580.00 ARS\n"
        "  Liabilities:Test:Visa  -23700.00 ARS\n",
    )

    result = summary.summarize(ledger, "2026-03", Series.MEP)

    assert result.by_account["Expenses:Compras"] == Decimal("15.00")
    assert result.total == Decimal("15.00")


def test_bare_usd_posting_with_no_price(ledger: LedgerPaths) -> None:
    _seed_fx(ledger, Series.MEP, [Quote(dt.date(2026, 3, 1), Decimal(1), Decimal(1))])
    _write_month(
        ledger,
        "2026-03",
        '2026-03-03 * "Shop" "USD purchase on USD card"\n'
        '  key: "k1"\n'
        "  Expenses:Compras       20.00 USD\n"
        "  Liabilities:Test:Visa  -20.00 USD\n",
    )

    result = summary.summarize(ledger, "2026-03", Series.MEP)

    assert result.by_account["Expenses:Compras"] == Decimal("20.00")


def test_multiple_accounts_sum_independently_and_the_total_is_their_sum(
    ledger: LedgerPaths,
) -> None:
    _seed_fx(ledger, Series.MEP, [Quote(dt.date(2026, 3, 1), Decimal(1000), Decimal(1000))])
    _write_month(
        ledger,
        "2026-03",
        '2026-03-01 * "A" "a"\n'
        '  key: "k1"\n'
        "  Expenses:Compras      1000.00 ARS\n"
        "  Assets:Test:Caja     -1000.00 ARS\n"
        "\n"
        '2026-03-01 * "B" "b"\n'
        '  key: "k2"\n'
        "  Expenses:ComidaFuera  2000.00 ARS\n"
        "  Assets:Test:Caja     -2000.00 ARS\n",
    )

    result = summary.summarize(ledger, "2026-03", Series.MEP)

    assert result.by_account == {
        "Expenses:ComidaFuera": Decimal(2),
        "Expenses:Compras": Decimal(1),
    }
    assert result.total == Decimal(3)


def test_a_month_with_no_expenses_postings_is_empty_not_an_error(ledger: LedgerPaths) -> None:
    _seed_fx(ledger, Series.MEP, [Quote(dt.date(2026, 3, 1), Decimal(1000), Decimal(1000))])

    result = summary.summarize(ledger, "2026-03", Series.MEP)

    assert result == MonthlySummary(month="2026-03", by_account={}, total=Decimal(0))


def test_a_date_beyond_the_staleness_bound_raises_summary_error_not_a_raw_fx_error(
    ledger: LedgerPaths,
) -> None:
    _seed_fx(ledger, Series.MEP, [Quote(dt.date(2026, 1, 1), Decimal(1000), Decimal(1000))])
    _write_month(
        ledger,
        "2026-03",
        '2026-03-01 * "A" "a"\n'
        '  key: "k1"\n'
        "  Expenses:Compras      1000.00 ARS\n"
        "  Assets:Test:Caja     -1000.00 ARS\n",
    )

    with pytest.raises(SummaryError) as excinfo:
        summary.summarize(ledger, "2026-03", Series.MEP)
    assert excinfo.value.reason == summary.FX_RATE_NOT_FOUND


def test_no_cached_series_at_all_raises_summary_error_not_a_raw_cache_error(
    ledger: LedgerPaths,
) -> None:
    _write_month(
        ledger,
        "2026-03",
        '2026-03-01 * "A" "a"\n'
        '  key: "k1"\n'
        "  Expenses:Compras      1000.00 ARS\n"
        "  Assets:Test:Caja     -1000.00 ARS\n",
    )

    with pytest.raises(SummaryError) as excinfo:
        summary.summarize(ledger, "2026-03", Series.MEP)
    assert excinfo.value.reason == summary.FX_CACHE_MISSING


def test_summarize_refuses_a_missing_main_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))
    paths = LedgerPaths()

    with pytest.raises(SummaryError) as excinfo:
        summary.summarize(paths, "2026-03", Series.MEP)
    assert excinfo.value.reason == summary.MAIN_MISSING


def test_summarize_refuses_a_malformed_month(ledger: LedgerPaths) -> None:
    with pytest.raises(SummaryError) as excinfo:
        summary.summarize(ledger, "not-a-month", Series.MEP)
    assert excinfo.value.reason == summary.MONTH_INVALID


def test_a_beancount_load_error_refuses_the_summary_instead_of_totalling_anyway(
    ledger: LedgerPaths,
) -> None:
    """A plain loader error (an unopened account) must not be silently discarded.

    Regression test for a review finding (R2-001/R3/R4-001 on
    ``review-3c4bb26211009475``): ``_load_entries`` used to unpack and drop
    beancount's own errors list, so ``summarize()`` would total postings from a
    ledger that would itself fail ``bean-check``.
    """
    _seed_fx(ledger, Series.MEP, [Quote(dt.date(2026, 3, 1), Decimal(1000), Decimal(1000))])
    _write_month(
        ledger,
        "2026-03",
        '2026-03-01 * "A" "a"\n'
        '  key: "k1"\n'
        "  Expenses:NuncaAbierta  1000.00 ARS\n"
        "  Assets:Test:Caja      -1000.00 ARS\n",
    )

    with pytest.raises(SummaryError) as excinfo:
        summary.summarize(ledger, "2026-03", Series.MEP)
    assert excinfo.value.reason == summary.LEDGER_INVALID


def test_a_doublecount_plugin_violation_refuses_the_summary_too(ledger: LedgerPaths) -> None:
    """The invariant this same project's own plugin enforces must not be bypassable.

    If ``main.beancount`` registers ``expensuchis.doublecount`` (as
    ``bootstrap.py`` now does for every new ledger) and a settlement transaction
    violates it, ``summarize()`` must refuse rather than total the double-counted
    expense into a normal-looking figure.
    """
    main = ledger.main()
    main.write_text(
        'plugin "expensuchis.doublecount"\n' + main.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _seed_fx(ledger, Series.MEP, [Quote(dt.date(2026, 3, 1), Decimal(1000), Decimal(1000))])
    _write_month(
        ledger,
        "2026-03",
        '2026-03-01 * "BBVA" "Bad settlement"\n'
        '  key: "k1"\n'
        "  Liabilities:Test:Visa  1000.00 ARS\n"
        "  Expenses:Compras       1000.00 ARS\n"
        "  Assets:Test:Caja      -2000.00 ARS\n",
    )

    with pytest.raises(SummaryError) as excinfo:
        summary.summarize(ledger, "2026-03", Series.MEP)
    assert excinfo.value.reason == summary.LEDGER_INVALID


def test_summarize_range_returns_one_summary_per_month_in_order(ledger: LedgerPaths) -> None:
    _seed_fx(
        ledger,
        Series.MEP,
        [
            Quote(dt.date(2026, 1, 1), Decimal(1000), Decimal(1000)),
            Quote(dt.date(2026, 3, 1), Decimal(1000), Decimal(1000)),
        ],
    )
    _write_month(
        ledger,
        "2026-01",
        '2026-01-01 * "A" "a"\n'
        '  key: "k1"\n'
        "  Expenses:Compras      1000.00 ARS\n"
        "  Assets:Test:Caja     -1000.00 ARS\n",
    )
    _write_month(
        ledger,
        "2026-03",
        '2026-03-01 * "B" "b"\n'
        '  key: "k2"\n'
        "  Expenses:Compras      3000.00 ARS\n"
        "  Assets:Test:Caja     -3000.00 ARS\n",
    )

    results = summary.summarize_range(ledger, "2026-01", "2026-03", Series.MEP)

    assert [r.month for r in results] == ["2026-01", "2026-02", "2026-03"]
    assert results[0].total == Decimal(1)
    assert results[1] == MonthlySummary(month="2026-02", by_account={}, total=Decimal(0))
    assert results[2].total == Decimal(3)


def test_summarize_range_of_a_single_month_matches_summarize(ledger: LedgerPaths) -> None:
    _seed_fx(ledger, Series.MEP, [Quote(dt.date(2026, 3, 1), Decimal(1000), Decimal(1000))])
    _write_month(
        ledger,
        "2026-03",
        '2026-03-01 * "A" "a"\n'
        '  key: "k1"\n'
        "  Expenses:Compras      1000.00 ARS\n"
        "  Assets:Test:Caja     -1000.00 ARS\n",
    )

    results = summary.summarize_range(ledger, "2026-03", "2026-03", Series.MEP)

    assert results == [summary.summarize(ledger, "2026-03", Series.MEP)]


def test_summarize_range_refuses_when_from_is_after_to(ledger: LedgerPaths) -> None:
    with pytest.raises(SummaryError) as excinfo:
        summary.summarize_range(ledger, "2026-03", "2026-01", Series.MEP)
    assert excinfo.value.reason == summary.RANGE_INVALID


def test_summarize_range_refuses_a_malformed_boundary(ledger: LedgerPaths) -> None:
    with pytest.raises(SummaryError) as excinfo:
        summary.summarize_range(ledger, "not-a-month", "2026-03", Series.MEP)
    assert excinfo.value.reason == summary.MONTH_INVALID


def test_summarize_range_propagates_a_beancount_load_error(ledger: LedgerPaths) -> None:
    _seed_fx(ledger, Series.MEP, [Quote(dt.date(2026, 3, 1), Decimal(1000), Decimal(1000))])
    _write_month(
        ledger,
        "2026-03",
        '2026-03-01 * "A" "a"\n'
        '  key: "k1"\n'
        "  Expenses:NuncaAbierta  1000.00 ARS\n"
        "  Assets:Test:Caja      -1000.00 ARS\n",
    )

    with pytest.raises(SummaryError) as excinfo:
        summary.summarize_range(ledger, "2026-03", "2026-03", Series.MEP)
    assert excinfo.value.reason == summary.LEDGER_INVALID
