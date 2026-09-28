"""Table-driven tests for the BBVA Argentina ``Extracto consolidado`` parser.

The fixtures under ``tests/fixtures/bbva/`` are synthetic: invented merchants
(``COMERCIO EJEMPLO``, ``KIOSCO EJEMPLO``), invented account numbers
(``000-111111/1``), invented CUITs (``20000000001``) and invented amounts.

Coverage is two-sided, as in the Brubank and Provincia suites. Half of it
proves the parser is a *reader*: the good fixtures reconcile, every concept
class classifies correctly, the debit-card detail join and the sent-transfer
join both resolve. The other half proves it is a *gate*: every
reconciliation-check failure and every structural refusal is produced by
mutating a good fixture in code, so the invariant a mutation breaks is named
in the test.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from expensuchis.importers.bbva import (
    CHECK_NAMES,
    ExtractoConsolidadoParseError,
    MovementKind,
    ReconciliationError,
    is_bbva_extracto,
    movement_keys,
    parse_extracto_consolidado,
)
from expensuchis.importers.brubank import is_resumen_movimientos
from expensuchis.importers.provincia import parse_extracto as provincia_parse_extracto

FIXTURES = Path(__file__).parent / "fixtures" / "bbva"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


FULL = _load("full_statement.txt")
MULTI_BLOCK = _load("multi_block_quiescent.txt")

ANCHOR = dt.date(2026, 9, 25)


def _parse(text: str = FULL, **kwargs):
    kwargs.setdefault("anchor", ANCHOR)
    return parse_extracto_consolidado(text, **kwargs)


# --------------------------------------------------------------------------- good fixtures


def test_the_full_statement_reconciles_with_every_check_ok() -> None:
    extracto = _parse(FULL)
    assert [check.name for check in extracto.checks] == list(CHECK_NAMES)
    assert all(check.ok for check in extracto.checks), [c for c in extracto.checks if not c.ok]


def test_the_full_statement_yields_the_expected_movement_count_and_close_date() -> None:
    extracto = _parse(FULL)
    assert len(extracto.movements) == 10
    assert extracto.close_date == dt.date(2026, 9, 20)


def test_the_multi_block_quiescent_statement_reconciles_with_no_movements() -> None:
    extracto = _parse(MULTI_BLOCK)
    assert extracto.movements == ()
    assert all(check.ok for check in extracto.checks), [c for c in extracto.checks if not c.ok]


# --------------------------------------------------------------------------- concept classes


def test_every_concept_class_is_classified() -> None:
    movements = _parse(FULL).movements
    kinds = [m.kind for m in movements]
    assert kinds == [
        MovementKind.DEBIT_CARD_PURCHASE,
        MovementKind.VISA_SETTLEMENT,
        MovementKind.MASTERCARD_SETTLEMENT,
        MovementKind.CASH_WITHDRAWAL,
        MovementKind.SALARY,
        MovementKind.DEBIT_CARD_PURCHASE,
        MovementKind.INTEREST,
        MovementKind.TRANSFER_OUT,
        MovementKind.TRANSFER_OUT,
        MovementKind.TRANSFER_IN,
    ]


@pytest.mark.parametrize(
    ("row", "kind"),
    [
        ("06/09 CUENTA VISA 58273941605837", MovementKind.VISA_SETTLEMENT),
        ("07/09 CUENTA MASTERCARD 70491638527049", MovementKind.MASTERCARD_SETTLEMENT),
    ],
)
def test_a_card_settlement_row_may_print_nro_before_its_account(
    row: str, kind: MovementKind
) -> None:
    """The real statement prints ``CUENTA VISA NRO. <account>`` (T-07c probe,
    2026-09-27); the shorter form stays accepted."""
    account = row.rsplit(" ", 1)[1]
    text = FULL.replace(row, row.replace(account, f"NRO. {account}"), 1)
    assert text != FULL
    movements = _parse(text).movements
    assert [m.kind for m in movements].count(kind) == 1


def test_a_salary_row_may_carry_a_trailing_detail() -> None:
    """The real statement prints a word and a reference after ``PAGO HABERES``
    (T-07c probe, 2026-09-27); salary posts to a fixed account, so the detail
    only has to be accepted, never interpreted."""
    text = FULL.replace("004 PAGO HABERES 5", "004 PAGO HABERES FULANOS QX4817293 5", 1)
    assert text != FULL
    movements = _parse(text).movements
    assert [m.kind for m in movements].count(MovementKind.SALARY) == 1


def test_a_sent_transfer_row_may_print_its_origin_account_with_separators() -> None:
    """The real statement prints the origin account as ``ddd-dddddd/d`` (T-07c
    probe, 2026-09-27); the unseparated form stays accepted."""
    text = FULL.replace("CBU $ 0002222222", "CA $ 000-222222/2", 1).replace(
        "CBU $ 0004444444", "CA $ 000-444444/4", 1
    )
    assert text != FULL
    movements = _parse(text).movements
    transfers = [m for m in movements if m.kind is MovementKind.TRANSFER_OUT]
    assert [m.recipient_cuit for m in transfers] == ["20000000001", "20000000002"]


def test_a_debit_card_purchase_carries_its_joined_merchant() -> None:
    movements = _parse(FULL).movements
    purchases = [m for m in movements if m.kind is MovementKind.DEBIT_CARD_PURCHASE]
    assert [m.merchant for m in purchases] == ["COMERCIO EJEMPLO", "KIOSCO EJEMPLO"]
    assert purchases[0].amount == Decimal("-483.17")
    assert purchases[1].amount == Decimal("-237.94")


def test_a_transfer_out_carries_its_joined_recipient_cuit() -> None:
    movements = _parse(FULL).movements
    transfers_out = [m for m in movements if m.kind is MovementKind.TRANSFER_OUT]
    assert [m.recipient_cuit for m in transfers_out] == ["20000000001", "20000000002"]


def test_non_purchase_non_transfer_out_movements_carry_no_merchant_or_cuit() -> None:
    movements = _parse(FULL).movements
    others = [
        m
        for m in movements
        if m.kind not in (MovementKind.DEBIT_CARD_PURCHASE, MovementKind.TRANSFER_OUT)
    ]
    assert all(m.merchant == "" for m in others)
    assert all(m.recipient_cuit == "" for m in others)


# --------------------------------------------------------------------------- the natural key


def test_the_natural_key_is_account_date_amount_balance() -> None:
    keys = movement_keys(_parse(FULL).movements)
    assert keys[0] == "000-111111/1:2026-09-05:-483.17:9954.02"
    assert keys[-1] == "000-111111/1:2026-09-13:917.53:11824.15"


def test_the_natural_key_is_unique_on_the_good_fixture() -> None:
    keys = movement_keys(_parse(FULL).movements)
    assert len(keys) == len(set(keys))


# --------------------------------------------------------------------------- the identify marker


def test_is_bbva_extracto_recognizes_the_full_statement() -> None:
    assert is_bbva_extracto(FULL) is True
    assert is_bbva_extracto(MULTI_BLOCK) is True


def test_is_bbva_extracto_rejects_other_banks_fixtures() -> None:
    brubank_minimal = (Path(__file__).parent / "fixtures" / "brubank" / "minimal.txt").read_text(
        encoding="utf-8"
    )
    provincia_shapes = (
        Path(__file__).parent / "fixtures" / "provincia" / "shapes_rows.txt"
    ).read_text(encoding="utf-8")
    provincia_visa_minimal = (
        Path(__file__).parent / "fixtures" / "provincia_visa" / "minimal_statement.txt"
    ).read_text(encoding="utf-8")
    mercadopago_compact = (
        Path(__file__).parent / "fixtures" / "mercadopago" / "compact_rows.txt"
    ).read_text(encoding="utf-8")
    assert is_bbva_extracto(brubank_minimal) is False
    assert is_bbva_extracto(provincia_shapes) is False
    assert is_bbva_extracto(provincia_visa_minimal) is False
    assert is_bbva_extracto(mercadopago_compact) is False


def test_other_identify_predicates_reject_the_bbva_fixtures() -> None:
    assert is_resumen_movimientos(FULL) is False
    assert is_resumen_movimientos(MULTI_BLOCK) is False


def test_a_bbva_page_carries_resumen_and_movimientos_on_different_lines() -> None:
    """A structural distinction from Brubank's title line, which requires both

    words on the *same* line -- the BBVA fixture never has that shape.
    """
    text = "EXTRACTO CONSOLIDADO\nRESUMEN DE CUENTAS\nMOVIMIENTOS DEL PERIODO\n"
    assert is_bbva_extracto(text) is False  # no table header / CONSOLIDADO-on-header-page marker
    assert is_resumen_movimientos(text) is False


def test_provincias_extracto_parser_refuses_the_bbva_fixture() -> None:
    with pytest.raises((ValueError, RuntimeError)):
        provincia_parse_extracto(FULL)


# --------------------------------------------------------------------------- crlf


def test_crlf_line_endings_are_normalised_before_parsing() -> None:
    extracto = parse_extracto_consolidado(FULL.replace("\n", "\r\n"), anchor=ANCHOR)
    assert len(extracto.movements) == 10


# --------------------------------------------------------------------------- December -> January crossing

_CROSS_YEAR = (
    "TARJETAS DE DEBITO\n"
    "DETALLE\n"
    "TITULAR VISA DEBITO 9999\n"
    "FECHA TARJETA FULANO EJEMPLO\n"
    "CUENTA DEBITO NRO $ 000-555555/5\n"
    "DETALLE\n"
    "MOVIMIENTOS EN PESOS\n"
    "CC $ 000-555555/5 CAJA DE AHORRO FINAL\n"
    "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
    "SALDO ANTERIOR 1.283,07\n"
    "28/12 PAGO HABERES 618,44 1.901,51\n"
    "03/01 INTERESES GANADOS 29,53 1.931,04\n"
    "SALDO AL 05 DE ENERO 1.931,04\n"
    "TOTAL MOVIMIENTOS -0,00 647,97\n"
    "LOS MOVIMIENTOS QUE GENERARON PERCEPCION DE IVA SE INFORMAN CON EL DETALLE DEL CREDITO\n"
)


def test_a_row_whose_month_is_later_than_the_close_month_belongs_to_the_previous_year() -> None:
    extracto = parse_extracto_consolidado(_CROSS_YEAR, anchor=dt.date(2027, 1, 10))
    assert extracto.close_date == dt.date(2027, 1, 5)
    dates = [m.date for m in extracto.movements]
    assert dates == [dt.date(2026, 12, 28), dt.date(2027, 1, 3)]


# --------------------------------------------------------------------------- reconciliation mutations


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, (
        f"expected exactly one occurrence of {old!r}, found {text.count(old)}"
    )
    return text.replace(old, new, 1)


def mutate_balance_chain(text: str) -> str:
    return _replace_once(text, "-483,17 9.954,02", "-483,17 9.954,03")


def mutate_closing_balance(text: str) -> str:
    return _replace_once(
        text, "SALDO AL 20 DE SEPTIEMBRE 11.824,15", "SALDO AL 20 DE SEPTIEMBRE 11.824,16"
    )


def mutate_total_movements(text: str) -> str:
    return _replace_once(
        text, "TOTAL MOVIMIENTOS -4.718,56 6.105,52", "TOTAL MOVIMIENTOS -4.718,57 6.105,52"
    )


def mutate_out_of_order_date(text: str) -> str:
    return _replace_once(text, "06/09 CUENTA VISA", "01/09 CUENTA VISA")


def mutate_date_past_the_close(text: str) -> str:
    """Push the last (chronologically latest, non-decreasing) row's day past the

    block's own close day (20) while keeping it the latest date in the block, so
    only ``dates-in-window`` -- never ``ascending-dates`` -- sees it.
    """
    return _replace_once(
        text,
        "13/09 TRANSFERENCIA INMEDIATA FULANO 917,53 11.824,15",
        "21/09 TRANSFERENCIA INMEDIATA FULANO 917,53 11.824,15",
    )


MUTATIONS_SIMPLE = [
    ("running-balance-chain", mutate_balance_chain),
    ("closing-balance", mutate_closing_balance),
    ("total-movements", mutate_total_movements),
    ("ascending-dates", mutate_out_of_order_date),
    ("dates-in-window", mutate_date_past_the_close),
]


@pytest.mark.parametrize(
    ("check_name", "mutate"),
    MUTATIONS_SIMPLE,
    ids=[name for name, _ in MUTATIONS_SIMPLE],
)
def test_each_mutation_fails_the_check_it_names(check_name: str, mutate) -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        _parse(mutate(FULL))
    failed = {check.name for check in excinfo.value.failures}
    assert check_name in failed
    assert check_name in str(excinfo.value)


def test_every_check_is_reported_even_when_an_earlier_one_fails() -> None:
    with pytest.raises(ReconciliationError) as excinfo:
        _parse(mutate_balance_chain(FULL))
    assert [check.name for check in excinfo.value.checks] == list(CHECK_NAMES)
    assert len(excinfo.value.failures) >= 1


def test_reconciliation_diagnostics_never_echo_statement_text_or_amounts() -> None:
    forbidden = (
        "COMERCIO EJEMPLO",
        "KIOSCO EJEMPLO",
        "20000000001",
        "58273941605837",
        "000-111111/1",
        "9.954,02",
        "11.824,15",
        "20/09",
    )
    for _name, mutate in MUTATIONS_SIMPLE:
        with pytest.raises(ReconciliationError) as excinfo:
            _parse(mutate(FULL))
        rendered = str(excinfo.value) + "".join(check.detail for check in excinfo.value.checks)
        for token in forbidden:
            assert token not in rendered, (token, rendered)


_KEYS_COLLIDE = (
    "CC $ 000-999999/9 CAJA DE AHORRO FINAL\n"
    "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
    "SALDO ANTERIOR 1.148,62\n"
    "05/09 PAGO HABERES 437,85 1.586,47\n"
    "SALDO AL 20 DE SEPTIEMBRE 1.586,47\n"
    "TOTAL MOVIMIENTOS -0,00 437,85\n"
    "LOS MOVIMIENTOS QUE GENERARON PERCEPCION DE IVA SE INFORMAN CON EL DETALLE DEL CREDITO\n"
    "CA $ 000-999999/9 CAJA DE AHORRO FINAL\n"
    "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
    "SALDO ANTERIOR 1.148,62\n"
    "05/09 PAGO HABERES 437,85 1.586,47\n"
    "SALDO AL 20 DE SEPTIEMBRE 1.586,47\n"
    "TOTAL MOVIMIENTOS -0,00 437,85\n"
    "LOS MOVIMIENTOS QUE GENERARON PERCEPCION DE IVA SE INFORMAN CON EL DETALLE DEL CREDITO\n"
)


def test_two_blocks_sharing_an_account_and_a_row_fail_only_keys_unique() -> None:
    """``keys-unique`` cannot be reached by mutating a single good row in place:

    the natural key is ``<account>:<date>:<amount>:<balance>``, and ``balance``
    is exactly what ``running-balance-chain`` pins to ``previous + amount`` --
    duplicating a key within *one* chain forces that chain to break first. The
    only way to collide two keys without tripping any other check first is
    across two *independent* chains: two blocks (each internally consistent on
    its own opening/closing/totals) that happen to share both the sub-account
    number and one identical (date, amount, balance) row. Neither block carries
    a debit-card purchase or a TRANSFERENCIA-out row, and no section (A)/(C) is
    needed at all -- this document isolates ``keys-unique`` alone.
    """
    with pytest.raises(ReconciliationError) as excinfo:
        parse_extracto_consolidado(_KEYS_COLLIDE, anchor=ANCHOR)
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"keys-unique"}


def test_a_saldo_al_disagreement_across_blocks_fails_that_check() -> None:
    text = MULTI_BLOCK.replace(
        "CA EUR 000-333333/3 CAJA DE AHORRO EUROS FINAL\n"
        "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
        "SALDO ANTERIOR 214,83\n"
        "20/09 SIN MOVIMIENTOS 214,83\n"
        "SALDO AL 20 DE SEPTIEMBRE 214,83\n",
        "CA EUR 000-333333/3 CAJA DE AHORRO EUROS FINAL\n"
        "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
        "SALDO ANTERIOR 214,83\n"
        "19/09 SIN MOVIMIENTOS 214,83\n"
        "SALDO AL 19 DE SEPTIEMBRE 214,83\n",
    )
    with pytest.raises(ReconciliationError) as excinfo:
        _parse(text)
    failed = {check.name for check in excinfo.value.failures}
    assert "saldo-al-agreement" in failed


def test_a_transfer_out_without_a_matching_section_c_row_refuses() -> None:
    text = FULL.replace(
        "12/09 20000000002 111222333 0003333333 FULANO DOS $ 689,14 CBU $ 0004444444\n",
        "",
    )
    with pytest.raises(ReconciliationError) as excinfo:
        _parse(text)
    failed = {check.name for check in excinfo.value.failures}
    assert "transfers-matched" in failed
    assert "20000000002" not in str(excinfo.value)


def test_a_purchase_row_with_two_detail_candidates_refuses() -> None:
    text = FULL.replace(
        "05/09/2026 COMERCIO EJEMPLO 1234 REF PAGO 847261 $ -483,17\n",
        "05/09/2026 COMERCIO EJEMPLO 1234 REF PAGO 847261 $ -483,17\n"
        "05/09/2026 OTRO COMERCIO 1234 REF PAGO 938471 $ -483,17\n",
    )
    with pytest.raises(ReconciliationError) as excinfo:
        _parse(text)
    failed = {check.name for check in excinfo.value.failures}
    assert "debit-card-details-matched" in failed


def test_an_in_window_detail_row_without_a_partner_refuses() -> None:
    text = FULL.replace(
        "10/09/2026 KIOSCO EJEMPLO 1234 REF PAGO 519384 $ -237,94\n",
        "10/09/2026 KIOSCO EJEMPLO 1234 REF PAGO 519384 $ -237,94\n"
        "11/09/2026 OTRO NEGOCIO 1234 REF PAGO 604827 $ -84,26\n",
    )
    with pytest.raises(ReconciliationError) as excinfo:
        _parse(text)
    failed = {check.name for check in excinfo.value.failures}
    assert "debit-card-details-matched" in failed


def test_an_out_of_window_detail_row_is_silently_ignored() -> None:
    """The pre-existing 01/08/2026 detail row, before the table window, never
    participates in reconciliation.
    """
    extracto = _parse(FULL)
    assert all(check.ok for check in extracto.checks)


def test_a_detail_date_disagreeing_with_the_inferred_table_date_refuses() -> None:
    """The printed dd/mm/yyyy in section (A) says the previous year; the table

    row's dd/mm infers the anchor's year (2026). The join keys on the full
    date, so the mismatched year yields zero candidates for the purchase row
    -- a :class:`ReconciliationError` on ``debit-card-details-matched``, never
    a silent, wrong merchant join.
    """
    text = FULL.replace("05/09/2026 COMERCIO EJEMPLO", "05/09/2025 COMERCIO EJEMPLO")
    with pytest.raises(ReconciliationError) as excinfo:
        _parse(text)
    failed = {check.name for check in excinfo.value.failures}
    assert failed == {"debit-card-details-matched"}


# --------------------------------------------------------------------------- structural refusals


def test_an_unknown_concept_refuses() -> None:
    text = FULL.replace(
        "11/09 INTERESES GANADOS 63,28 12.638,39", "11/09 CONCEPTO DESCONOCIDO 63,28 12.638,39"
    )
    with pytest.raises(ExtractoConsolidadoParseError) as excinfo:
        _parse(text)
    assert "vocabulary" in str(excinfo.value)


def test_a_wrong_sign_for_a_known_concept_refuses() -> None:
    text = FULL.replace(
        "09/09 004 PAGO HABERES 5.124,71 12.813,05", "09/09 004 PAGO HABERES -5.124,71 7.688,34"
    )
    with pytest.raises(ExtractoConsolidadoParseError) as excinfo:
        _parse(text)
    assert "wrong sign" in str(excinfo.value)


def test_an_unrecognized_line_inside_a_table_refuses() -> None:
    text = FULL.replace(
        "08/09 003 EXTRACCION ELEC+CASH -317,89 7.688,34\n",
        "08/09 003 EXTRACCION ELEC+CASH -317,89 7.688,34\nnota interna sin molde\n",
    )
    with pytest.raises(ExtractoConsolidadoParseError) as excinfo:
        _parse(text)
    message = str(excinfo.value)
    assert "nota interna sin molde" not in message
    assert "unrecognized" in message


def test_a_footer_line_inside_a_table_refuses() -> None:
    """The repeating page/IVA footer lines are furniture outside a region but

    must never silently close or skip through an open one -- a table split
    across pages was never observed.
    """
    text = FULL.replace(
        "08/09 003 EXTRACCION ELEC+CASH -317,89 7.688,34\n",
        "08/09 003 EXTRACCION ELEC+CASH -317,89 7.688,34\n1 DE 1 - PAGINA 2 DE 2\n",
    )
    with pytest.raises(ExtractoConsolidadoParseError) as excinfo:
        _parse(text)
    assert "unrecognized" in str(excinfo.value)


def test_a_usd_block_with_a_movement_refuses() -> None:
    text = MULTI_BLOCK.replace(
        "CA U$S 000-444444/4 CAJA DE AHORRO DOLARES FINAL\n"
        "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
        "SALDO ANTERIOR 337,61\n"
        "20/09 SIN MOVIMIENTOS 337,61\n"
        "SALDO AL 20 DE SEPTIEMBRE 337,61\n"
        "TOTAL MOVIMIENTOS -0,00 0,00\n",
        "CA U$S 000-444444/4 CAJA DE AHORRO DOLARES FINAL\n"
        "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
        "SALDO ANTERIOR 337,61\n"
        "20/09 PAGO HABERES 54,37 391,98\n"
        "SALDO AL 20 DE SEPTIEMBRE 391,98\n"
        "TOTAL MOVIMIENTOS -0,00 54,37\n",
    )
    with pytest.raises(ExtractoConsolidadoParseError) as excinfo:
        _parse(text)
    message = str(excinfo.value)
    assert "movement" in message
    assert "54,37" not in message


def test_an_eur_block_with_a_movement_refuses() -> None:
    text = MULTI_BLOCK.replace(
        "CA EUR 000-333333/3 CAJA DE AHORRO EUROS FINAL\n"
        "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
        "SALDO ANTERIOR 214,83\n"
        "20/09 SIN MOVIMIENTOS 214,83\n"
        "SALDO AL 20 DE SEPTIEMBRE 214,83\n"
        "TOTAL MOVIMIENTOS -0,00 0,00\n",
        "CA EUR 000-333333/3 CAJA DE AHORRO EUROS FINAL\n"
        "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
        "SALDO ANTERIOR 214,83\n"
        "20/09 PAGO HABERES 41,92 256,75\n"
        "SALDO AL 20 DE SEPTIEMBRE 256,75\n"
        "TOTAL MOVIMIENTOS -0,00 41,92\n",
    )
    with pytest.raises(ExtractoConsolidadoParseError) as excinfo:
        _parse(text)
    message = str(excinfo.value)
    assert "movement" in message
    assert "41,92" not in message


def test_an_unknown_currency_marker_refuses() -> None:
    text = MULTI_BLOCK.replace(
        "CA U$S 000-444444/4 CAJA DE AHORRO DOLARES FINAL",
        "CA GBP 000-444444/4 CAJA DE AHORRO LIBRAS FINAL",
    )
    with pytest.raises(ExtractoConsolidadoParseError) as excinfo:
        _parse(text)
    assert "unknown currency" in str(excinfo.value)


def test_anchor_more_than_60_days_after_the_close_refuses() -> None:
    with pytest.raises(ExtractoConsolidadoParseError) as excinfo:
        _parse(FULL, anchor=dt.date(2026, 12, 1))
    assert "60 day" in str(excinfo.value)


def test_a_year_override_bypasses_the_60_day_ceiling() -> None:
    extracto = _parse(FULL, anchor=dt.date(2026, 12, 1), year=2026)
    assert extracto.close_date == dt.date(2026, 9, 20)


def test_a_statement_without_purchase_rows_and_without_section_a_parses() -> None:
    """Section (A) is optional: a month without debit purchases (or a loose

    'CUENTA DEBITO'/'TARJETAS DE CREDITO'/'VISA <word>' header area with no
    'VISA DEBITO <last4>' anchor) carries no detail rows at all, and that is
    fine as long as no PAGO CON VISA DEBITO row needs one.
    """
    extracto = _parse(MULTI_BLOCK)
    assert extracto.movements == ()


def test_purchase_rows_without_section_a_refuse_through_the_join() -> None:
    """No section (A) at all, but the table still carries a debit-card purchase
    row: the join has zero candidates, never a missing-section structural error.
    """
    text = MULTI_BLOCK.replace(
        "20/09 SIN MOVIMIENTOS 1.437,29\n"
        "SALDO AL 20 DE SEPTIEMBRE 1.437,29\n"
        "TOTAL MOVIMIENTOS -0,00 0,00\n",
        "20/09 PAGO CON VISA DEBITO -118,47 1.318,82\n"
        "SALDO AL 20 DE SEPTIEMBRE 1.318,82\n"
        "TOTAL MOVIMIENTOS -118,47 0,00\n",
    )
    with pytest.raises(ReconciliationError) as excinfo:
        _parse(text)
    failed = {check.name for check in excinfo.value.failures}
    assert "debit-card-details-matched" in failed


def test_a_visa_debito_header_without_its_cuenta_debito_line_refuses() -> None:
    text = FULL.replace("CUENTA DEBITO NRO $ 000-111111/1\n", "")
    with pytest.raises(ExtractoConsolidadoParseError) as excinfo:
        _parse(text)
    assert "never followed" in str(excinfo.value)


def test_ambiguous_last4_split_refuses() -> None:
    text = FULL.replace(
        "05/09/2026 COMERCIO EJEMPLO 1234 REF PAGO 847261 $ -483,17",
        "05/09/2026 COMERCIO 1234 EJEMPLO 1234 REF PAGO 847261 $ -483,17",
    )
    with pytest.raises(ExtractoConsolidadoParseError) as excinfo:
        _parse(text)
    assert "ambiguous" in str(excinfo.value)
