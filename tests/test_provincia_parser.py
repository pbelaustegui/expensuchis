"""Table-driven tests for the Banco Provincia ``Extracto de cuenta`` parser.

The fixtures under ``tests/fixtures/provincia/`` are synthetic: invented names,
invented references and invented amounts, laid out in the geometry of the real
document (a movement is a block of one or two lines ending in ``<importe>
<dd-mm> <saldo>``, the column header repeats every page, the first data row is
``SALDO ANTERIOR``, and the last page carries boilerplate that must not parse).

The invented names are deliberately **non-words** (``Zxqv``, ``Qwerty``, ``Plugh``) and the
amounts are deliberately **not round**, because the repository's leak guard refuses content
that repeats a token of the real statements. The statement *template* words (``SALDO
ANTERIOR``, ``Fecha Concepto Importe Fecha Valor Saldo``, the frozen verbs) cannot be
avoided — a fixture that does not reproduce the template proves nothing — and they are
baselined in the ledger directory with a written reason. Keep both properties when editing
a fixture: a plausible Spanish merchant name or a round amount is exactly what the guard
exists to catch.

Coverage is deliberately two-sided. Half of it proves the parser is a *reader*:
every fixture reconciles, the vocabulary is closed, and a compact row and its
wrapped equivalent are the same movement. The other half proves it is a *gate*:
each failing case is produced by mutating a good fixture **in code**, so the
invariant a fixture breaks is named in the test rather than hidden inside a
committed broken file.

Masking is behaviour, not politeness. The reconciliation diagnostics are part of
the public surface because a human reads them when an import refuses, so a test
asserts that no diagnostic echoes a reference, a name or an amount from the fixture.

The ``keys-unique`` check is unique by construction — the natural key carries a
deterministic ``#N`` suffix on repeated ``(date, identity, amount)`` rows — but not
unbreakable: a description that *already carries* the ``#2`` suffix collides with the
second occurrence of the plain description. That crafted case is pinned below so the
guard cannot silently rot into ``ok=True``.

The closing summary: the last page carries a line of exactly two ``$``-prefixed
amounts — the closing saldo and the total debits. The document's own labels for those
columns are uncertain from the geometry, so the match is structural (two ``$`` figures
on one line), and the fixtures' figures make the semantics observable: the first
figure equals the last row's saldo, the second the sum of the negative importes. Both
are reconciliation checks, so a dropped last row, a truncated document or a
0-movement statement is refused, not silently accepted.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from expensuchis.importers.provincia import (
    CHECK_NAMES,
    ExtractoParseError,
    Movement,
    MovementKind,
    ReconciliationError,
    movement_keys,
    parse_extracto,
)

FIXTURES = Path(__file__).parent / "fixtures" / "provincia"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


DESCRIBED = _load("described_rows.txt")
REFERENCE = _load("reference_rows.txt")
WRAPPED = _load("wrapped_rows.txt")
SHAPES = _load("shapes_rows.txt")

FIXTURES_TABLE = [
    ("described_rows.txt", DESCRIBED, 9, Decimal("4160.77"), Decimal("-1339.72")),
    ("reference_rows.txt", REFERENCE, 3, Decimal("120.49"), Decimal("-60.06")),
    ("wrapped_rows.txt", WRAPPED, 9, Decimal("5.55"), Decimal("-280.52")),
    ("shapes_rows.txt", SHAPES, 8, Decimal("0.00"), Decimal("-422.25")),
]


def _sum_positive(extracto) -> Decimal:
    return sum((m.amount for m in extracto.movements if m.amount > 0), Decimal(0))


def _sum_negative(extracto) -> Decimal:
    return sum((m.amount for m in extracto.movements if m.amount < 0), Decimal(0))


# --------------------------------------------------------------------------- good fixtures


@pytest.mark.parametrize(
    ("name", "text", "count", "sum_positive", "sum_negative"),
    FIXTURES_TABLE,
    ids=[row[0] for row in FIXTURES_TABLE],
)
def test_every_fixture_parses_to_the_expected_movement_count_and_totals(
    name: str, text: str, count: int, sum_positive: Decimal, sum_negative: Decimal
) -> None:
    extracto = parse_extracto(text)
    assert len(extracto.movements) == count
    assert _sum_positive(extracto) == sum_positive
    assert _sum_negative(extracto) == sum_negative


@pytest.mark.parametrize(
    ("name", "text"),
    [(row[0], row[1]) for row in FIXTURES_TABLE],
    ids=[row[0] for row in FIXTURES_TABLE],
)
def test_every_check_reports_ok_on_a_good_fixture(name: str, text: str) -> None:
    checks = parse_extracto(text).checks
    assert [check.name for check in checks] == list(CHECK_NAMES)
    assert all(check.ok for check in checks), [c for c in checks if not c.ok]


def test_the_opening_balance_is_parsed_from_the_saldo_anterior_row() -> None:
    assert parse_extracto(DESCRIBED).opening == Decimal("1000.37")
    assert parse_extracto(WRAPPED).opening == Decimal("500.29")


def test_a_described_row_carries_every_field_and_its_kind() -> None:
    movement = parse_extracto(DESCRIBED).movements[0]
    assert movement == Movement(
        date=dt.date(2026, 1, 1),
        description="sueldo Zxqv Qwerty",
        reference="",
        amount=Decimal("2500.13"),
        value_date="01-01",
        balance=Decimal("3500.50"),
        page=1,
        line=4,
        kind=MovementKind.SALARY,
    )


def test_a_reference_row_keeps_its_reference_and_no_description() -> None:
    movement = parse_extracto(REFERENCE).movements[0]
    assert movement.description == ""
    assert movement.reference == "Nº.12345 78/78-1.234567 9:12345678901"
    assert movement.amount == Decimal("-50.07")
    assert movement.balance == Decimal("1950.12")
    assert movement.value_date == "10-01"
    assert movement.kind is MovementKind.REFERENCE


def test_a_wrapped_description_and_a_compact_row_are_the_same_movement() -> None:
    header = (
        "Extracto de Cuenta\nFecha Concepto Importe Fecha Valor Saldo\nSALDO ANTERIOR 1000.37\n"
    )
    compact = parse_extracto(
        header + "01/01/2026 compra Zxqv Qwerty Plugh -100.37 01-01 900.00\n"
        "Fecha Valor                       Importe\n"
        "$ 900.00                          $ 100.37\n"
    ).movements[0]
    wrapped = parse_extracto(
        header + "01/01/2026 compra Zxqv Qwerty Plugh\n-100.37 01-01 900.00\n"
        "Fecha Valor                       Importe\n"
        "$ 900.00                          $ 100.37\n"
    ).movements[0]
    assert compact == wrapped


def test_the_movement_vocabulary_is_a_closed_set_covering_every_kind() -> None:
    kinds: set[MovementKind] = set()
    for _name, text, _count, _pos, _neg in FIXTURES_TABLE:
        kinds |= {movement.kind for movement in parse_extracto(text).movements}
    assert kinds == {
        MovementKind.PURCHASE,
        MovementKind.CARD_PURCHASE,
        MovementKind.BILL_PAYMENT,
        MovementKind.CARD_SETTLEMENT,
        MovementKind.SALARY,
        MovementKind.EARNINGS,
        MovementKind.DEPOSIT,
        MovementKind.CREDIT,
        MovementKind.INTEREST,
        MovementKind.FEES,
        MovementKind.CHARGES,
        MovementKind.DEBIT,
        MovementKind.REFUND,
        MovementKind.TOPUP,
        MovementKind.REFERENCE,
    }


def test_a_page_break_advances_the_page_and_the_chain_continues_across_pages() -> None:
    extracto = parse_extracto(WRAPPED)
    assert [movement.page for movement in extracto.movements] == [1, 1, 2, 2, 2, 2, 2, 2, 2]
    assert extracto.movements[-1].balance == Decimal("225.32")


def test_the_last_page_boilerplate_is_never_parsed_as_a_movement() -> None:
    """The footer's ``$``-prefixed summary and legends are furniture: nine rows, no more."""
    text = WRAPPED + "Fecha Valor Importe $ 999.99\n$ 123.45\nLegales Zxqv S.A.\n"
    assert len(parse_extracto(text).movements) == 9


# --------------------------------------------------------------------------- mutations


def _replace_once(text: str, old: str, new: str) -> str:
    assert old in text, f"mutation target not found: {old!r}"
    return text.replace(old, new, 1)


def mutate_opening(text: str) -> str:
    """Move the declared opening balance one cent: the chain breaks at movement 1."""
    return _replace_once(text, "SALDO ANTERIOR 500.29", "SALDO ANTERIOR 500.30")


def mutate_interior_balance(text: str) -> str:
    """Shift one interior running saldo by a cent: only the chain can see this."""
    return _replace_once(text, " 354.64", " 354.65")


def mutate_amount(text: str) -> str:
    """Move one importe by a cent: the chain breaks at that row."""
    return _replace_once(text, "-45.63 20-02", "-45.64 20-02")


def mutate_verb(text: str) -> str:
    """Replace a known verb with one outside the frozen vocabulary."""
    return _replace_once(text, "compra Zxqv Qwerty Plugh", "qwertyzacion Zxqv Qwerty Plugh")


def mutate_closing(text: str) -> str:
    """Move the declared closing saldo one cent: only the closing check can see it."""
    return _replace_once(text, "$ 225.32", "$ 225.33")


def mutate_total_debits(text: str) -> str:
    """Move the declared total debits one cent: only the debits check can see it."""
    return _replace_once(text, "$ 280.52", "$ 280.53")


MUTATIONS: list[tuple[str, Callable[[str], str]]] = [
    ("running-balance-chain", mutate_opening),
    ("running-balance-chain", mutate_interior_balance),
    ("running-balance-chain", mutate_amount),
    ("closing-balance", mutate_closing),
    ("declared-debits", mutate_total_debits),
    ("vocabulary-known", mutate_verb),
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
        parse_extracto(mutate(WRAPPED))
    failed = {check.name for check in excinfo.value.failures}
    assert check_name in failed
    assert check_name in str(excinfo.value)
    assert all(check.detail for check in excinfo.value.failures)


def test_a_shifted_interior_balance_fails_only_the_chain() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_extracto(mutate_interior_balance(WRAPPED))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"running-balance-chain"}


def test_a_moved_amount_breaks_the_chain_and_the_declared_debits() -> None:
    """An importe shift is caught twice: by the chain and by the footer's total.

    The single-fault mutation cannot keep ``failed == {running-balance-chain}``
    anymore: the moved cent also changes the sum of the negative importes away from
    the declared total debits. That double detection is the point of the footer
    checks.
    """
    with pytest.raises(ReconciliationError) as excinfo:
        parse_extracto(mutate_amount(WRAPPED))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"running-balance-chain", "declared-debits"}


def test_an_unknown_verb_fails_only_the_vocabulary_check() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_extracto(mutate_verb(WRAPPED))
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"vocabulary-known"}


def test_every_check_is_reported_even_when_an_earlier_one_fails() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        parse_extracto(mutate_opening(WRAPPED))
    assert [check.name for check in excinfo.value.checks] == list(CHECK_NAMES)
    assert len(excinfo.value.failures) >= 1


# --------------------------------------------------------------- the natural key


def test_the_natural_key_is_date_identity_amount() -> None:
    extracto = parse_extracto(DESCRIBED)
    keys = movement_keys(extracto.movements)
    assert keys[0] == "2026-01-01:sueldo Zxqv Qwerty:2500.13"
    assert keys[5] == "2026-01-06:compra tarjeta Zxqv:-400.11"


def test_a_reference_row_keys_on_the_recipient_cuit() -> None:
    """The ``X:XXXXXXXXXXX`` field is the recipient's CUIT/CUIL — the key's identity.

        The user confirmed the reference rows are immediate transfers, so two transfers
    to the same person must not collapse: the CUIT (not the moving reference numbers)
    is the stable part. The CUIT is personal data: it is key material that reaches the
    private ledger's staged and report bytes by design, never a module or probe log line.
    """
    extracto = parse_extracto(REFERENCE)
    keys = movement_keys(extracto.movements)
    assert keys[0] == "2026-01-10:12345678901:-50.07"
    assert keys[1] == "2026-01-11:93847463012:120.49"


def test_two_same_day_transfers_to_one_cuit_get_the_occurrence_suffix() -> None:
    header = (
        "Extracto de Cuenta\nFecha Concepto Importe Fecha Valor Saldo\nSALDO ANTERIOR 2000.19\n"
    )
    text = (
        header
        + "01/01/2026 Nº.99991 11/11-1.111111 9:12345678901 -50.07 01-01 1950.12\n"
        + "01/01/2026 Nº.99992 22/22-2.222222 9:12345678901 -50.07 01-01 1900.05\n"
        + "Fecha Valor                       Importe\n"
        + "$ 1900.05                         $ 100.14\n"
    )
    keys = movement_keys(parse_extracto(text).movements)
    assert keys[0] == "2026-01-01:12345678901:-50.07"
    assert keys[1] == "2026-01-01:12345678901#2:-50.07"


def test_a_reference_row_without_a_cuit_shaped_token_keys_on_its_reference() -> None:
    header = (
        "Extracto de Cuenta\nFecha Concepto Importe Fecha Valor Saldo\nSALDO ANTERIOR 2000.19\n"
    )
    text = (
        header
        + "01/01/2026 Nº.99991 11/11-1.111111 -50.07 01-01 1950.12\n"
        + "Fecha Valor                       Importe\n"
        + "$ 1950.12                         $ 50.07\n"
    )
    keys = movement_keys(parse_extracto(text).movements)
    assert keys[0] == "2026-01-01:Nº.99991 11/11-1.111111:-50.07"


def test_repeated_rows_get_a_deterministic_occurrence_suffix() -> None:
    """Two identical same-day rows must not collide: the second identity carries ``#2``.

    The suffix makes the key unique for ordinary repeats; the crafted collision that
    still reaches ``keys-unique`` as a failure is pinned below.
    """
    row_a = "01/01/2026 compra Zxqv Uno -10.05 01-01 990.32"
    row_b = "01/01/2026 compra Zxqv Uno -10.05 01-01 980.27"
    header = (
        "Extracto de Cuenta\nFecha Concepto Importe Fecha Valor Saldo\nSALDO ANTERIOR 1000.37\n"
    )
    text = (
        header
        + row_a
        + "\n"
        + row_b
        + "\n"
        + ("Fecha Valor                       Importe\n$ 980.27                          $ 20.10\n")
    )
    extracto = parse_extracto(text)
    keys = movement_keys(extracto.movements)
    assert keys[0] == "2026-01-01:compra Zxqv Uno:-10.05"
    assert keys[1] == "2026-01-01:compra Zxqv Uno#2:-10.05"
    keys_unique = next(check for check in extracto.checks if check.name == "keys-unique")
    assert keys_unique.ok


def test_a_description_already_carrying_the_suffix_makes_keys_unique_fail() -> None:
    """The only way ``keys-unique`` can fail: a description that already says ``#2``.

    Row 2 (the plain repeat) gets the ``#2`` suffix; row 3's literal ``#2``
    description then produces the same key. A guard mutant (``ok=True`` or
    ``repeated=0``) would let this document through; the refusal pins the guard.
    """
    text = (
        "Extracto de Cuenta\n"
        "Fecha Concepto Importe Fecha Valor Saldo\n"
        "SALDO ANTERIOR 1000.37\n"
        "01/01/2026 compra Zxqv Uno -10.05 01-01 990.32\n"
        "01/01/2026 compra Zxqv Uno -10.05 01-01 980.27\n"
        "01/01/2026 compra Zxqv Uno#2 -10.05 01-01 970.22\n"
        "Fecha Valor                       Importe\n"
        "$ 970.22                          $ 30.15\n"
    )
    with pytest.raises(ReconciliationError) as excinfo:
        parse_extracto(text)
    assert {check.name for check in excinfo.value.failures} == {"keys-unique"}


# ------------------------------------------------------- the closing summary (footer)


def test_the_footer_summary_is_parsed_into_the_extracto() -> None:
    """Two ``$``-prefixed figures: the closing saldo, then the total debits."""
    extracto = parse_extracto(WRAPPED)
    assert extracto.closing == Decimal("225.32")
    assert extracto.declared_debits == Decimal("280.52")


def test_a_document_without_movements_is_refused() -> None:
    """An opening balance and a footer but no rows: nothing may be silently accepted."""
    text = (
        "Extracto de Cuenta\n"
        "Fecha Concepto Importe Fecha Valor Saldo\n"
        "SALDO ANTERIOR 1000.37\n"
        "Fecha Valor                       Importe\n"
        "$ 1000.37                         $ 0.00\n"
    )
    with pytest.raises(ReconciliationError) as excinfo:
        parse_extracto(text)
    assert "closing-balance" in {check.name for check in excinfo.value.failures}


def test_a_truncated_document_without_the_closing_summary_refuses() -> None:
    text = _replace_once(
        WRAPPED,
        "Fecha Valor                       Importe\n$ 225.32                          $ 280.52\n",
        "",
    )
    with pytest.raises(ExtractoParseError) as excinfo:
        parse_extracto(text)
    assert "225.32" not in str(excinfo.value) and "280.52" not in str(excinfo.value)


def test_a_repeated_closing_summary_refuses() -> None:
    text = WRAPPED + "$ 225.32                          $ 280.52\n"
    with pytest.raises(ExtractoParseError) as excinfo:
        parse_extracto(text)
    assert "more than once" in str(excinfo.value)


# ------------------------------------------------------- token-boundary classification


def test_card_classification_matches_whole_leading_tokens_not_substrings() -> None:
    """``pago VISALUD`` is a bill payment; only ``pago visa`` as a token pair is not.

    The substring form would send ``pago VISALUD`` to the card liability; the token
    form keeps it on the counterparty map. ``compra Tarjeta Shopping`` keeps the
    ``compra tarjeta`` leading pair (a debit-card purchase) but posts through the
    map per the user-confirmed correction, never to the liability.
    """
    text = (
        "Extracto de Cuenta\n"
        "Fecha Concepto Importe Fecha Valor Saldo\n"
        "SALDO ANTERIOR 1000.37\n"
        "01/01/2026 pago VISALUD -10.05 01-01 990.32\n"
        "02/01/2026 pago Zxqv Visaplus -20.05 02-01 970.27\n"
        "03/01/2026 compra Tarjeta Shopping -30.05 03-01 940.22\n"
        "04/01/2026 pago visa Zxqv -40.05 04-01 900.17\n"
        "Fecha Valor                       Importe\n"
        "$ 900.17                          $ 100.20\n"
    )
    kinds = [movement.kind for movement in parse_extracto(text).movements]
    assert kinds == [
        MovementKind.BILL_PAYMENT,
        MovementKind.BILL_PAYMENT,
        MovementKind.CARD_PURCHASE,
        MovementKind.CARD_SETTLEMENT,
    ]


def test_a_reference_head_without_any_digit_is_not_a_reference_row() -> None:
    """The reference shape needs at least one digit: ``Nº.`` alone is an unknown verb.

    Without that clause the head would classify as a reference row and be accepted;
    with it the document refuses through ``vocabulary-known``.
    """
    text = (
        "Extracto de Cuenta\n"
        "Fecha Concepto Importe Fecha Valor Saldo\n"
        "SALDO ANTERIOR 1000.37\n"
        "01/01/2026 Nº. -10.05 01-01 990.32\n"
        "Fecha Valor                       Importe\n"
        "$ 990.32                          $ 10.05\n"
    )
    with pytest.raises(ReconciliationError) as excinfo:
        parse_extracto(text)
    assert {check.name for check in excinfo.value.failures} == {"vocabulary-known"}


# ------------------------------------------------------------------ structure


def test_text_without_the_saldo_anterior_row_refuses_without_echoing_it() -> None:
    with pytest.raises(ExtractoParseError) as excinfo:
        parse_extracto("Extracto de Cuenta\nTitular Secreto 12345.67\n")
    assert "Titular Secreto" not in str(excinfo.value)
    assert "SALDO ANTERIOR" in str(excinfo.value)


def test_a_repeated_saldo_anterior_row_refuses() -> None:
    text = WRAPPED.replace("\f", "\fSALDO ANTERIOR 500.29\n", 1)
    with pytest.raises(ExtractoParseError) as excinfo:
        parse_extracto(text)
    assert "more than once" in str(excinfo.value)


def test_a_malformed_amount_refuses_without_echoing_the_value() -> None:
    mutated = _replace_once(WRAPPED, "-45.63 20-02", "-45,63 20-02")
    with pytest.raises(ExtractoParseError) as excinfo:
        parse_extracto(mutated)
    assert "-45,63" not in str(excinfo.value)


def test_the_strict_amount_path_masks_a_number_the_shape_regex_accepts() -> None:
    """The tail is found by loose shape and then parsed strictly.

    ``-45.633`` matches the tail pattern — digits, dots and a run — and the strict
    plain parser refuses it, so this reaches the *second* stage. That stage is a
    different message from the structural one, and it is the one that could leak a
    raw number: pinning only the structural path leaves it unpinned.
    """
    mutated = _replace_once(WRAPPED, "-45.63 20-02", "-45.633 20-02")
    with pytest.raises(ExtractoParseError) as excinfo:
        parse_extracto(mutated)
    message = str(excinfo.value)
    assert "-45.633" not in message
    assert "value" in message and "line" in message


def test_a_date_prefixed_line_without_a_tail_or_continuation_refuses() -> None:
    """A movement the parser cannot understand must refuse, never be silently dropped."""
    text = WRAPPED + "29/02/2026 compra algo sin cola\n"
    with pytest.raises(ExtractoParseError) as excinfo:
        parse_extracto(text)
    assert "compra algo sin cola" not in str(excinfo.value)
    assert "continuation" in str(excinfo.value)


def test_a_wrapped_movement_that_crosses_a_page_boundary_is_refused() -> None:
    text = (
        "Extracto de Cuenta\n"
        "Fecha Concepto Importe Fecha Valor Saldo\n"
        "SALDO ANTERIOR 1000.37\n"
        "01/01/2026 compra Zxqv Qwerty Plugh\n"
        "\f2/2\n"
        "Fecha Concepto Importe Fecha Valor Saldo\n"
        "-100.37 01-01 900.00\n"
        "Fecha Valor                       Importe\n"
        "$ 900.00                          $ 100.37\n"
    )
    with pytest.raises(ExtractoParseError) as excinfo:
        parse_extracto(text)
    assert "continuation" in str(excinfo.value)


def test_a_continuation_whose_head_is_the_last_line_of_a_page_is_refused() -> None:
    """The continuation is the first line of the next page: the pair must not join.

    This reaches the page-boundary branch itself — the line right after the head is
    a well-formed continuation, but on the wrong page — unlike the test above, where
    an interleaved line fails first on the missing continuation.
    """
    text = (
        "Extracto de Cuenta\n"
        "Fecha Concepto Importe Fecha Valor Saldo\n"
        "SALDO ANTERIOR 1000.37\n"
        "01/01/2026 compra Zxqv Qwerty Plugh\f"
        "-100.37 01-01 900.00\n"
        "Fecha Valor                       Importe\n"
        "$ 900.00                          $ 100.37\n"
    )
    with pytest.raises(ExtractoParseError) as excinfo:
        parse_extracto(text)
    assert "page boundary" in str(excinfo.value)


def test_crlf_line_endings_are_normalised_before_parsing() -> None:
    extracto = parse_extracto(DESCRIBED.replace("\n", "\r\n"))
    assert len(extracto.movements) == 9


def test_reconciliation_diagnostics_never_echo_statement_text_or_amounts() -> None:
    forbidden = (
        "Zxqv",
        "Qwerty",
        "Plugh",
        "Nº.12345",
        "12345678901",
        "-45.63",
        "225.32",
        "500.29",
        "20/02/2026",
    )
    for _check_name, mutate in MUTATIONS:
        with pytest.raises(ReconciliationError) as excinfo:
            parse_extracto(mutate(WRAPPED))
        rendered = str(excinfo.value) + "".join(check.detail for check in excinfo.value.checks)
        for token in forbidden:
            assert token not in rendered, (token, rendered)
