"""Table-driven tests for the Mercado Pago ``RESUMEN DE CUENTA EN PESOS`` parser.

The fixtures under ``tests/fixtures/mercadopago/`` are synthetic: invented names,
invented operation ids and invented amounts, laid out in the geometry of the real
document (a movement is a block of one to three lines, the payload is the line
ending in two amounts, and the table header repeats on every page).

The invented names are deliberately **non-words** (``Zxqv``, ``Qwerty``, ``Plugh``) and the
amounts are deliberately **not round**, because the repository's leak guard refuses content
that repeats a token of the real statements. The statement *template* words (``Periodo``,
``DETALLE DE MOVIMIENTOS``, ``Movimiento Valor Saldo``) cannot be avoided — a fixture that
does not reproduce the template proves nothing — and they are baselined in the ledger
directory with a written reason. Keep both properties when editing a fixture: a plausible
Spanish merchant name or a round amount is exactly what the guard exists to catch.

Coverage is deliberately two-sided. Half of it proves the parser is a *reader*:
every fixture reconciles, the vocabulary is closed, and a compact row and its
wrapped equivalent are the same movement. The other half proves it is a *gate*:
each failing case is produced by mutating a good fixture **in code**, so the
invariant a fixture breaks is named in the test rather than hidden inside a
committed broken file.

Masking is behaviour, not politeness. The reconciliation diagnostics are part of
the public surface because a human reads them when an import refuses, so a test
asserts that no diagnostic echoes a merchant name or an amount from the fixture.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from expensuchis.importers.mercadopago import (
    CHECK_NAMES,
    Movement,
    MovementKind,
    ReconciliationError,
    Resumen,
    ResumenParseError,
    parse_resumen,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mercadopago"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


COMPACT = _load("compact_rows.txt")
WRAPPED = _load("wrapped_rows.txt")
WRAPPED_DESCRIPTION = _load("wrapped_description.txt")


def _sum_positive(resumen: Resumen) -> Decimal:
    return sum((m.amount for m in resumen.movements if m.amount > 0), Decimal(0))


def _sum_negative(resumen: Resumen) -> Decimal:
    return sum((m.amount for m in resumen.movements if m.amount < 0), Decimal(0))


FIXTURES_TABLE = [
    ("compact_rows.txt", COMPACT, 5, Decimal("999.99"), Decimal("-444.44")),
    ("wrapped_rows.txt", WRAPPED, 8, Decimal("1123.44"), Decimal("-1383.92")),
    ("wrapped_description.txt", WRAPPED_DESCRIPTION, 1, Decimal("0.00"), Decimal("-321.09")),
]


# --------------------------------------------------------------------------- good fixtures


@pytest.mark.parametrize(
    ("name", "text", "count", "sum_positive", "sum_negative"),
    FIXTURES_TABLE,
    ids=[row[0] for row in FIXTURES_TABLE],
)
def test_every_fixture_parses_to_the_expected_movement_count_and_totals(
    name: str, text: str, count: int, sum_positive: Decimal, sum_negative: Decimal
) -> None:
    resumen = parse_resumen(text)
    assert len(resumen.movements) == count
    assert _sum_positive(resumen) == sum_positive
    assert _sum_negative(resumen) == sum_negative


@pytest.mark.parametrize(
    ("name", "text"),
    [(row[0], row[1]) for row in FIXTURES_TABLE],
    ids=[row[0] for row in FIXTURES_TABLE],
)
def test_every_check_reports_ok_on_a_good_fixture(name: str, text: str) -> None:
    checks = parse_resumen(text).checks
    assert [check.name for check in checks] == list(CHECK_NAMES)
    assert all(check.ok for check in checks), [c for c in checks if not c.ok]


def test_header_figures_are_parsed_as_signed_amounts_and_the_period_is_kept() -> None:
    resumen = parse_resumen(COMPACT)
    assert resumen.opening == Decimal("9876.54")
    assert resumen.declared_entries == Decimal("999.99")
    assert resumen.declared_withdrawals == Decimal("-444.44")
    assert resumen.closing == Decimal("10432.09")
    assert resumen.period_text == "01 de 01 de 2026 al 31 de 01 de 2026"


def test_a_compact_row_carries_its_date_id_amount_balance_page_and_line() -> None:
    movement = parse_resumen(COMPACT).movements[0]
    assert movement == Movement(
        date=dt.date(2026, 1, 1),
        operation_id="100000000001",
        description="Rendimientos Zxqv",
        amount=Decimal("111.11"),
        balance=Decimal("9987.65"),
        page=1,
        line=13,
        kind=MovementKind.RETURNS,
    )


def test_a_two_line_wrapped_description_keeps_the_id_from_the_payload_line() -> None:
    movement = parse_resumen(WRAPPED_DESCRIPTION).movements[0]
    assert movement.operation_id == "300000000001"
    assert movement.description == "Pago con Zxqv Qwerty Plugh"
    assert movement.kind is MovementKind.PURCHASE


def test_the_movement_vocabulary_is_a_closed_set_with_one_kind_per_verb() -> None:
    kinds = {movement.kind for movement in parse_resumen(WRAPPED).movements}
    assert kinds == {
        MovementKind.TRANSFER_RECEIVED,
        MovementKind.TRANSFER_SENT,
        MovementKind.PURCHASE,
        MovementKind.BILL_PAYMENT,
        MovementKind.WITHDRAWAL,
        MovementKind.HOLD,
        MovementKind.RETURNS,
    }


def test_a_page_break_advances_the_page_and_the_chain_continues_across_pages() -> None:
    resumen = parse_resumen(WRAPPED)
    assert [movement.page for movement in resumen.movements] == [1, 1, 1, 2, 2, 2, 2, 2]
    assert resumen.movements[-1].balance == resumen.closing


_HEADER = (
    "1/1\n"
    "RESUMEN DE CUENTA EN PESOS\n"
    "Zxqv Qwerty Plugh\n"
    "Zq: ZqZqZqZqZqZq CUIT/ ZqZq: ZqZqZqZqZqZq\n"
    "Periodo: 01 de 01 de 2026 al 31 de 01 de 2026\n"
    "Saldo inicial: $ 1.000,00\n"
    "Entradas: $ 100,00\n"
    "Salidas: $ 0,00\n"
    "Saldo final: $ 1.100,00\n"
    "DETALLE DE MOVIMIENTOS\n"
    "Fecha Operacion ID de operacion\n"
    "Movimiento Valor Saldo\n"
)


def _one_row_text(row_lines: list[str]) -> str:
    return _HEADER + "\n".join(row_lines) + "\n"


def test_a_compact_row_and_its_wrapped_equivalent_are_the_same_movement() -> None:
    compact = parse_resumen(
        _one_row_text(["01-01-2026 Rendimientos op-x-0001 $ 100,00 $ 1.100,00"])
    ).movements[0]
    wrapped = parse_resumen(
        _one_row_text(["01-01-2026", "Rendimientos", "op-x-0001 $ 100,00 $ 1.100,00"])
    ).movements[0]
    assert compact == wrapped


# --------------------------------------------------------------------------- mutations


def _replace_once(text: str, old: str, new: str) -> str:
    assert old in text, f"mutation target not found: {old!r}"
    return text.replace(old, new, 1)


def mutate_opening(text: str) -> str:
    """Move the declared opening balance one cent: breaks header arithmetic."""
    return _replace_once(text, "Saldo inicial: $ 9.876,54", "Saldo inicial: $ 9.876,55")


def mutate_entries(text: str) -> str:
    """Move the declared Entradas one cent: breaks the positive sum check."""
    return _replace_once(text, "Entradas: $ 999,99", "Entradas: $ 1.000,99")


def mutate_withdrawals(text: str) -> str:
    """Move the declared Salidas one cent: breaks the negative sum check."""
    return _replace_once(text, "Salidas: $ -444,44", "Salidas: $ -444,45")


def mutate_closing(text: str) -> str:
    """Move the declared closing balance one cent: breaks the closing check."""
    return _replace_once(text, "Saldo final: $ 10.432,09", "Saldo final: $ 10.432,10")


def mutate_middle_balance(text: str) -> str:
    """Shift one interior running balance: only the chain can see this."""
    return _replace_once(text, "$ 10.209,87", "$ 10.209,88")


def mutate_sign_swap(text: str) -> str:
    """Swap the signs of two equal-and-opposite rows: totals are unchanged."""
    text = _replace_once(text, "100000000004 $ -444,44", "100000000004 $ 444,44")
    return _replace_once(text, "100000000005 $ 444,44", "100000000005 $ -444,44")


def mutate_delete_row(text: str) -> str:
    """Delete one whole movement row."""
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if "100000000002" not in line]
    assert len(kept) == len(lines) - 1
    return "".join(kept)


def mutate_duplicate_id(text: str) -> str:
    """Copy one row's operation id onto a same-date, same-amount row."""
    return _replace_once(text, "100000000003", "100000000002")


def mutate_unknown_verb(text: str) -> str:
    """Replace a known verb with one outside the frozen vocabulary."""
    return _replace_once(text, "Rendimientos Zxqv", "Qwertyzacion Zxqv")


MUTATIONS: list[tuple[str, Callable[[str], str]]] = [
    ("header-arithmetic", mutate_opening),
    ("sum-positive-matches-entries", mutate_entries),
    ("sum-negative-matches-withdrawals", mutate_withdrawals),
    ("running-balance-chain", mutate_sign_swap),
    ("closing-balance", mutate_closing),
    ("keys-unique", mutate_duplicate_id),
    ("vocabulary-known", mutate_unknown_verb),
]


@pytest.mark.parametrize(
    ("check_name", "mutate"),
    MUTATIONS,
    ids=[name for name, _ in MUTATIONS],
)
def test_each_mutation_fails_the_check_it_targets_and_names_it(
    check_name: str, mutate: Callable[[str], str]
) -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate(COMPACT))
    failed = {check.name for check in excinfo.value.failures}
    assert check_name in failed
    assert check_name in str(excinfo.value)
    assert all(check.detail for check in excinfo.value.failures)


def test_every_check_is_reported_even_when_an_earlier_one_fails() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_opening(COMPACT))
    assert [check.name for check in excinfo.value.checks] == list(CHECK_NAMES)
    assert len(excinfo.value.failures) >= 1


def test_the_balance_chain_catches_a_sign_swap_even_when_the_totals_still_match() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_sign_swap(COMPACT))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"running-balance-chain"}


def test_a_shifted_interior_balance_fails_only_the_chain() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_middle_balance(COMPACT))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"running-balance-chain"}


def test_a_deleted_row_is_caught_by_the_balance_chain() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_resumen(mutate_delete_row(COMPACT))
    failed = {check.name for check in excinfo.value.failures}
    assert "running-balance-chain" in failed


def test_the_four_aggregate_checks_cannot_fail_alone() -> None:
    """They are mutually redundant given the chain and the declared figures.

    The chain makes ``last = opening + sum(values)``, and the two sum checks make
    ``sum(values) = Entradas + Salidas``, so closing-balance equals header arithmetic
    and each sum check follows from the others. A mutation that breaks any one of the
    four therefore breaks at least a second; only the chain, the key and the vocabulary
    checks are independently reachable.
    """
    targets = [
        ("header-arithmetic", mutate_opening),
        ("sum-positive-matches-entries", mutate_entries),
        ("sum-negative-matches-withdrawals", mutate_withdrawals),
        ("closing-balance", mutate_closing),
    ]
    for check_name, mutate in targets:
        with pytest.raises(ReconciliationError) as excinfo:
            parse_resumen(mutate(COMPACT))
        failed = {check.name for check in excinfo.value.failures}
        assert check_name in failed
        assert len(failed) >= 2, (check_name, failed)


def test_reconciliation_diagnostics_never_echo_statement_text_or_amounts() -> None:
    forbidden = (
        "Zxqv",
        "Qwerty",
        "Plugh",
        "100000000001",
        "10.432,09",
        "111,11",
        "999,99",
    )
    for _check_name, mutate in MUTATIONS:
        with pytest.raises(ReconciliationError) as excinfo:
            parse_resumen(mutate(COMPACT))
        rendered = str(excinfo.value) + "".join(check.detail for check in excinfo.value.checks)
        for token in forbidden:
            assert token not in rendered, (token, rendered)


# --------------------------------------------------------------------------- structure


def test_text_without_the_header_refuses_structurally_without_echoing_it() -> None:
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen("1/1\nRESUMEN DE CUENTA EN PESOS\nTitular Secreto\n")
    assert "Titular Secreto" not in str(excinfo.value)
    assert "opening" in str(excinfo.value)


def test_a_malformed_amount_refuses_without_echoing_the_value() -> None:
    mutated = _replace_once(COMPACT, "$ 111,11", "$ 111,xx")
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(mutated)
    assert "100,xx" not in str(excinfo.value)


def test_the_strict_amount_path_masks_a_number_the_shape_regex_accepts() -> None:
    """The payload line is found by loose shape and then parsed strictly.

    ``1.2.3,45`` matches the payload pattern — digits, dots and a two-decimal tail —
    and the strict Argentine parser refuses it, so this reaches the *second* stage.
    That stage is a different message from the structural one above, and it is the
    one that could leak a raw number: pinning only the structural path leaves it
    unpinned, which a mutation proved by echoing the number and failing no test.
    """
    mutated = _replace_once(COMPACT, "$ 111,11", "$ 1.2.3,45")
    with pytest.raises(ResumenParseError) as excinfo:
        parse_resumen(mutated)
    message = str(excinfo.value)
    assert "1.2.3" not in message
    assert "value" in message and "line" in message


def test_crlf_line_endings_are_normalised_before_parsing() -> None:
    resumen = parse_resumen(COMPACT.replace("\n", "\r\n"))
    assert len(resumen.movements) == 5


def test_a_wrapped_movement_that_crosses_a_page_boundary_is_refused() -> None:
    text = _HEADER + "01-01-2026\nRendimientos\n\f1/2\nop-x-0001 $ 100,00 $ 1.100,00\n"
    with pytest.raises(ResumenParseError):
        parse_resumen(text)
