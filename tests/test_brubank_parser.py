"""Table-driven tests for the Brubank ``Resumen de Movimientos`` parser.

The fixtures under ``tests/fixtures/brubank/`` are synthetic: invented names
(``Persona Ejemplo``, ``Aguas Ejemplo``, ``Electrica Ejemplo``), invented
references (the leading digits of pi, e, the golden ratio, sqrt(2) and sqrt(5),
chosen precisely because they are obviously fictional and never resemble a real
Brubank reference) and invented amounts, laid out in the geometry the
2026-09-26 reconnaissance mapped (masked, shape-only, against one real file):
a header block (``Saldo Inicial``, ``Saldo Final``, ``Créditos``, ``Débitos``,
``Imp. Trans. Financieras``) that repeats verbatim as a recap at the end of the
last movement page, a movement table
(``Fecha | #Ref | Descripción | Débito | Crédito | Saldo``) with no wrapped rows
and no minus sign anywhere, and a footer period line
(``dd Mon yyyy al dd Mon yyyy``).

Coverage is two-sided, as in the Provincia and card-liquidación suites. Half of
it proves the parser is a *reader*: the good fixtures reconcile, the two special
descriptions (``Intereses pagados``, ``De una cuenta tuya - <bank>``) classify
correctly, and CRLF line endings normalize. The other half proves it is a
*gate*: every reconciliation-check failure is produced by mutating the
multi-page fixture **in code**, so the invariant a mutation breaks is named in
the test rather than hidden inside a committed broken file; every structural
failure (a malformed row, the forbidden ``Imp. Trans. Financieras`` row, a
mismatched header/recap, a missing or disagreeing period) is inline text, built
to isolate exactly one invariant.

Masking is behaviour, not politeness: a test asserts that no reconciliation
diagnostic echoes a reference, a name or an amount from the fixture.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from expensuchis.importers.brubank import (
    CHECK_NAMES,
    Movement,
    MovementKind,
    ReconciliationError,
    ResumenParseError,
    is_resumen_movimientos,
    movement_keys,
    parse_resumen,
)

FIXTURES = Path(__file__).parent / "fixtures" / "brubank"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


MINIMAL = _load("minimal.txt")
MULTIPAGE = _load("multipage.txt")

FIXTURES_TABLE = [
    ("minimal.txt", MINIMAL, 2, Decimal("3500.00"), Decimal("1200.50")),
    ("multipage.txt", MULTIPAGE, 5, Decimal("12697.99"), Decimal("1913.46")),
]


def _sum_credits(resumen) -> Decimal:
    return sum((m.amount for m in resumen.movements if m.amount > 0), Decimal(0))


def _sum_debits(resumen) -> Decimal:
    return -sum((m.amount for m in resumen.movements if m.amount < 0), Decimal(0))


# --------------------------------------------------------------------------- good fixtures


@pytest.mark.parametrize(
    ("name", "text", "count", "credits", "debits"),
    FIXTURES_TABLE,
    ids=[row[0] for row in FIXTURES_TABLE],
)
def test_every_fixture_parses_to_the_expected_movement_count_and_totals(
    name: str, text: str, count: int, credits: Decimal, debits: Decimal
) -> None:
    resumen = parse_resumen(text)
    assert len(resumen.movements) == count
    assert _sum_credits(resumen) == credits
    assert _sum_debits(resumen) == debits


@pytest.mark.parametrize(
    ("name", "text"),
    [(row[0], row[1]) for row in FIXTURES_TABLE],
    ids=[row[0] for row in FIXTURES_TABLE],
)
def test_every_check_reports_ok_on_a_good_fixture(name: str, text: str) -> None:
    checks = parse_resumen(text).checks
    assert [check.name for check in checks] == list(CHECK_NAMES)
    assert all(check.ok for check in checks), [c for c in checks if not c.ok]


def test_the_header_and_its_recap_are_reconciled_into_one_value() -> None:
    resumen = parse_resumen(MULTIPAGE)
    assert resumen.opening == Decimal("48732.15")
    assert resumen.closing == Decimal("59516.68")
    assert resumen.declared_credits == Decimal("12697.99")
    assert resumen.declared_debits == Decimal("1913.46")
    assert resumen.financial_transactions_tax == Decimal("99.00")


def test_the_financial_transactions_tax_is_kept_as_data_never_posted() -> None:
    """The field is parsed and carried on ``Resumen`` but never becomes a movement."""
    resumen = parse_resumen(MULTIPAGE)
    assert resumen.financial_transactions_tax == Decimal("99.00")
    assert all(m.description != "Imp. Trans. Financieras" for m in resumen.movements)


def test_the_period_is_parsed_from_the_repeated_footer_line() -> None:
    resumen = parse_resumen(MULTIPAGE)
    assert resumen.period_start == dt.date(2026, 8, 31)
    assert resumen.period_end == dt.date(2026, 9, 20)


def test_an_ordinary_row_carries_every_field_and_its_kind() -> None:
    movement = parse_resumen(MULTIPAGE).movements[0]
    assert movement == Movement(
        date=dt.date(2026, 8, 31),
        reference="3141592653",
        description="Persona Ejemplo",
        amount=Decimal("9876.54"),
        balance=Decimal("58608.69"),
        page=1,
        line=8,
        kind=MovementKind.ORDINARY,
        transfer_bank="",
    )


def test_a_debit_row_carries_a_negative_signed_amount() -> None:
    movement = parse_resumen(MULTIPAGE).movements[1]
    assert movement.amount == Decimal("-1234.56")
    assert movement.description == "Aguas Ejemplo"
    assert movement.kind is MovementKind.ORDINARY


def test_an_internal_transfer_row_carries_the_counterpart_bank() -> None:
    movement = parse_resumen(MULTIPAGE).movements[2]
    assert movement.kind is MovementKind.INTERNAL_TRANSFER
    assert movement.transfer_bank == "BBVA"
    assert movement.amount == Decimal("2500.00")


def test_an_interest_row_is_tagged_and_is_a_credit() -> None:
    movement = parse_resumen(MULTIPAGE).movements[3]
    assert movement.kind is MovementKind.INTEREST
    assert movement.description == "Intereses pagados"
    assert movement.amount == Decimal("321.45")


def test_movements_carry_their_page() -> None:
    pages = [movement.page for movement in parse_resumen(MULTIPAGE).movements]
    assert pages == [1, 1, 2, 2, 3]


def test_the_legal_prose_trailing_page_is_never_parsed_as_a_movement() -> None:
    """The fourth page carries no date-prefixed line and contributes no movements."""
    assert len(parse_resumen(MULTIPAGE).movements) == 5


def test_the_repeated_table_header_and_footer_are_never_parsed_as_movements() -> None:
    """The table header and the period footer repeat on every page; neither is a row."""
    text = (
        MULTIPAGE
        + "\fFecha #Ref Descripción Débito Crédito Saldo\nPeríodo 31 Ago 2026 al 20 Sep 2026\n"
    )
    assert len(parse_resumen(text).movements) == 5


def test_crlf_line_endings_are_normalised_before_parsing() -> None:
    resumen = parse_resumen(MINIMAL.replace("\n", "\r\n"))
    assert len(resumen.movements) == 2


# --------------------------------------------------------------------------- the identify marker


def test_is_resumen_movimientos_recognizes_the_title_line() -> None:
    assert is_resumen_movimientos(MULTIPAGE) is True
    assert is_resumen_movimientos(MINIMAL) is True


def test_is_resumen_movimientos_rejects_provincias_extracto() -> None:
    assert is_resumen_movimientos("Extracto de Cuenta\nFecha Concepto Importe\n") is False


def test_is_resumen_movimientos_requires_both_words_on_the_same_line() -> None:
    """Two words split across lines are not the title: the check is structural, not a scan."""
    text = "Resumen mensual\nEste documento no es un resumen de movimientos.\nOtros movimientos.\n"
    assert is_resumen_movimientos("Resumen\nde\nmovimientos\n") is False
    assert is_resumen_movimientos(text) is False


# --------------------------------------------------------------------------- the natural key


def test_the_natural_key_is_date_ref_amount() -> None:
    keys = movement_keys(parse_resumen(MULTIPAGE).movements)
    assert keys[0] == "2026-08-31:3141592653:9876.54"
    assert keys[1] == "2026-09-01:2718281828:-1234.56"
    assert keys[2] == "2026-09-05:1618033988:2500.00"
    assert keys[3] == "2026-09-10:1414213562:321.45"
    assert keys[4] == "2026-09-20:2236067977:-678.90"


def test_the_natural_key_carries_no_occurrence_suffix() -> None:
    """Unlike Provincia's key, the identity is #Ref alone: no ``#N`` suffix mechanism exists."""
    keys = movement_keys(parse_resumen(MULTIPAGE).movements)
    assert not any("#" in key for key in keys)


# --------------------------------------------------------------------------- reconciliation mutations


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, (
        f"expected exactly one occurrence of {old!r}, found {text.count(old)}"
    )
    return text.replace(old, new, 1)


def _replace_both(text: str, old: str, new: str) -> str:
    assert text.count(old) == 2, (
        f"expected exactly two occurrences of {old!r}, found {text.count(old)}"
    )
    return text.replace(old, new)


def mutate_interior_balance(text: str) -> str:
    """Shift one interior running saldo by one centavo: only the chain can see this."""
    return _replace_once(text, "1.234,56 - 57.374,13", "1.234,56 - 57.374,14")


def mutate_out_of_order_date(text: str) -> str:
    """Move one row's date earlier than its predecessor's: only date order breaks."""
    return _replace_once(text, "10-09-26 1414213562", "01-09-26 1414213562")


def mutate_date_outside_period(text: str) -> str:
    """Move the last row's date past the declared period end: only the period check sees it."""
    return _replace_once(text, "20-09-26 2236067977", "25-09-26 2236067977")


def mutate_duplicate_ref(text: str) -> str:
    """Reuse one row's #Ref on another row: only uniqueness breaks."""
    return _replace_once(text, "2236067977", "1414213562")


def mutate_closing_recap(text: str) -> str:
    """Move the declared closing balance (both the header and its recap) by one centavo."""
    return _replace_both(text, "Saldo Final $ 59.516,68", "Saldo Final $ 59.516,69")


def mutate_credits_recap(text: str) -> str:
    """Move the declared credits total (both copies) by one centavo.

    This is a deliberate double fault: the header's own balance equation reads
    the same (now-moved) figure, so ``header-balance-equation`` breaks too. See
    ``test_a_moved_credits_total_breaks_two_checks``.
    """
    return _replace_both(text, "Créditos $ 12.697,99", "Créditos $ 12.698,99")


def mutate_debits_recap(text: str) -> str:
    """Move the declared debits total (both copies) by one centavo. Also a double fault."""
    return _replace_both(text, "Débitos $ 1.913,46", "Débitos $ 1.913,47")


MUTATIONS: list[tuple[str, Callable[[str], str]]] = [
    ("running-balance-chain", mutate_interior_balance),
    ("ascending-dates", mutate_out_of_order_date),
    ("dates-in-period", mutate_date_outside_period),
    ("refs-unique", mutate_duplicate_ref),
    ("header-balance-equation", mutate_closing_recap),
    ("declared-credits", mutate_credits_recap),
    ("declared-debits", mutate_debits_recap),
]


@pytest.mark.parametrize(
    ("check_name", "mutate"),
    MUTATIONS,
    ids=[f"{name}-{index}" for index, (name, _) in enumerate(MUTATIONS)],
)
def test_each_mutation_fails_the_check_it_targets_and_names_it(
    check_name: str, mutate: Callable[[str], str]
) -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate(MULTIPAGE))
    failed = {check.name for check in excinfo.value.failures}
    assert check_name in failed
    assert check_name in str(excinfo.value)
    assert all(check.detail for check in excinfo.value.failures)


def test_every_check_is_reported_even_when_an_earlier_one_fails() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_interior_balance(MULTIPAGE))
    assert [check.name for check in excinfo.value.checks] == list(CHECK_NAMES)
    assert len(excinfo.value.failures) >= 1


def test_a_shifted_interior_balance_fails_only_the_chain() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_interior_balance(MULTIPAGE))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"running-balance-chain"}


def test_an_out_of_order_date_fails_only_ascending_dates() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_out_of_order_date(MULTIPAGE))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"ascending-dates"}


def test_a_date_outside_the_period_fails_only_that_check() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_date_outside_period(MULTIPAGE))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"dates-in-period"}


def test_a_duplicate_ref_fails_only_uniqueness() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_duplicate_ref(MULTIPAGE))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"refs-unique"}


def test_a_moved_closing_recap_fails_only_the_header_equation() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_closing_recap(MULTIPAGE))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"header-balance-equation"}


def test_a_moved_credits_total_breaks_two_checks() -> None:
    """Moving the declared Créditos also moves the figure the balance equation reads.

    The single-fault mutation cannot keep ``failed == {declared-credits}``: the
    header's own equation (``opening + credits - debits == closing``) uses the
    same now-moved total, so it breaks too. That double detection is the point
    of keeping both checks.
    """
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_credits_recap(MULTIPAGE))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"declared-credits", "header-balance-equation"}


def test_a_moved_debits_total_breaks_two_checks() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_debits_recap(MULTIPAGE))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"declared-debits", "header-balance-equation"}


def test_reconciliation_diagnostics_never_echo_statement_text_or_amounts() -> None:
    forbidden = (
        "Persona Ejemplo",
        "Aguas Ejemplo",
        "Electrica Ejemplo",
        "3141592653",
        "1618033988",
        "48.732,15",
        "59.516,68",
        "678,90",
        "20-09-26",
        "BBVA",
    )
    for _check_name, mutate in MUTATIONS:
        with pytest.raises(ReconciliationError) as excinfo:
            parse_resumen(mutate(MULTIPAGE))
        rendered = str(excinfo.value) + "".join(check.detail for check in excinfo.value.checks)
        for token in forbidden:
            assert token not in rendered, (token, rendered)


# --------------------------------------------------------------------------- structural refusals


def test_a_row_with_both_debito_and_credito_refuses() -> None:
    text = MINIMAL.replace(
        "01-09-26 1234567890 Persona Ejemplo - 3.500,00 13.500,00",
        "01-09-26 1234567890 Persona Ejemplo 1,00 3.500,00 13.500,00",
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "both" in str(excinfo.value) or "neither" in str(excinfo.value)
    assert "Persona Ejemplo" not in str(excinfo.value)


def test_a_row_with_neither_debito_nor_credito_refuses() -> None:
    text = MINIMAL.replace(
        "01-09-26 1234567890 Persona Ejemplo - 3.500,00 13.500,00",
        "01-09-26 1234567890 Persona Ejemplo - - 13.500,00",
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "both" in str(excinfo.value) or "neither" in str(excinfo.value)


def test_a_row_with_a_minus_sign_is_an_unrecognized_shape_and_refuses() -> None:
    """The document carries no minus sign; a negative-looking amount fails to match at all."""
    text = MINIMAL.replace(
        "02-09-26 2345678901 Aguas Ejemplo 1.200,50 - 12.299,50",
        "02-09-26 2345678901 Aguas Ejemplo -1.200,50 - 12.299,50",
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "1.200,50" not in str(excinfo.value)
    assert "unrecognized" in str(excinfo.value)


def test_an_unrecognized_row_shape_refuses_without_echoing_it() -> None:
    text = MINIMAL.replace(
        "02-09-26 2345678901 Aguas Ejemplo 1.200,50 - 12.299,50\n",
        "02-09-26 2345678901 Aguas Ejemplo 1.200,50 - 12.299,50\n"
        "03-09-26 algo sin forma reconocida\n",
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "algo sin forma reconocida" not in str(excinfo.value)
    assert "unrecognized" in str(excinfo.value)


def test_an_extra_line_between_two_movement_rows_refuses() -> None:
    """A stray line inside the table region — e.g. a wrapped-description fragment.

    The document carries no wrapped descriptions (reconnaissance), so a line
    like this between two rows is not a continuation this module knows how to
    join: it must refuse rather than being silently dropped, which is exactly
    the failure mode that produced repeated CRITICAL findings in T-06b (a
    dropped non-monetary line passes every reconciliation check while still
    truncating a movement).
    """
    text = MULTIPAGE.replace(
        "31-08-26 3141592653 Persona Ejemplo - 9.876,54 58.608,69\n"
        "01-09-26 2718281828 Aguas Ejemplo 1.234,56 - 57.374,13\n",
        "31-08-26 3141592653 Persona Ejemplo - 9.876,54 58.608,69\n"
        "continuación de la descripción\n"
        "01-09-26 2718281828 Aguas Ejemplo 1.234,56 - 57.374,13\n",
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    message = str(excinfo.value)
    assert "continuación de la descripción" not in message
    assert "unrecognized" in message
    assert "page 1" in message


def test_an_unrecognized_line_before_the_recap_refuses() -> None:
    """A line between the last row and the recap block is still inside the region."""
    text = MULTIPAGE.replace(
        "20-09-26 2236067977 Electrica Ejemplo 678,90 - 59.516,68\nSaldo Inicial $ 48.732,15",
        "20-09-26 2236067977 Electrica Ejemplo 678,90 - 59.516,68\n"
        "nota inesperada\n"
        "Saldo Inicial $ 48.732,15",
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    message = str(excinfo.value)
    assert "nota inesperada" not in message
    assert "unrecognized" in message
    assert "page 3" in message


def test_a_malformed_date_inside_the_table_refuses_at_parse_time_not_reconciliation() -> None:
    """A movement-looking row with an impossible date refuses structurally.

    This must raise :class:`ResumenParseError` at parse time, never reach the
    reconciliation gate as a :class:`ReconciliationError` — the row is not a
    trustworthy movement to begin with, so there is nothing to reconcile.
    """
    text = MULTIPAGE.replace("05-09-26 1618033988", "31-04-26 1618033988")
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "31-04-26" not in str(excinfo.value)
    assert "real date" in str(excinfo.value)


def test_a_row_carrying_the_forbidden_financial_transactions_tax_refuses() -> None:
    text = MINIMAL.replace(
        "01-09-26 1234567890 Persona Ejemplo - 3.500,00 13.500,00",
        "01-09-26 1234567890 Imp. Trans. Financieras - 3.500,00 13.500,00",
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "header-only" in str(excinfo.value)


def test_a_row_with_an_invalid_date_refuses_without_echoing_it() -> None:
    text = MINIMAL.replace("01-09-26", "31-04-26")
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "31-04-26" not in str(excinfo.value)
    assert "real date" in str(excinfo.value)


def test_a_missing_header_field_refuses() -> None:
    text = "\n".join(line for line in MINIMAL.split("\n") if not line.startswith("Créditos"))
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "declared_credits" in str(excinfo.value)
    assert "0 time" in str(excinfo.value)


def test_a_header_field_missing_its_recap_refuses() -> None:
    lines = MINIMAL.split("\n")
    # Drop only the *second* occurrence of the Créditos line (the recap).
    seen = 0
    kept = []
    for line in lines:
        if line.startswith("Créditos"):
            seen += 1
            if seen == 2:
                continue
        kept.append(line)
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen("\n".join(kept))
    assert "declared_credits" in str(excinfo.value)
    assert "1 time" in str(excinfo.value)


def test_a_recap_that_disagrees_with_its_opening_block_refuses() -> None:
    lines = MINIMAL.split("\n")
    seen = 0
    for index, line in enumerate(lines):
        if line.startswith("Créditos"):
            seen += 1
            if seen == 2:
                lines[index] = "Créditos $ 3.500,01"
    text = "\n".join(lines)
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "recap" in str(excinfo.value)
    assert "3.500,00" not in str(excinfo.value) and "3.500,01" not in str(excinfo.value)


def test_a_missing_period_line_refuses() -> None:
    text = "\n".join(line for line in MINIMAL.split("\n") if not line.startswith("Período"))
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "period" in str(excinfo.value)


def test_an_unknown_label_and_amount_line_inside_the_table_refuses() -> None:
    """``_HEADER_FIELD_RE`` matches any label-then-amount shape, not only the five

    known header labels. A stray line like a bank commission note ending in an
    amount must not be mistaken for a header/recap field closing the region —
    it must refuse, not silently close the region and drop the rows that
    follow on the same page.
    """
    text = MULTIPAGE.replace(
        "05-09-26 1618033988 De una cuenta tuya - BBVA - 2.500,00 59.874,13\n"
        "10-09-26 1414213562 Intereses pagados - 321,45 60.195,58\n",
        "05-09-26 1618033988 De una cuenta tuya - BBVA - 2.500,00 59.874,13\n"
        "Comisión mantenimiento cuenta 1.234,56\n"
        "10-09-26 1414213562 Intereses pagados - 321,45 60.195,58\n",
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    message = str(excinfo.value)
    assert "Comisión mantenimiento cuenta" not in message
    assert "1.234,56" not in message
    assert "unrecognized" in message
    assert "page 2" in message


def test_a_period_shaped_substring_among_other_text_inside_the_table_refuses() -> None:
    """The region must close on the *whole* footer line, not a substring search.

    A note that merely contains a period-shaped run of text among other words
    is not the footer line and must refuse, not silently close the region.
    """
    text = MULTIPAGE.replace(
        "05-09-26 1618033988 De una cuenta tuya - BBVA - 2.500,00 59.874,13\n"
        "10-09-26 1414213562 Intereses pagados - 321,45 60.195,58\n",
        "05-09-26 1618033988 De una cuenta tuya - BBVA - 2.500,00 59.874,13\n"
        "nota interna 31 Ago 2026 al 20 Sep 2026 pendiente de revision\n"
        "10-09-26 1414213562 Intereses pagados - 321,45 60.195,58\n",
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    message = str(excinfo.value)
    assert "nota interna" not in message
    assert "pendiente de revision" not in message
    assert "unrecognized" in message
    assert "page 2" in message


def test_a_movement_row_after_the_recap_footer_on_the_same_page_refuses() -> None:
    """A movement row must never be silently ignored, even outside a closed region.

    Once the table region on the last page closes at the footer/period line,
    a further movement-row-shaped line on that same page is not furniture: it
    must refuse rather than being dropped.
    """
    text = MULTIPAGE.replace(
        "Imp. Trans. Financieras $ 99,00\nPeríodo 31 Ago 2026 al 20 Sep 2026\n",
        "Imp. Trans. Financieras $ 99,00\nPeríodo 31 Ago 2026 al 20 Sep 2026\n"
        "25-09-26 9999999999 Otra Persona 1,00 - 1,00\n",
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    message = str(excinfo.value)
    assert "Otra Persona" not in message
    assert "outside" in message
    assert "page 3" in message


def test_disagreeing_period_lines_refuse_without_echoing_the_dates() -> None:
    text = MULTIPAGE.replace(
        "Período 31 Ago 2026 al 20 Sep 2026\n"
        "\fFecha #Ref Descripción Débito Crédito Saldo\n05-09-26",
        "Período 30 Ago 2026 al 20 Sep 2026\n"
        "\fFecha #Ref Descripción Débito Crédito Saldo\n05-09-26",
        1,
    )
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(text)
    assert "does not agree" in str(excinfo.value)
    assert "2026" not in str(excinfo.value)
