"""Executable proof of the accounting model in ``docs/accounting-model.md``.

These tests load the synthetic sample ledger through beancount's Python API and
assert on the parsed entries, postings and balances. One targeted check
(``test_no_market_rate_token_appears_in_sample_text``) is the single exception:
it scans the raw sample text for the forbidden market-rate tokens before the
parsed ``Price``/``Commodity`` checks run, because a token can enter through a
comment or a directive the parsed structures do not normalise away. The
load-bearing checks are the two the document calls out as silent-corruption
risks: the card liability returning to exactly zero after settlement, and every
purchase being counted as an expense exactly once.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader
from beancount.core import data
from beancount.core.amount import Amount

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "sample"
SAMPLE_LEDGER = SAMPLE_DIR / "family-model.beancount"

CARD = "Liabilities:BBVA:P1:Visa"
VISA_USD = "Liabilities:BBVA:P1:VisaUSD"
INSTALLMENT_CARD = "Liabilities:Provincia:P2:Visa"
EXPENSES_PREFIX = "Expenses:"
LIABILITIES_PREFIX = "Liabilities:"
EQUITY_OPENING = "Equity:Opening-Balances"

# Pinned on purpose. Any change to the sample must be a deliberate change to this
# expectation too: the brittleness is the defence against a silently duplicated
# expense, which ``bean-check`` accepts when it balances.
EXPECTED_EXPENSES_BY_CURRENCY = {"ARS": Decimal("773500.00"), "USD": Decimal("45.00")}


def _load() -> tuple[list[data.Transaction], list[tuple[str, int, str]]]:
    entries, errors, _ = loader.load_file(str(SAMPLE_LEDGER))
    transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
    return transactions, errors


def _tagged(transactions: list[data.Transaction], tag: str) -> list[data.Transaction]:
    return [entry for entry in transactions if tag in entry.tags]


def _balance(transactions: list[data.Transaction], account: str) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for entry in transactions:
        for posting in entry.postings:
            if posting.account == account:
                totals[posting.units.currency] += posting.units.number
    return dict(totals)


def _expense_postings(entry: data.Transaction) -> list[data.Posting]:
    return [posting for posting in entry.postings if posting.account.startswith(EXPENSES_PREFIX)]


def _sum_expenses_by_currency(transactions: list[data.Transaction]) -> dict[str, Decimal]:
    """Sum every ``Expenses:*`` posting in the whole sample, grouped by currency."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for entry in transactions:
        for posting in entry.postings:
            if posting.account.startswith(EXPENSES_PREFIX):
                totals[posting.units.currency] += posting.units.number
    return dict(totals)


def _is_zero(balance: dict[str, Decimal]) -> bool:
    return all(value == 0 for value in balance.values())


def test_sample_loads_without_errors() -> None:
    """`load_file` reports undeclared accounts and unbalanced entries in `errors`."""
    transactions, errors = _load()
    assert errors == [], errors
    assert transactions, "sample ledger should contain transactions"


def test_card_purchase_is_counted_as_an_expense_exactly_once() -> None:
    transactions, _ = _load()
    purchases = _tagged(transactions, "card-purchase")
    assert len(purchases) == 1

    expense_postings = _expense_postings(purchases[0])
    assert len(expense_postings) == 1
    assert expense_postings[0].account == "Expenses:Supermercado"
    assert expense_postings[0].units.number == Decimal("45000.00")

    # The whole category totals the purchase once, not twice.
    assert _balance(transactions, "Expenses:Supermercado") == {"ARS": Decimal("45000.00")}

    # The whole sample totals each purchase once, in every currency. A duplicate
    # recorded in a different category leaves the category assertion above green
    # while this one fails.
    assert _sum_expenses_by_currency(transactions) == EXPECTED_EXPENSES_BY_CURRENCY


def test_card_liability_is_exactly_zero_after_settlement() -> None:
    transactions, _ = _load()
    settled = _tagged(transactions, "card-settlement")
    assert settled, "sample must contain at least one statement settlement"
    # Exact equality, not approximate: an inverted sign would leave +90,000 here.
    assert _balance(transactions, CARD) == {"ARS": Decimal("0.00")}


def test_every_liability_except_the_installment_plan_is_zero() -> None:
    """Enumerate liabilities rather than trusting one hardcoded account.

    A non-zero ``Liabilities:Brubank:P1:Visa`` — or any liability added later —
    passes a hardcoded check, so the only account allowed a non-zero balance is
    the installment plan's.
    """
    entries, errors, _ = loader.load_file(str(SAMPLE_LEDGER))
    assert errors == [], errors
    transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
    liability_accounts = sorted(
        entry.account
        for entry in entries
        if isinstance(entry, data.Open) and entry.account.startswith(LIABILITIES_PREFIX)
    )
    assert liability_accounts, "sample must open at least one liability account"

    balances = {account: _balance(transactions, account) for account in liability_accounts}
    outstanding = {
        account: balance
        for account, balance in balances.items()
        if not _is_zero(balance)
    }
    assert outstanding == {INSTALLMENT_CARD: {"ARS": Decimal("-720000.00")}}, outstanding
    # The case-(b) USD card is settled, and the ARS card is settled, so both are zero.
    assert _is_zero(balances[VISA_USD]), balances[VISA_USD]
    assert _is_zero(balances[CARD]), balances[CARD]


def test_settlement_contributes_no_expense_posting() -> None:
    transactions, _ = _load()
    settlements = _tagged(transactions, "card-settlement")
    assert settlements, "sample must contain at least one statement settlement"
    for entry in settlements:
        assert _expense_postings(entry) == [], (
            f"settlement on {entry.date} must not create spending: {entry.narration}"
        )


@pytest.mark.parametrize("tag", ["internal-transfer", "internal-transfer-fx", "transfer-to-p3"])
def test_transfers_contribute_no_expense_posting(tag: str) -> None:
    transactions, _ = _load()
    transfers = _tagged(transactions, tag)
    assert transfers, f"sample must contain a #{tag} transaction"
    for entry in transfers:
        assert _expense_postings(entry) == []


def test_cross_currency_transfer_carries_an_executed_rate() -> None:
    """A real FX operation carries the rate it executed, observed, not looked up."""
    transactions, _ = _load()
    transfers = _tagged(transactions, "internal-transfer-fx")
    assert len(transfers) == 1
    entry = transfers[0]
    assert _expense_postings(entry) == [], "an FX transfer is not an expense"

    usd_legs = [
        posting for posting in entry.postings if posting.account == "Assets:BBVA:P1:CajaUSD"
    ]
    assert len(usd_legs) == 1
    assert usd_legs[0].units.number == Decimal("500.00")
    assert usd_legs[0].units.currency == "USD"
    assert usd_legs[0].price.number == Decimal("1240.00")
    assert usd_legs[0].price.currency == "ARS"

    ars_legs = [posting for posting in entry.postings if posting.account == "Assets:BBVA:P1:Caja"]
    assert len(ars_legs) == 1
    assert ars_legs[0].units.number == Decimal("-620000.00")
    # The executed rate is exactly the ratio of the two legs, not a series value.
    assert usd_legs[0].units.number * usd_legs[0].price.number == -ars_legs[0].units.number


def test_cash_expense_is_in_its_category() -> None:
    transactions, _ = _load()
    cash = _tagged(transactions, "cash")
    assert len(cash) == 1
    expense = _expense_postings(cash[0])
    assert len(expense) == 1
    assert expense[0].account == "Expenses:ComidaFuera:Restaurantes"
    assert expense[0].units.number == Decimal("8500.00")
    assert _balance(transactions, "Expenses:ComidaFuera:Restaurantes") == {
        "ARS": Decimal("8500.00")
    }


def test_opening_balances_post_against_equity_in_the_account_currency() -> None:
    transactions, _ = _load()
    openings = _tagged(transactions, "opening")
    assert len(openings) == 2, "sample must carry one opening balance per funded account"
    for entry in openings:
        equity = [p for p in entry.postings if p.account == EQUITY_OPENING]
        assets = [p for p in entry.postings if p.account.startswith("Assets:")]
        assert len(equity) == 1
        assert len(assets) == 1
        assert assets[0].units.currency == equity[0].units.currency
        assert assets[0].units.number == -equity[0].units.number

    assert _balance(transactions, "Assets:BBVA:P1:CajaUSD")["USD"] == Decimal("1470.00")


def test_income_is_coarse_and_matches_an_asset_posting() -> None:
    transactions, _ = _load()
    income_entries = _tagged(transactions, "income")
    assert len(income_entries) == 2
    for entry in income_entries:
        income = [p for p in entry.postings if p.account.startswith("Income:")]
        assets = [p for p in entry.postings if p.account.startswith("Assets:")]
        assert len(income) == 1
        assert len(assets) == 1
        assert income[0].units.number < 0
        assert assets[0].units.number == -income[0].units.number
        assert income[0].units.currency == assets[0].units.currency == "ARS"

    income_accounts = {
        posting.account
        for entry in income_entries
        for posting in entry.postings
        if posting.account.startswith("Income:")
    }
    assert income_accounts == {"Income:P1:Sueldo", "Income:P2:Sueldo"}


def test_case_b_usd_settlement_brings_visa_usd_to_zero() -> None:
    transactions, _ = _load()
    purchases = _tagged(transactions, "usd-card-usd")
    settlements = _tagged(transactions, "usd-card-settlement")
    assert len(purchases) == 1
    assert len(settlements) == 1

    purchase = [p for p in purchases[0].postings if p.account == VISA_USD]
    settlement = [p for p in settlements[0].postings if p.account == VISA_USD]
    assert len(purchase) == 1
    assert len(settlement) == 1
    assert purchase[0].units.number == Decimal("-15.00")
    assert settlement[0].units.number == Decimal("15.00")
    assert _balance(transactions, VISA_USD) == {"USD": Decimal("0.00")}


def test_no_account_is_opened_with_more_than_one_currency() -> None:
    """The two-account rule is a convention; this assertion is what enforces it."""
    entries, errors, _ = loader.load_file(str(SAMPLE_LEDGER))
    assert errors == [], errors
    openings = [entry for entry in entries if isinstance(entry, data.Open)]
    assert openings, "sample must open accounts"
    multi_currency = [
        (entry.account, entry.currencies)
        for entry in openings
        if len(entry.currencies or []) > 1
    ]
    assert multi_currency == [], f"accounts opened with several currencies: {multi_currency}"


def test_usd_card_rate_is_derived_from_the_two_statement_amounts() -> None:
    transactions, _ = _load()
    purchases = _tagged(transactions, "usd-card")
    assert len(purchases) == 1
    entry = purchases[0]

    priced = [posting for posting in entry.postings if posting.price is not None]
    assert len(priced) == 1
    usd_posting = priced[0]
    assert usd_posting.units.currency == "USD"
    assert usd_posting.units.number == Decimal("15.00")
    assert usd_posting.price.currency == "ARS"
    assert usd_posting.price.number == Decimal("1580.00")

    liability = [posting for posting in entry.postings if posting.account == CARD]
    assert len(liability) == 1
    assert liability[0].units.currency == "ARS"
    assert liability[0].units.number == Decimal("-23700.00")

    # Implied rate == the two amounts divided, computed from the ledger, not looked up.
    charged_ars = -liability[0].units.number
    assert usd_posting.units.number * usd_posting.price.number == charged_ars
    assert charged_ars / usd_posting.units.number == Decimal("1580.00")


def test_usd_savings_purchase_needs_no_rate() -> None:
    transactions, _ = _load()
    purchases = _tagged(transactions, "usd-savings")
    assert len(purchases) == 1
    entry = purchases[0]
    assert all(posting.price is None for posting in entry.postings)
    assert {posting.units.currency for posting in entry.postings} == {"USD"}


def test_no_market_rate_token_appears_in_sample_text() -> None:
    """The one raw-text check: no MEP/CCL marker anywhere in ``sample/``."""
    for path in SAMPLE_DIR.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").lower()
        for token in ("mep", "ccl"):
            assert token not in text, f"{token!r} must not appear in {path.name}"


def test_sample_contains_no_price_or_commodity_directive() -> None:
    """A market rate can enter through a directive the token scan never sees."""
    entries, errors, _ = loader.load_file(str(SAMPLE_LEDGER))
    assert errors == [], errors
    prices = [entry for entry in entries if isinstance(entry, data.Price)]
    commodities = [entry for entry in entries if isinstance(entry, data.Commodity)]
    assert prices == [], prices
    assert commodities == [], commodities


def test_the_only_priced_postings_are_the_recorded_rates() -> None:
    """A count of one is satisfied by the wrong price; the expected set is not.

    Two rates are legitimate: the card's administrative conversion (an observed
    statement fact) and the executed rate of the cross-currency transfer. A series
    lookup would add a third entry, or replace one of these.
    """
    transactions, _ = _load()
    priced = {
        (
            entry.narration,
            posting.account,
            posting.units.number,
            posting.units.currency,
            posting.price.number,
            posting.price.currency,
        )
        for entry in transactions
        for posting in entry.postings
        if posting.price is not None
    }
    assert priced == {
        (
            "Subscription charged in USD",
            "Expenses:ServiciosDigitales:Suscripciones",
            Decimal("15.00"),
            "USD",
            Decimal("1580.00"),
            "ARS",
        ),
        (
            "Cross-currency transfer",
            "Assets:BBVA:P1:CajaUSD",
            Decimal("500.00"),
            "USD",
            Decimal("1240.00"),
            "ARS",
        ),
    }, priced


def test_installment_plan_is_captured_as_metadata() -> None:
    transactions, _ = _load()
    purchases = _tagged(transactions, "installments")
    assert len(purchases) == 1
    entry = purchases[0]
    assert entry.meta["installments"] == 12
    assert entry.meta["first_due"] == "2026-04"

    # The amount is an Amount, not a bare number: the currency must survive parsing.
    installment_amount = entry.meta["installment_amount"]
    assert isinstance(installment_amount, Amount)
    assert installment_amount.number == Decimal("60000.00")
    assert installment_amount.currency == "ARS"

    expense = _expense_postings(entry)
    assert len(expense) == 1
    assert expense[0].account == "Expenses:Compras:Electrodomesticos"
    assert expense[0].units.number == Decimal("720000.00")


def test_installment_card_is_still_outstanding() -> None:
    """The plan is not amortized: its card carries the whole debt at the sample's end."""
    transactions, _ = _load()
    assert _balance(transactions, "Liabilities:Provincia:P2:Visa") == {
        "ARS": Decimal("-720000.00")
    }


def test_usd_suffixed_accounts_are_declared_usd() -> None:
    """The USD suffix is a human convention, so the tests enforce it structurally."""
    entries, errors, _ = loader.load_file(str(SAMPLE_LEDGER))
    assert errors == [], errors
    openings = [entry for entry in entries if isinstance(entry, data.Open)]
    assert openings, "sample must open accounts"

    suffixed = [entry for entry in openings if entry.account.endswith("USD")]
    assert suffixed, "sample must exercise at least one USD-suffixed account"
    mismatched = [
        (entry.account, entry.currencies)
        for entry in suffixed
        if set(entry.currencies or []) != {"USD"}
    ]
    assert mismatched == [], f"USD-suffixed accounts declared otherwise: {mismatched}"


def test_p3_has_no_income_and_is_funded_by_transfer() -> None:
    transactions, _ = _load()
    income_accounts = {
        posting.account
        for entry in transactions
        for posting in entry.postings
        if posting.account.startswith("Income:")
    }
    assert income_accounts, "income must be modelled so balances reconcile"
    assert all("P3" not in account for account in income_accounts)

    funding = _tagged(transactions, "transfer-to-p3")
    assert len(funding) == 1
    p3_leg = [
        posting
        for posting in funding[0].postings
        if posting.account == "Assets:MercadoPago:P3:Caja"
    ]
    assert len(p3_leg) == 1
    assert p3_leg[0].units.number > 0
