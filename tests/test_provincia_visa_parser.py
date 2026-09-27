"""Behaviour tests for the Banco Provincia card ``Liquidación Visa`` parser.

The fixtures under ``tests/fixtures/provincia_visa/`` are synthetic: invented
non-word merchants (``Zxqv``, ``Qwerty``, ``Plugh``), invented comprobantes and
coupons, and deliberately non-round amounts, laid out in the geometry of the real
document (a ten-line letterhead anchored on an ``(NNNN)`` token, ``SALDO ANTERIOR``
with two amounts, ``SU PAGO EN PESOS`` payment rows, a rule of underscores, charge
rows whose month is drawn once per group as a merged cell, a ``TOTAL CONSUMOS``
row, and post-total tax rows). A plausible Spanish merchant name or a round amount
is exactly what the leak guard exists to catch — keep both properties.

Coverage is two-sided, like the account parser's. Half proves the parser is a
*reader*: dates come from the merged cell plus the row day, descriptions stay
verbatim, USD-only rows keep one USD amount, and the paginated fixture resumes
after a repeated letterhead. The other half proves it is a *gate*: each failing
fixture breaks exactly one check, so the test can assert the failing check's name
AND that every other check is still reported — one run tells the whole story.

Masking is behaviour, not politeness: a test walks every failure and asserts no
diagnostic echoes a merchant, a comprobante, a coupon, a rate or an amount.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from expensuchis.importers.provincia_visa import (
    CHECK_NAMES,
    Balance,
    Charge,
    LiquidacionParseError,
    Payment,
    ReconciliationError,
    SurchargeKind,
    charge_keys,
    parse_liquidacion,
)

FIXTURES = Path(__file__).parent / "fixtures" / "provincia_visa"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


FULL = _load("full_statement.txt")
MINIMAL = _load("minimal_statement.txt")
PAGINATED = _load("paginated_statement.txt")
CROSS_YEAR = _load("cross_year_statement.txt")
PERCEPCION_SHAPES = _load("percepcion_shapes.txt")

#: Each failing fixture breaks exactly one check. The expected set is asserted
#: exactly, so a check that fires spuriously (or stops firing) is caught.
FAILING_FIXTURES: list[tuple[str, frozenset[str]]] = [
    ("fail_total_ars.txt", frozenset({"charge-sums-ars"})),
    ("fail_total_usd.txt", frozenset({"charge-sums-usd"})),
    ("fail_duplicate_comprobante.txt", frozenset({"comprobante-unique"})),
    ("fail_unclassified_line.txt", frozenset({"block-complete"})),
    ("fail_periods_order.txt", frozenset({"periods-ordered"})),
    ("fail_date_after_closing.txt", frozenset({"date-not-after-closing"})),
    ("fail_balance_chain.txt", frozenset({"balance-chain"})),
    ("fail_invalid_day.txt", frozenset({"day-valid"})),
    ("fail_unknown_surcharge.txt", frozenset({"surcharge-complete"})),
    ("fail_unrecognized_post_total.txt", frozenset({"surcharge-complete"})),
    ("fail_installment.txt", frozenset({"installment-valid"})),
]

#: Tokens a diagnostic must never echo: merchants, comprobantes, the coupon, the
#: rate, and amounts from the fixtures.
FORBIDDEN = (
    "Zxqv",
    "Qwerty",
    "Plugh",
    "400111",
    "400222",
    "400333",
    "400999",
    "42,13",
    "51,01",
    "53,24",
    "512,34",
    "1.751,68",
    "1.000,12",
    "661,49",
    "210,13",
    "137.284,59",
    "9.713,48",
    "12.345,67",
    "11.545,54",
    "11.545,55",
    "1.455,987",
)


# --------------------------------------------------------------------------- good fixtures


@pytest.mark.parametrize(
    "text",
    [FULL, MINIMAL, PAGINATED, CROSS_YEAR, PERCEPCION_SHAPES],
    ids=["full", "minimal", "paginated", "cross_year", "percepcion_shapes"],
)
def test_every_good_fixture_reconciles_with_every_check_ok(text: str) -> None:
    liquidacion = parse_liquidacion(text)
    assert [check.name for check in liquidacion.checks] == list(CHECK_NAMES)
    assert all(check.ok for check in liquidacion.checks), [
        check for check in liquidacion.checks if not check.ok
    ]


def test_the_full_fixture_parses_its_balances_period_and_totals() -> None:
    liquidacion = parse_liquidacion(FULL)
    assert liquidacion.opening_ars == Decimal("12345.67")
    assert liquidacion.opening_usd == Decimal("900.12")
    assert liquidacion.total_ars == Decimal("1751.68")
    assert liquidacion.total_usd == Decimal("512.34")
    # CIERRE 28 Mar 26, VENCIMIENTO 10 Abr 26: the real period line uses abbreviated
    # months, so this fixture pins defect-1 geometry, and the statement's year is
    # the closing one; the due date may fall in the next calendar year.
    assert liquidacion.period_year == 2026
    assert (liquidacion.closing_year, liquidacion.closing_month) == (2026, 3)
    assert (liquidacion.due_year, liquidacion.due_month) == (2026, 4)


def test_the_month_table_accepts_abbreviations_setiembre_and_full_names() -> None:
    """The real period line abbreviates (``Set``); the table is shared with merged cells.

    The statement built here carries no charges at all, so the window's cell check
    is vacuous (N8's documented case) and the letterhead's months are the only
    months under test.
    """
    letterhead = FULL.splitlines()[:10]
    letterhead[2] = "Periodo Zxqv CIERRE 28 Set 26 VENCIMIENTO 10 Oct 26"
    text = (
        "\n".join(letterhead)
        + "\nSALDO ANTERIOR 1.247,89 13,72\n"
        + "_" * 60
        + "\nTotales 0007 Total Consumos de Tarjeta Zxqv 0,00 * 0,00 *\n"
        + "IMPUESTO DE SELLOS $ 0,00\n"
    )
    liquidacion = parse_liquidacion(text)
    assert (liquidacion.closing_year, liquidacion.closing_month) == (2026, 9)
    assert (liquidacion.due_year, liquidacion.due_month) == (2026, 10)
    assert liquidacion.charges == ()


def test_a_charge_stamps_the_merged_cell_year_and_month_onto_the_row_day() -> None:
    charges = parse_liquidacion(FULL).charges
    assert [charge.when for charge in charges] == [
        dt.date(2026, 1, 9),
        dt.date(2026, 1, 21),
        dt.date(2026, 2, 5),
        dt.date(2026, 2, 27),
        dt.date(2026, 3, 3),
        dt.date(2026, 3, 14),
    ]


def test_a_charge_keeps_its_description_comprobante_and_position_verbatim() -> None:
    charge = parse_liquidacion(FULL).charges[5]
    assert charge == Charge(
        when=dt.date(2026, 3, 14),
        comprobante="400234",
        description="PAGOAPP* Zxqv Recarga",
        installment_number=3,
        installment_total=12,
        amount_ars=Decimal("200.34"),
        amount_usd=None,
        page=1,
        line=20,
    )


def test_a_lone_star_marker_stays_in_the_verbatim_description() -> None:
    charge = parse_liquidacion(FULL).charges[2]
    assert charge.description == "* SHOPONLINE* Plugh"
    assert charge.installment_number is None and charge.installment_total is None


def test_a_usd_only_row_keeps_one_usd_amount_and_no_ars() -> None:
    """The document prints the same value twice (importe and dólares): one USD amount."""
    charge = parse_liquidacion(FULL).charges[3]
    assert charge.amount_ars is None
    assert charge.amount_usd == Decimal("512.34")
    assert charge.when == dt.date(2026, 2, 27)


def test_installment_markers_are_parsed_into_a_number_total_tuple() -> None:
    charges = parse_liquidacion(FULL).charges
    assert [(charge.installment_number, charge.installment_total) for charge in charges] == [
        (2, 6),
        (1, 3),
        (None, None),
        (None, None),
        (None, None),
        (3, 12),
    ]


def test_the_payment_and_balance_rows_are_parsed_and_never_become_charges() -> None:
    liquidacion = parse_liquidacion(FULL)
    # The credit row, as printed: the trailing minus makes it a credit.
    assert liquidacion.payments == (
        Payment(
            when=dt.date(2026, 3, 25),
            amount_ars=Decimal("-800.13"),
            amount_usd=None,
            page=1,
            line=12,
        ),
    )
    # The balance row restates the block: previous ARS, the rate, resulting ARS,
    # resulting USD. It is identified by its TC token, never by position.
    assert liquidacion.balance == Balance(
        when=dt.date(2026, 3, 25),
        previous_ars=Decimal("12345.67"),
        rate=Decimal("1455.987"),
        resulting_ars=Decimal("-11545.54"),
        resulting_usd=Decimal("-900.12"),
        page=1,
        line=13,
    )
    # The payment description folds to SU PAGO EN PESOS and must never surface as
    # a consumption: the charges are exactly the six known comprobantes.
    assert [charge.comprobante for charge in liquidacion.charges] == [
        "400567",
        "400678",
        "400345",
        "400456",
        "400123",
        "400234",
    ]


def test_the_balance_chain_closes_in_magnitudes_on_the_full_fixture() -> None:
    """|opening| - |payments| == |resulting| and |previous| == |opening|, in magnitudes."""
    check = next(check for check in parse_liquidacion(FULL).checks if check.name == "balance-chain")
    assert check.ok
    assert "closes in magnitudes" in check.detail


def test_a_perception_row_needs_only_a_rate_a_base_and_an_amount() -> None:
    """The regime number is optional and the base may be ungrouped or fully glued:
    the last two rows replay the real document's base shapes."""
    surcharges = parse_liquidacion(PERCEPCION_SHAPES).surcharges
    assert [(surcharge.kind, surcharge.label) for surcharge in surcharges] == [
        (SurchargeKind.PERCEPCION, "IIBB Zxqv qwerty"),
        (SurchargeKind.PERCEPCION, "IVA RG 4240"),
        (SurchargeKind.PERCEPCION, "AA.AA 1234"),
        (SurchargeKind.PERCEPCION, "IIBB Zxqv"),
        (SurchargeKind.PERCEPCION, "IIBB Zxqv"),
    ]
    assert [surcharge.amount_ars for surcharge in surcharges] == [
        Decimal("249.75"),
        Decimal("105.00"),
        Decimal("28.00"),
        Decimal("171.72"),
        Decimal("113.45"),
    ]
    # (26 Marzo, matching the real shape: the block's first row carries the cell).
    assert [surcharge.when for surcharge in surcharges] == [
        dt.date(2026, 3, 5),
        dt.date(2026, 3, 6),
        dt.date(2026, 3, 7),
        dt.date(2026, 3, 26),
        dt.date(2026, 3, 27),
    ]


def test_a_second_balance_row_is_an_unobserved_shape_and_refuses() -> None:
    mutated = FULL.replace(
        "25 SU PAGO EN PESOS 12.345,67 TC1.455,987 11.545,54- 900,12-\n",
        "25 SU PAGO EN PESOS 12.345,67 TC1.455,987 11.545,54- 900,12-\n"
        "25 SU PAGO EN PESOS 12.345,67 TC1.455,987 11.545,54- 900,12-\n",
        1,
    )
    assert mutated != FULL
    with pytest.raises(LiquidacionParseError) as excinfo:
        parse_liquidacion(mutated)
    assert "second balance row" in str(excinfo.value)
    assert "12.345,67" not in str(excinfo.value)


def test_post_total_rows_are_classified_by_shape_not_by_label_alone() -> None:
    surcharges = parse_liquidacion(FULL).surcharges
    assert [(surcharge.kind, surcharge.label) for surcharge in surcharges] == [
        (SurchargeKind.SELLOS, "IMPUESTO DE SELLOS"),
        (SurchargeKind.SELLOS, "IMPUESTO DE SELLOS"),
        (SurchargeKind.PERCEPCION, "IIBB 5555"),
    ]
    assert surcharges[0].amount_ars == Decimal("12.34") and surcharges[0].amount_usd is None
    assert surcharges[1].amount_usd == Decimal("3.45") and surcharges[1].amount_ars is None
    assert surcharges[2].amount_ars == Decimal("75.00")
    # The perception row carries its own group cell (26 Marzo 27): the cell joins
    # the ordering sequence and stamps the row's date.
    assert surcharges[2].when == dt.date(2026, 3, 27)


def test_the_minimal_fixture_has_no_payments_and_zero_valued_surcharges() -> None:
    liquidacion = parse_liquidacion(MINIMAL)
    assert liquidacion.payments == ()
    assert len(liquidacion.charges) == 1
    assert liquidacion.charges[0] == Charge(
        when=dt.date(2026, 3, 8),
        comprobante="400111",
        description="Zxqv Qwerty",
        installment_number=None,
        installment_total=None,
        amount_ars=Decimal("42.13"),
        amount_usd=None,
        page=1,
        line=13,
    )
    assert liquidacion.total_usd == Decimal("0.00")
    assert all(surcharge.amount_ars in (None, Decimal("0.00")) for surcharge in liquidacion.surcharges)
    assert all(surcharge.when is None for surcharge in liquidacion.surcharges)


def test_the_paginated_fixture_resumes_after_the_repeated_letterhead() -> None:
    liquidacion = parse_liquidacion(PAGINATED)
    assert [charge.comprobante for charge in liquidacion.charges] == ["400777", "400888", "400999"]
    assert [charge.page for charge in liquidacion.charges] == [1, 2, 2]
    assert [charge.line for charge in liquidacion.charges] == [13, 12, 13]
    # The second page's merged cell (26 Febrero) is stamped onto both rows: the
    # group carry survives the page break and the skipped letterhead, and the
    # cell sequence stays non-decreasing across the break (Enero, then Febrero).
    assert [charge.when for charge in liquidacion.charges] == [
        dt.date(2026, 1, 12),
        dt.date(2026, 2, 3),
        dt.date(2026, 2, 13),
    ]
    assert liquidacion.total_ars == Decimal("388.88")


def test_the_cross_year_fixture_pins_that_a_previous_purchase_year_parses() -> None:
    """A plan bought in Noviembre 25 legitimately appears in a statement closing Agosto 26.

    The merged cell's year is the purchase year; equality with the letterhead's
    year was rejected as a check precisely because it refused every such document.
    """
    liquidacion = parse_liquidacion(CROSS_YEAR)
    assert liquidacion.charges[0].when == dt.date(2025, 11, 12)
    assert liquidacion.charges[0].when.year == 2025
    assert liquidacion.period_year == 2026
    assert (liquidacion.closing_year, liquidacion.closing_month) == (2026, 8)
    assert liquidacion.total_ars == Decimal("661.49")


# --------------------------------------------------------------------------- the gate


@pytest.mark.parametrize(
    ("name", "expected"),
    FAILING_FIXTURES,
    ids=[name for name, _expected in FAILING_FIXTURES],
)
def test_each_failing_fixture_breaks_exactly_its_check_and_reports_every_check(
    name: str, expected: frozenset[str]
) -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_liquidacion(_load(name))
    assert {check.name for check in excinfo.value.failures} == expected
    # One run tells the whole story: every check is reported, not just the failure.
    assert [check.name for check in excinfo.value.checks] == list(CHECK_NAMES)
    assert all(check.detail for check in excinfo.value.checks)


def test_a_group_cell_out_of_order_fires_periods_ordered() -> None:
    """Consumos are listed chronologically by purchase month: a decreasing cell refuses."""
    with pytest.raises(ReconciliationError) as excinfo:
        parse_liquidacion(_load("fail_periods_order.txt"))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"periods-ordered"}
    detail = next(check for check in excinfo.value.checks if check.name == "periods-ordered")
    assert "decrease" in detail.detail and "page" in detail.detail


def test_a_date_past_the_closing_date_fires_date_not_after_closing() -> None:
    """The CIERRE date is the bound: a charge dated after the closing day refuses."""
    with pytest.raises(ReconciliationError) as excinfo:
        parse_liquidacion(_load("fail_date_after_closing.txt"))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"date-not-after-closing"}
    detail = next(
        check for check in excinfo.value.checks if check.name == "date-not-after-closing"
    )
    assert "after the closing date" in detail.detail and "page" in detail.detail


def test_a_row_dated_exactly_on_the_closing_date_passes() -> None:
    """The closing date is the bound, inclusively: a row ON the CIERRE date is fine.

    Pins the boundary against the ``>`` → ``>=`` mutant, which would refuse the
    last cycle's own purchases.
    """
    mutated = MINIMAL.replace("26 Marzo 08 400111", "26 Marzo 28 400111", 1)
    liquidacion = parse_liquidacion(mutated)
    check = next(c for c in liquidacion.checks if c.name == "date-not-after-closing")
    assert check.ok


def test_a_repeated_identical_group_cell_passes() -> None:
    """Non-decreasing means equal is fine: a repeated group cell does not refuse.

    Pins the boundary against the ``<`` → ``<=`` mutant, which would refuse two
    groups of the same month inside one statement.
    """
    mutated = MINIMAL.replace(
        "26 Marzo 08 400111 Zxqv Qwerty 42,13",
        "26 Marzo 08 400111 Zxqv Qwerty 42,13\n26 Marzo 19 400222 Zxqv Plugh 7,77",
        1,
    ).replace("42,13 * 0,00", "49,90 * 0,00", 1)
    liquidacion = parse_liquidacion(mutated)
    check = next(c for c in liquidacion.checks if c.name == "periods-ordered")
    assert check.ok


def test_c_01_01_is_refused_by_installment_valid() -> None:
    """A plan total of one is not a plan: ``TOT >= 2`` is load-bearing.

    Pins the ``TOT >= 2`` clause against the mutant that drops it.
    """
    mutated = MINIMAL.replace(
        "26 Marzo 08 400111 Zxqv Qwerty 42,13",
        "26 Marzo 08 400111 Zxqv Qwerty 42,13\n19 400333 Zxqv Qwerty C.01/01 11,11",
        1,
    ).replace("42,13 * 0,00", "53,24 * 0,00", 1)
    with pytest.raises(ReconciliationError) as excinfo:
        parse_liquidacion(mutated)
    assert {check.name for check in excinfo.value.failures} == {"installment-valid"}


def test_the_rule_line_needs_forty_characters() -> None:
    """A 39-character separator is not the rule (the block does not close there);
    a 40-character one is.

    Pins the ``>= 40`` boundary against the ``>= 4`` mutant: with it, a short
    underscore run would be swallowed as the rule instead of refusing as an
    unclassified line.
    """
    short = MINIMAL.replace("_" * 60, "_" * 39, 1)
    assert short != MINIMAL
    with pytest.raises(ReconciliationError) as excinfo:
        parse_liquidacion(short)
    assert {check.name for check in excinfo.value.failures} == {"block-complete"}
    assert parse_liquidacion(MINIMAL.replace("_" * 60, "_" * 40, 1))


def test_a_cell_below_the_installment_window_fires_periods_window() -> None:
    """The window's lower bound: with no installment plans, three months back.

    A plan-less statement closing 28 Marzo cannot carry a Julio 25 purchase — it
    would have appeared on the earlier statement. The bound is the closing month
    shifted back by ``max(3, the largest installment number)`` months; the floor
    of 3 is what keeps a plan-less document windowed at a normal cycle.
    """
    mutated = MINIMAL.replace("26 Marzo 08 400111", "25 Marzo 08 400111", 1)
    with pytest.raises(ReconciliationError) as excinfo:
        parse_liquidacion(mutated)
    assert {check.name for check in excinfo.value.failures} == {"periods-window"}


def test_a_planless_document_windows_three_months_of_cells() -> None:
    """N3's shape: a plan-less statement legitimately carries the previous months' cells.

    The floor of 3 means a cell one, two or three months before the closing month
    is inside the window; a cell four or more months back refuses.
    """
    for cell in ("26 Enero 08", "26 Febrero 08", "26 Marzo 08"):
        mutated = MINIMAL.replace("26 Marzo 08 400111", f"{cell} 400111", 1)
        liquidacion = parse_liquidacion(mutated)
        check = next(c for c in liquidacion.checks if c.name == "periods-window")
        assert check.ok
    # Four months back is outside the plan-less window: Noviembre 25 (the bound
    # itself — Diciembre 25, three months back — is inside, and pinned above).
    mutated = MINIMAL.replace("26 Marzo 08 400111", "25 Noviembre 08 400111", 1)
    with pytest.raises(ReconciliationError) as excinfo:
        parse_liquidacion(mutated)
    assert {check.name for check in excinfo.value.failures} == {"periods-window"}


def test_a_c_01_99_row_cannot_widen_the_window() -> None:
    """N4's shape: the bound follows the installment NUMBER, never the plan total.

    A ``C.01/99`` row is the first billing of a plan; its purchase month is at
    most ``NN - 1 = 0`` months before the closing month, so the floor of 3 keeps
    the window at three months instead of letting TOT=99 admit a 2020 cell.
    """
    mutated = MINIMAL.replace(
        "26 Marzo 08 400111 Zxqv Qwerty 42,13",
        "20 Marzo 08 400111 Zxqv Qwerty C.01/99 11,11",
        1,
    ).replace("42,13 * 0,00", "11,11 * 0,00", 1)
    with pytest.raises(ReconciliationError) as excinfo:
        parse_liquidacion(mutated)
    assert {check.name for check in excinfo.value.failures} == {"periods-window"}


def test_a_cell_above_the_closing_month_fails_the_window_upper_bound() -> None:
    """N5's pin: the window's upper bound is load-bearing even though it is
    redundant with ``date-not-after-closing`` — which is also failing here, and
    the assertion pins both.
    """
    mutated = MINIMAL.replace("26 Marzo 08 400111", "26 Abril 08 400111", 1)
    with pytest.raises(ReconciliationError) as excinfo:
        parse_liquidacion(mutated)
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"periods-window", "date-not-after-closing"}


def test_a_prose_line_without_an_amount_closes_the_surcharge_block() -> None:
    """The legitimate termination: furniture without a trailing amount flows past.

    The full fixture's legal-prose lines carry no amount-shaped tail, so they end
    the post-total block with no ``surcharge-complete`` failure and no phantom
    rows — exactly the path the real document's pages take after the block.
    """
    liquidacion = parse_liquidacion(FULL)
    check = next(c for c in liquidacion.checks if c.name == "surcharge-complete")
    assert check.ok
    assert len(liquidacion.surcharges) == 3


def test_a_rate_bearing_prose_line_is_never_claimed_as_a_charge() -> None:
    """The strictness boundary: prose with a rate, even with parentheses, is not claimed.

    The perception pattern requires money inside the parentheses (an
    amount-shaped token between them); a legal-prose line — a rate plus words in
    parentheses — matches no marker and closes the block silently, which is what
    the real document's later pages need.
    """
    mutated = MINIMAL.replace(
        "IMPUESTO DE SELLOS USD 0,00",
        "IMPUESTO DE SELLOS USD 0,00\nZxqv intereses del 5% (art. 760) Plugh Zwernk",
        1,
    )
    liquidacion = parse_liquidacion(mutated)
    check = next(c for c in liquidacion.checks if c.name == "surcharge-complete")
    assert check.ok
    assert len(liquidacion.surcharges) == 2


#: One well-formed base document (MINIMAL up to the total row) with only the final
#: post-total row swapped: breadth per row shape with a cheap harness. Expectation
#: is ``ok`` or the name of the single failing check; anything uncontrolled
#: (``TypeError``, ``IndexError``, ...) escapes the harness and fails the test.
POST_TOTAL_CORPUS: list[tuple[str, str]] = [
    ("IIBB Zxqv 3% ( 2.187,43", "surcharge-complete"),  # R3-001: rate, '(' and no ')'
    ("IIBB Zxqv 3% 2.187,43 )", "surcharge-complete"),  # rate, ')' and no '('
    ("IIBB 3% ( ( 1,00 ) 75,00", "surcharge-complete"),  # otherwise unmatched (nested open)
    ("IIBB Zxqv 3% 2.187,43", "surcharge-complete"),  # a '%' with no parenthesis at all
    ("IIBB Zxqv 3% ()", "ok"),  # empty parentheses hold no money: the block closes
    ("Zxqv intereses 5% (art. 760) Plugh Zwernk", "ok"),  # non-money fragment: prose
    ("26 Marzo IIBB 3% ( 2.187,43 ) 75,00", "ok"),  # money, no glue: classified
    ("27 IIBB 3%( 2.187,43) 75,00", "ok"),  # money, glued on either side: classified
    ("IIBB 3% ( 1,00 ) ( 2,00 ) 75,00", "ok"),  # two separate pairs: classified
    ("26 Marzo 27 IIBB 3% ( 2.187,43 )", "surcharge-complete"),  # perception shape, no amount
    ("26 Marzo 27 IIBB 3% ( 2.187,43 ) 75,00 80,00 90,00", "surcharge-complete"),  # three amounts
    ("IMPUESTO DE SELLOS", "surcharge-complete"),  # SELLOS with zero amounts
    ("IMPUESTO DE SELLOS $ 12,34 3,45", "surcharge-complete"),  # SELLOS with two amounts
    ("IMPUESTO DE SELLOS USD 12,34 3,45 5,67", "surcharge-complete"),  # SELLOS with three
    ("Zxqv Qwerty prosa sin marca", "ok"),  # no marker at all: the block closes
]


@pytest.mark.parametrize(
    ("row", "expected"), POST_TOTAL_CORPUS, ids=[row for row, _expected in POST_TOTAL_CORPUS]
)
def test_malformed_post_total_rows_stay_inside_the_controlled_contract(
    row: str, expected: str
) -> None:
    """Every post-total row shape ends in a parse or one of the two controlled
    exceptions — never a ``TypeError``/``IndexError``/``AttributeError``/
    ``ValueError`` from inside the parser, which would fail this test with the
    row's shape in the id.
    """
    text = MINIMAL[: MINIMAL.index("IMPUESTO DE SELLOS")] + row + "\n"
    try:
        parse_liquidacion(text)
    except LiquidacionParseError:
        outcome = "structural"
    except ReconciliationError as error:
        outcome = ",".join(check.name for check in error.failures)
    else:
        outcome = "ok"
    assert outcome == expected, f"post-total row shape {row!r} produced {outcome!r}"


def test_a_malformed_amount_refuses_without_echoing_the_value() -> None:
    with pytest.raises(LiquidacionParseError) as excinfo:
        parse_liquidacion(_load("fail_malformed_amount.txt"))
    message = str(excinfo.value)
    assert "42,1" not in message
    assert "wrapping" in message and "line" in message


def test_a_charge_row_without_a_trailing_amount_is_refused_as_a_wrapped_row() -> None:
    """A row wrapping onto two lines has never been observed: refuse, never guess."""
    mutated = FULL.replace(
        "14 400234 PAGOAPP* Zxqv Recarga C.03/12 200,34",
        "14 400234 PAGOAPP* Zxqv Recarga C.03/12",
    )
    assert mutated != FULL
    with pytest.raises(LiquidacionParseError) as excinfo:
        parse_liquidacion(mutated)
    assert "wrapping" in str(excinfo.value)
    assert "400234" not in str(excinfo.value)


def test_a_paginated_block_that_does_not_resume_with_a_charge_row_refuses() -> None:
    mutated = PAGINATED.replace("26 Febrero 03 400888 Zxqv Plugh 222,22", "Zxqv Qwerty prosa suelta")
    assert mutated != PAGINATED
    with pytest.raises(LiquidacionParseError) as excinfo:
        parse_liquidacion(mutated)
    assert "resume" in str(excinfo.value)


def test_text_that_is_not_a_liquidacion_refuses_without_echoing_it() -> None:
    with pytest.raises(LiquidacionParseError) as excinfo:
        parse_liquidacion("Extracto de Cuenta\nTitular Secreto 12345.67\n")
    assert "Titular Secreto" not in str(excinfo.value)
    assert "letterhead" in str(excinfo.value)


def test_a_truncated_document_without_the_total_row_refuses() -> None:
    with pytest.raises(LiquidacionParseError) as excinfo:
        parse_liquidacion(MINIMAL[: MINIMAL.index("Totales")])
    assert "TOTAL CONSUMOS" in str(excinfo.value)


def test_an_invalid_charge_day_is_reported_by_day_valid_and_the_sums_cascade() -> None:
    """A charge row with an invalid day cannot be represented, so the sums see it gone."""
    mutated = MINIMAL.replace("26 Marzo 08 400111 Zxqv Qwerty 42,13", "26 Febrero 30 400111 Zxqv Qwerty 42,13")
    with pytest.raises(ReconciliationError) as excinfo:
        parse_liquidacion(mutated)
    failed = {check.name for check in excinfo.value.failures}
    assert "day-valid" in failed
    assert "charge-sums-ars" in failed


def test_every_failing_fixture_masks_statement_content() -> None:
    for name, _expected in FAILING_FIXTURES:
        with pytest.raises(ReconciliationError) as excinfo:
            parse_liquidacion(_load(name))
        rendered = str(excinfo.value) + "".join(check.detail for check in excinfo.value.checks)
        for token in FORBIDDEN:
            assert token not in rendered, (name, token, rendered)


def test_structural_refusals_mask_statement_content() -> None:
    mutated = FULL.replace("14 400234 PAGOAPP* Zxqv Recarga C.03/12 200,34", "14 400234 PAGOAPP* Zxqv Recarga")
    second_balance = FULL.replace(
        "25 SU PAGO EN PESOS 12.345,67 TC1.455,987 11.545,54- 900,12-\n",
        "25 SU PAGO EN PESOS 12.345,67 TC1.455,987 11.545,54- 900,12-\n"
        "25 SU PAGO EN PESOS 12.345,67 TC1.455,987 11.545,54- 900,12-\n",
        1,
    )
    for text in (_load("fail_malformed_amount.txt"), mutated, second_balance):
        with pytest.raises(LiquidacionParseError) as excinfo:
            parse_liquidacion(text)
        for token in FORBIDDEN:
            assert token not in str(excinfo.value), token


# --------------------------------------------------------------------------- the natural key


def test_the_natural_key_is_date_comprobante_amount() -> None:
    charges = parse_liquidacion(FULL).charges
    keys = charge_keys(charges)
    assert keys[0] == "2026-01-09:400567:150.78"
    assert keys[1] == "2026-01-21:400678:99.88"


def test_a_usd_only_row_keys_on_its_usd_amount() -> None:
    charges = parse_liquidacion(FULL).charges
    keys = charge_keys(charges)
    assert keys[3] == "2026-02-27:400456:512.34"

def test_the_keys_are_unique_across_a_reconciled_document() -> None:
    """The comprobante is unique per document, so the bare key needs no suffix."""
    charges = parse_liquidacion(FULL).charges
    keys = charge_keys(charges)
    assert len(keys) == len(set(keys))
