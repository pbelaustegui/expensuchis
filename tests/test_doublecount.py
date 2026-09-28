"""Tests for the double-counting beancount plugin (T-N+2).

These load synthetic beancount text through ``beancount.loader.load_string`` with
``plugin "expensuchis.doublecount"`` embedded in the string itself, so the tests
prove beancount actually discovers and runs the plugin end to end -- not just that
:func:`expensuchis.doublecount.doublecount` behaves correctly as a bare function.
"""

from __future__ import annotations

from beancount import loader

from expensuchis.doublecount import DoublecountError

_HEADER = """\
    option "operating_currency" "ARS"
    plugin "expensuchis.doublecount"

    2026-01-01 open Assets:BBVA:P1:Caja       ARS
    2026-01-01 open Assets:BBVA:P1:CajaUSD    USD
    2026-01-01 open Liabilities:BBVA:P1:Visa  ARS
    2026-01-01 open Liabilities:BBVA:P1:VisaUSD USD
    2026-01-01 open Expenses:Compras          ARS
    2026-01-01 open Expenses:ComprasUSD       USD
"""


def _errors(body: str) -> list[DoublecountError]:
    _entries, errors, _options = loader.load_string(_HEADER + body, dedent=True)
    return [error for error in errors if isinstance(error, DoublecountError)]


def test_a_normal_purchase_raises_no_error() -> None:
    body = """
    2026-02-10 * "A real merchant name" "A real narration"
      Expenses:Compras            10,000.00 ARS
      Liabilities:BBVA:P1:Visa   -10,000.00 ARS
    """
    assert _errors(body) == []


def test_a_normal_settlement_raises_no_error() -> None:
    body = """
    2026-02-20 * "BBVA" "Card statement settlement"
      Liabilities:BBVA:P1:Visa    10,000.00 ARS
      Assets:BBVA:P1:Caja        -10,000.00 ARS
    """
    assert _errors(body) == []


def test_a_settlement_that_also_posts_an_expense_is_flagged() -> None:
    body = """
    2026-02-20 * "A real merchant name" "A real narration that must not leak"
      Liabilities:BBVA:P1:Visa    10,000.00 ARS
      Expenses:Compras           -10,000.00 ARS
    """
    errors = _errors(body)
    assert len(errors) == 1
    message = errors[0].message
    assert "Liabilities:BBVA:P1:Visa" in message
    assert "2026-02-20" in message
    assert "A real merchant name" not in message
    assert "A real narration" not in message


def test_a_usd_settlement_that_also_posts_an_expense_is_flagged() -> None:
    body = """
    2026-03-22 * "BBVA" "USD card statement settlement"
      Liabilities:BBVA:P1:VisaUSD    15.00 USD
      Expenses:ComprasUSD           -15.00 USD
    """
    errors = _errors(body)
    assert len(errors) == 1
    assert "Liabilities:BBVA:P1:VisaUSD" in errors[0].message


def test_only_the_violating_transaction_is_flagged_among_several() -> None:
    body = """
    2026-02-10 * "A real merchant name" "A purchase"
      Expenses:Compras            10,000.00 ARS
      Liabilities:BBVA:P1:Visa   -10,000.00 ARS

    2026-02-20 * "BBVA" "A clean settlement"
      Liabilities:BBVA:P1:Visa    10,000.00 ARS
      Assets:BBVA:P1:Caja        -10,000.00 ARS

    2026-03-05 * "A real merchant name" "Another purchase"
      Expenses:Compras             5,000.00 ARS
      Liabilities:BBVA:P1:Visa    -5,000.00 ARS

    2026-03-20 * "A real merchant name" "The bad one"
      Liabilities:BBVA:P1:Visa     5,000.00 ARS
      Expenses:Compras            -5,000.00 ARS
    """
    errors = _errors(body)
    assert len(errors) == 1
    assert "2026-03-20" in errors[0].message
