"""Behaviour tests for the BBVA Visa/Mastercard card ``Liquidación`` parser.

The fixtures under ``tests/fixtures/bbva_card/`` are synthetic, in a custom
"positioned row" text format this file's own loader (:func:`_load`) turns
directly into :class:`~expensuchis.importers.pdf.PositionedRow` values --
**no PDF is ever built or read**, which is what lets the parser stay pure and
the tests stay fast. The format: ``PAGE <n>`` starts a page, ``ROW`` starts a
row, and every other non-blank, non-``#`` line is ``<x0> <x1> <token text>``
-- one token per line. Only an amount token's ``x1`` (and the detail header's
``PESOS``/``DÓLARES`` tokens' ``x1``) is ever read by the parser; every other
token's position is a cosmetic placeholder.

Invented placeholder names (``FULANO``, ``ZUTANO``, ``MENGANO``, and
nonsense merchants borrowed from the account/card siblings' own fixtures --
``QWERTY``, ``XYZZY``, ``PLUGH``), invented comprobantes and deliberately
irregular, non-round amounts throughout -- a plausible Spanish name or a
round figure is exactly what the leak guard exists to catch.

Coverage is two-sided, like the sibling parsers'. Half proves the parser is a
*reader*: the two-section Visa fixture exercises both currencies, dated
payment and charge rows, an installment marker, all three charge shapes, both
payment rows, and a summary box whose early label-only ``SALDO ACTUAL
$``/``SALDO ACTUAL U$S`` pair carries deliberately *wrong* values -- proving
they are furniture the parser never reads, not merely values it happens to
agree with. The Mastercard fixture proves a single consumption section and an
absent charges section both work, and that Mastercard's own ``DÓLARES``
column (always present, always near-zero) never produces a USD movement
because no Mastercard row's amount ever lands in it. The other half proves it
is a *gate*: each failing fixture breaks exactly one reconciliation check
(mirroring ``provincia_visa``'s design), and a handful of structural fixtures
prove the strictness rules -- notably that the reconnaissance's documented
"known unknown" (an undated line carrying a column amount inside a
consumption section) is refused, never guessed at.

Masking is behaviour, not politeness: a test walks every failure and asserts
no diagnostic echoes a merchant, a comprobante or an amount.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
from decimal import Decimal
from pathlib import Path

import pytest

from expensuchis.importers import bbva, provincia_visa
from expensuchis.importers.bbva_card import (
    CHECK_NAMES,
    CardBrand,
    CardLiquidacionParseError,
    ChargeClass,
    Currency,
    MovementKind,
    ReconciliationError,
    is_bbva_card_liquidacion,
    parse_card_liquidacion,
)
from expensuchis.importers.pdf import PAGE_SEPARATOR, PositionedRow, PositionedToken

FIXTURES = Path(__file__).parent / "fixtures" / "bbva_card"


def _load(name: str) -> tuple[PositionedRow, ...]:
    """Parse the fixture's custom text format into :class:`PositionedRow` values.

    Test-only infrastructure: production code never builds a
    :class:`PositionedRow` from text, only :func:`expensuchis.importers.pdf.read_pdf_rows`
    does, from a real PDF.
    """
    rows: list[PositionedRow] = []
    page = 0
    row_number = 0
    tokens: list[PositionedToken] | None = None
    for raw_line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("PAGE "):
            if tokens is not None:
                rows.append(PositionedRow(page=page, row=row_number, tokens=tuple(tokens)))
                tokens = None
            page = int(line.split()[1])
            row_number = 0
            continue
        if line == "ROW":
            if tokens is not None:
                rows.append(PositionedRow(page=page, row=row_number, tokens=tuple(tokens)))
            tokens = []
            row_number += 1
            continue
        assert tokens is not None, f"{name}: a token line appears before any ROW"
        x0_raw, x1_raw, text = line.split(" ", 2)
        tokens.append(PositionedToken(x0=float(x0_raw), x1=float(x1_raw), text=text))
    if tokens is not None:
        rows.append(PositionedRow(page=page, row=row_number, tokens=tuple(tokens)))
    return tuple(rows)


def _flatten(rows: tuple[PositionedRow, ...]) -> str:
    """Rebuild BBVA-style flat text from positioned rows, for identify-predicate tests."""
    pages: dict[int, list[str]] = {}
    for row in rows:
        pages.setdefault(row.page, []).append(" ".join(token.text for token in row.tokens))
    return PAGE_SEPARATOR.join("\n".join(pages[page]) for page in sorted(pages))


VISA_FULL = _load("visa_full.txt")
MASTERCARD_FULL = _load("mastercard_full.txt")
VISA_MERGED_TOKENS = _load("visa_merged_tokens.txt")
VISA_PAGEBREAKS = _load("visa_pagebreaks.txt")

#: Each failing fixture breaks exactly one reconciliation check.
FAILING_FIXTURES: list[tuple[str, str]] = [
    ("fail_section_sum_ars.txt", "section-sums-ars"),
    ("fail_section_sum_usd.txt", "section-sums-usd"),
    ("fail_balance_ars.txt", "balance-ars"),
    ("fail_balance_usd.txt", "balance-usd"),
    ("fail_summary_detail_disagreement.txt", "summary-detail-agreement"),
    ("fail_duplicate_comprobante.txt", "comprobante-unique"),
    ("fail_installment.txt", "installment-valid"),
]

#: Fixtures that refuse the document structurally (CardLiquidacionParseError),
#: never reaching reconciliation.
STRUCTURAL_FIXTURES: list[str] = [
    "fail_unknown_charge_shape.txt",
    "fail_unrecognized_consumption_line.txt",
    "fail_ambiguous_column.txt",
    "fail_no_brand.txt",
    "fail_no_cierre_date.txt",
    "fail_no_charges_header.txt",
    "fail_stray_token_below_margin_cut.txt",
    "fail_page_counter_wrong_shape.txt",
]

#: Tokens a diagnostic must never echo: merchants, comprobantes and amounts
#: from the fixtures.
FORBIDDEN = (
    "FULANO",
    "ZUTANO",
    "MENGANO",
    "QWERTY",
    "XYZZY",
    "PLUGH",
    "412233",
    "412234",
    "412235",
    "412236",
    "412331",
    "412332",
    "512233",
    "512234",
    "512235",
    "234,56",
    "88,10",
    "15,99",
    "60,00",
    "338,65",
    "45,00",
    "12,75",
    "57,75",
    "12,40",
    "3,15",
    "22,08",
    "12.345,67",
    "289,34",
    "12.267,36",
    "304,14",
    "512,34",
    "45,20",
)


# --------------------------------------------------------------------------- good fixtures


def test_visa_reconciles_with_every_check_ok() -> None:
    liquidacion = parse_card_liquidacion(VISA_FULL)
    assert [check.name for check in liquidacion.checks] == list(CHECK_NAMES)
    assert all(check.ok for check in liquidacion.checks), [
        check for check in liquidacion.checks if not check.ok
    ]


def test_mastercard_reconciles_with_every_check_ok() -> None:
    liquidacion = parse_card_liquidacion(MASTERCARD_FULL)
    assert [check.name for check in liquidacion.checks] == list(CHECK_NAMES)
    assert all(check.ok for check in liquidacion.checks), [
        check for check in liquidacion.checks if not check.ok
    ]


def test_visa_with_merged_tokens_reconciles_identically_to_the_split_one() -> None:
    """The real pypdfium2 extraction glues words together with no space
    whenever the gap is too small (T-07d, second real-file correction):
    CIERRE ACTUAL, SU PAGO EN PESOS/USD, TOTAL CONSUMOS DE <name>, and every
    charge row's label+rate+base become single tokens, while SALDO/ANTERIOR,
    Impuestos/cargos/e/intereses and Consumos/<name> stay split -- both
    shapes appear in this one fixture, and parsing must agree with the fully
    split fixture's own figures either way."""
    merged = parse_card_liquidacion(VISA_MERGED_TOKENS)
    split = parse_card_liquidacion(VISA_FULL)
    assert [check.name for check in merged.checks] == list(CHECK_NAMES)
    assert all(check.ok for check in merged.checks), [
        check for check in merged.checks if not check.ok
    ]
    assert merged.close_date == split.close_date
    assert merged.opening_ars == split.opening_ars
    assert merged.opening_usd == split.opening_usd
    assert merged.detail_saldo_actual_ars == split.detail_saldo_actual_ars
    assert merged.detail_saldo_actual_usd == split.detail_saldo_actual_usd
    assert len(merged.movements) == len(split.movements)
    merged_kinds = {m.kind for m in merged.movements}
    assert merged_kinds == {MovementKind.PURCHASE, MovementKind.PAYMENT, MovementKind.CHARGE}
    merged_charge_classes = {
        m.charge_class for m in merged.movements if m.kind is MovementKind.CHARGE
    }
    assert merged_charge_classes == {ChargeClass.PERCEPCION, ChargeClass.IVA}


def test_visa_with_page_breaks_reconciles_identically_to_the_split_one() -> None:
    """T-07d, fourth pass: every real page carries a vertical right-margin
    run and a page/of counter row, and a page break can fall between
    sections (right after the payments) or in the middle of one (mid
    consumption section) -- both appear in this fixture, and parsing must
    still agree with the fixture that has neither."""
    with_breaks = parse_card_liquidacion(VISA_PAGEBREAKS)
    split = parse_card_liquidacion(VISA_FULL)
    assert [check.name for check in with_breaks.checks] == list(CHECK_NAMES)
    assert all(check.ok for check in with_breaks.checks), [
        check for check in with_breaks.checks if not check.ok
    ]
    assert with_breaks.close_date == split.close_date
    assert with_breaks.opening_ars == split.opening_ars
    assert with_breaks.opening_usd == split.opening_usd
    assert with_breaks.detail_saldo_actual_ars == split.detail_saldo_actual_ars
    assert with_breaks.detail_saldo_actual_usd == split.detail_saldo_actual_usd
    assert len(with_breaks.movements) == len(split.movements)
    # No margin character and no page-counter text ever reached a Movement.
    for movement in with_breaks.movements:
        assert movement.description not in {"a", "b", "c", "d", "e", "f", "m", "n", "o", "p"}
        assert "pagina(" not in movement.description


def _with_left_margin_run(rows: tuple[PositionedRow, ...], x0: float, x1: float):
    """Insert a vertical left-margin run (single-character rows) right before
    the charges heading, where the real Visa's third page starts."""
    out: list[PositionedRow] = []
    for row in rows:
        if "".join(token.text for token in row.tokens).lower().startswith("impuestos"):
            for offset in range(8):
                token = PositionedToken(x0=x0, x1=x1, text="q")
                out.append(PositionedRow(page=row.page, row=-1 - offset, tokens=(token,)))
        out.append(row)
    assert len(out) == len(rows) + 8
    return tuple(out)


#: The fixture's own detail-header FECHA x0 (fixture geometry is synthetic;
#: only the edges the parser derives from the header matter).
_FIXTURE_FECHA_X0 = next(
    token.x0 for row in VISA_PAGEBREAKS for token in row.tokens if token.text == "FECHA"
)


def test_a_vertical_left_margin_run_is_page_furniture() -> None:
    """The real cards carry a second vertical run at x≈29-56 on some pages
    (T-07d, fifth pass), left of the detail header's FECHA (x0≈62); on the
    real Visa it sits between the last consumption section and the charges."""
    run = _with_left_margin_run(VISA_PAGEBREAKS, _FIXTURE_FECHA_X0 - 33.0, _FIXTURE_FECHA_X0 - 6.0)
    parsed = parse_card_liquidacion(run)
    assert all(check.ok for check in parsed.checks)
    assert len(parsed.movements) == len(parse_card_liquidacion(VISA_PAGEBREAKS).movements)


def test_a_token_reaching_the_fecha_column_is_not_left_margin_furniture() -> None:
    run = _with_left_margin_run(VISA_PAGEBREAKS, _FIXTURE_FECHA_X0 - 33.0, _FIXTURE_FECHA_X0 - 0.5)
    with pytest.raises(CardLiquidacionParseError, match="unrecognized line"):
        parse_card_liquidacion(run)


def test_a_page_counter_like_row_with_a_different_shape_still_refuses() -> None:
    rows = _load("fail_page_counter_wrong_shape.txt")
    with pytest.raises(CardLiquidacionParseError, match="unrecognized line"):
        parse_card_liquidacion(rows)


def test_a_stray_token_below_the_margin_cut_still_refuses() -> None:
    """A token shaped like margin furniture but positioned *before* the
    derived cut is not dropped, and is not automatically tolerated just
    because it is short -- it must still be a recognized row shape."""
    rows = _load("fail_stray_token_below_margin_cut.txt")
    with pytest.raises(CardLiquidacionParseError, match="unrecognized line"):
        parse_card_liquidacion(rows)


def test_visa_brand_and_close_date_and_balances() -> None:
    liquidacion = parse_card_liquidacion(VISA_FULL)
    assert liquidacion.brand is CardBrand.VISA
    assert liquidacion.close_date == dt.date(2026, 3, 28)
    assert liquidacion.opening_ars == Decimal("12345.67")
    assert liquidacion.opening_usd == Decimal("289.34")
    assert liquidacion.summary_saldo_actual_ars == Decimal("12267.36")
    assert liquidacion.summary_saldo_actual_usd == Decimal("304.14")
    assert liquidacion.detail_saldo_actual_ars == Decimal("12267.36")
    assert liquidacion.detail_saldo_actual_usd == Decimal("304.14")


def test_mastercard_brand_and_near_zero_usd_balances() -> None:
    """Mastercard prints a DÓLARES column throughout (T-07d real-file correction),
    but it is always near-zero and no Mastercard row's amount ever lands in
    it, so every movement still comes out ARS."""
    liquidacion = parse_card_liquidacion(MASTERCARD_FULL)
    assert liquidacion.brand is CardBrand.MASTERCARD
    assert liquidacion.opening_usd == Decimal("0.00")
    assert liquidacion.summary_saldo_actual_usd == Decimal("0.00")
    assert liquidacion.detail_saldo_actual_usd == Decimal("0.00")
    assert all(movement.currency is Currency.ARS for movement in liquidacion.movements)


def test_mastercard_early_split_saldo_actual_rows_are_ignored() -> None:
    """The fixture's early label-only ``SALDO ACTUAL $``/``U$S`` pair carries
    deliberately wrong values; parsing must use the later combined row."""
    liquidacion = parse_card_liquidacion(MASTERCARD_FULL)
    assert liquidacion.summary_saldo_actual_ars != Decimal("5439.10") + Decimal("111.00")
    assert liquidacion.summary_saldo_actual_ars == liquidacion.detail_saldo_actual_ars


def test_visa_early_split_saldo_actual_rows_are_ignored() -> None:
    liquidacion = parse_card_liquidacion(VISA_FULL)
    assert liquidacion.summary_saldo_actual_ars == liquidacion.detail_saldo_actual_ars
    assert liquidacion.summary_saldo_actual_usd == liquidacion.detail_saldo_actual_usd


def test_visa_movements_cover_purchase_payment_and_both_charge_classes() -> None:
    liquidacion = parse_card_liquidacion(VISA_FULL)
    kinds = {movement.kind for movement in liquidacion.movements}
    assert kinds == {MovementKind.PURCHASE, MovementKind.PAYMENT, MovementKind.CHARGE}

    charges = [m for m in liquidacion.movements if m.kind is MovementKind.CHARGE]
    assert {c.charge_class for c in charges} == {ChargeClass.PERCEPCION, ChargeClass.IVA}
    # IIBB PERCEP-* and DB.RG NNNN are both PERCEPCION; IVA RG NNNN is IVA.
    assert sum(1 for c in charges if c.charge_class is ChargeClass.PERCEPCION) == 2
    assert sum(1 for c in charges if c.charge_class is ChargeClass.IVA) == 1
    # Confirmed against the real files (T-07d): charge rows are dated, unlike
    # the first draft's (wrong) assumption.
    assert all(c.when == dt.date(2026, 3, 28) and c.comprobante == "" for c in charges)

    payments = [m for m in liquidacion.movements if m.kind is MovementKind.PAYMENT]
    assert len(payments) == 2
    assert {p.currency for p in payments} == {Currency.ARS, Currency.USD}
    assert all(p.amount < 0 for p in payments)  # kept as printed: a trailing minus is a credit
    # Confirmed against the real files (T-07d): the detail's payment rows are
    # dated, unlike the summary box's own undated restatement (never parsed).
    assert all(p.when == dt.date(2026, 3, 22) for p in payments)

    usd_purchase = next(
        m
        for m in liquidacion.movements
        if m.kind is MovementKind.PURCHASE and m.currency is Currency.USD
    )
    assert usd_purchase.amount == Decimal("60.00")
    assert "USD" in usd_purchase.description  # the inline foreign-amount reference stays in text


def test_visa_installment_marker_is_parsed() -> None:
    liquidacion = parse_card_liquidacion(VISA_FULL)
    installments = [m for m in liquidacion.movements if m.installment_number is not None]
    assert len(installments) == 1
    assert (installments[0].installment_number, installments[0].installment_total) == (2, 6)


def test_visa_second_section_is_flagged_as_the_additional_holder() -> None:
    liquidacion = parse_card_liquidacion(VISA_FULL)
    purchases = [m for m in liquidacion.movements if m.kind is MovementKind.PURCHASE]
    holder = [m for m in purchases if not m.is_additional_holder]
    additional = [m for m in purchases if m.is_additional_holder]
    assert len(holder) == 4  # 3 ARS + 1 USD
    assert len(additional) == 2
    assert {m.comprobante for m in additional} == {"412331", "412332"}
    # Movement carries no holder-name field at all: nothing to echo by
    # construction, since the TOTAL CONSUMOS row's name token is only ever
    # skipped as non-amount-shaped, never captured.
    assert not any(hasattr(m, "holder_name") for m in liquidacion.movements)


def test_mastercard_has_a_single_non_additional_holder_section() -> None:
    liquidacion = parse_card_liquidacion(MASTERCARD_FULL)
    assert all(not m.is_additional_holder for m in liquidacion.movements)


def test_comprobantes_are_kept_verbatim_on_purchases() -> None:
    liquidacion = parse_card_liquidacion(VISA_FULL)
    purchases = [m for m in liquidacion.movements if m.kind is MovementKind.PURCHASE]
    assert {m.comprobante for m in purchases} == {
        "412233",
        "412234",
        "412235",
        "412236",
        "412331",
        "412332",
    }


# --------------------------------------------------------------------------- gate: reconciliation


@pytest.mark.parametrize(
    "name,failing_check", FAILING_FIXTURES, ids=[n for n, _ in FAILING_FIXTURES]
)
def test_each_failing_fixture_breaks_exactly_one_check(name: str, failing_check: str) -> None:
    rows = _load(name)
    with pytest.raises(ReconciliationError) as excinfo:
        parse_card_liquidacion(rows)
    checks = excinfo.value.checks
    assert [check.name for check in checks] == list(CHECK_NAMES)  # every check still reported
    failed_names = {check.name for check in checks if not check.ok}
    assert failed_names == {failing_check}, (name, failed_names)


@pytest.mark.parametrize("name", STRUCTURAL_FIXTURES)
def test_each_structural_fixture_refuses_before_reconciliation(name: str) -> None:
    rows = _load(name)
    with pytest.raises(CardLiquidacionParseError):
        parse_card_liquidacion(rows)


def test_the_known_unknown_wrapped_row_is_refused_not_guessed() -> None:
    """The reconnaissance's documented open question: an undated line inside a
    consumption section, carrying a column amount -- likely a wrapped
    description. This parser refuses rather than inventing a merge rule."""
    rows = _load("fail_unrecognized_consumption_line.txt")
    with pytest.raises(CardLiquidacionParseError, match="unrecognized line"):
        parse_card_liquidacion(rows)


def test_an_amount_between_the_two_columns_is_refused() -> None:
    rows = _load("fail_ambiguous_column.txt")
    with pytest.raises(CardLiquidacionParseError, match="neither derived column edge"):
        parse_card_liquidacion(rows)


def test_cierre_actual_without_a_following_date_row_is_refused() -> None:
    """Confirmed against the real files: CIERRE ACTUAL's date is on the next
    row, never the same one. A missing next-row date is a structural refusal,
    not a same-row fallback."""
    rows = _load("fail_no_cierre_date.txt")
    with pytest.raises(CardLiquidacionParseError, match="CIERRE ACTUAL"):
        parse_card_liquidacion(rows)


def test_charges_section_without_its_own_header_is_refused() -> None:
    rows = _load("fail_no_charges_header.txt")
    with pytest.raises(CardLiquidacionParseError, match="its own detail header"):
        parse_card_liquidacion(rows)


def test_empty_rows_refuse() -> None:
    with pytest.raises(CardLiquidacionParseError, match="empty"):
        parse_card_liquidacion(())


# --------------------------------------------------------------------------- masking


@pytest.mark.parametrize("name,_check", FAILING_FIXTURES, ids=[n for n, _ in FAILING_FIXTURES])
def test_reconciliation_failure_details_never_echo_forbidden_tokens(name: str, _check: str) -> None:
    rows = _load(name)
    with pytest.raises(ReconciliationError) as excinfo:
        parse_card_liquidacion(rows)
    rendered = " ".join(check.detail for check in excinfo.value.checks) + str(excinfo.value)
    for token in FORBIDDEN:
        assert token not in rendered, (name, token)


@pytest.mark.parametrize("name", STRUCTURAL_FIXTURES)
def test_structural_failure_messages_never_echo_forbidden_tokens(name: str) -> None:
    rows = _load(name)
    with pytest.raises(CardLiquidacionParseError) as excinfo:
        parse_card_liquidacion(rows)
    message = str(excinfo.value)
    for token in FORBIDDEN:
        assert token not in message, (name, token)


# --------------------------------------------------------------------------- identify predicate


def test_is_bbva_card_liquidacion_accepts_visa_and_mastercard() -> None:
    assert is_bbva_card_liquidacion(_flatten(VISA_FULL)) is True
    assert is_bbva_card_liquidacion(_flatten(MASTERCARD_FULL)) is True


def test_is_bbva_card_liquidacion_rejects_unrelated_text() -> None:
    assert is_bbva_card_liquidacion("just some prose\nwith no markers at all") is False


def test_bbva_extracto_predicate_rejects_the_card_fixtures() -> None:
    assert bbva.is_bbva_extracto(_flatten(VISA_FULL)) is False
    assert bbva.is_bbva_extracto(_flatten(MASTERCARD_FULL)) is False


def test_provincia_visa_parser_rejects_the_bbva_card_fixtures() -> None:
    with pytest.raises(provincia_visa.LiquidacionParseError):
        provincia_visa.parse_liquidacion(_flatten(VISA_FULL))
    with pytest.raises(provincia_visa.LiquidacionParseError):
        provincia_visa.parse_liquidacion(_flatten(MASTERCARD_FULL))


def test_is_bbva_card_liquidacion_rejects_a_synthetic_extracto_snippet() -> None:
    extracto_snippet = (
        "Extracto consolidado\n"
        "CC $ 000123456789 CUENTA FINAL\n"
        "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
        "SALDO ANTERIOR 1.000,00\n"
    )
    assert is_bbva_card_liquidacion(extracto_snippet) is False


# --------------------------------------------------------------------------- pdfium: read_pdf_rows
#
# The geometry primitive (expensuchis.importers.pdf.read_pdf_rows) is the one
# piece of this unit that a synthetic PositionedRow test cannot exercise --
# only a real pypdfium2 read proves the gap-splitting and y-grouping rules
# against actual character boxes. These tests build a minimal text-layer PDF
# with absolute text-matrix placements (`Tm`), the same opt-in convention the
# account/mercadopago suites already use for the real reader.

_PYPDFIUM2_AVAILABLE = importlib.util.find_spec("pypdfium2") is not None
_PDFIUM_OPT_IN = pytest.mark.skipif(
    not os.environ.get("EXPENSUCHIS_TEST_PDFIUM"),
    reason="opt-in: set EXPENSUCHIS_TEST_PDFIUM=1 to exercise the real reader",
)


def _build_geometry_pdf(placements: list[tuple[float, float, str]]) -> bytes:
    """One page; each ``(x, y, text)`` is drawn at an absolute text matrix position."""
    content = b"BT /F1 12 Tf\n"
    for x, y, text in placements:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content += f"1 0 0 1 {x} {y} Tm ({escaped}) Tj\n".encode()
    content += b"ET"
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: (
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        ),
    }
    out = b"%PDF-1.4\n"
    for object_id in sorted(objects):
        out += f"{object_id} 0 obj ".encode() + objects[object_id] + b"\nendobj\n"
    return out + b"trailer << /Root 1 0 R >>\n%%EOF\n"


@pytest.mark.pdfium
@pytest.mark.skipif(not _PYPDFIUM2_AVAILABLE, reason="pypdfium2 is not installed")
@_PDFIUM_OPT_IN
def test_read_pdf_rows_splits_a_wide_horizontal_gap_into_two_tokens(tmp_path: Path) -> None:
    from expensuchis.importers.pdf import read_pdf_rows

    path = tmp_path / "geometry.pdf"
    # Same y (same row); the second run starts 300pt to the right of the
    # first -- far past the ~9pt "FECHA" run, well over the gap tolerance.
    path.write_bytes(_build_geometry_pdf([(20, 750, "FECHA"), (320, 750, "PESOS")]))

    rows = read_pdf_rows(path)

    assert len(rows) == 1
    assert [token.text for token in rows[0].tokens] == ["FECHA", "PESOS"]
    assert rows[0].tokens[1].x0 > rows[0].tokens[0].x1 + 2.5


@_PDFIUM_OPT_IN
@pytest.mark.pdfium
@pytest.mark.skipif(not _PYPDFIUM2_AVAILABLE, reason="pypdfium2 is not installed")
def test_read_pdf_rows_splits_on_a_literal_space_within_one_run(tmp_path: Path) -> None:
    from expensuchis.importers.pdf import read_pdf_rows

    path = tmp_path / "geometry.pdf"
    path.write_bytes(_build_geometry_pdf([(20, 750, "12.267,36 304,14")]))

    rows = read_pdf_rows(path)

    assert len(rows) == 1
    assert [token.text for token in rows[0].tokens] == ["12.267,36", "304,14"]
    assert rows[0].tokens[0].x1 < rows[0].tokens[1].x0


@_PDFIUM_OPT_IN
@pytest.mark.pdfium
@pytest.mark.skipif(not _PYPDFIUM2_AVAILABLE, reason="pypdfium2 is not installed")
def test_read_pdf_rows_groups_by_row_and_counts_pages(tmp_path: Path) -> None:
    from expensuchis.importers.pdf import read_pdf_rows

    path = tmp_path / "geometry.pdf"
    # Two clearly separate baselines (14pt apart, a normal line height) must
    # yield two rows, each carrying its own token.
    path.write_bytes(_build_geometry_pdf([(20, 750, "FECHA"), (20, 736, "03-Mar-26")]))

    rows = read_pdf_rows(path)

    assert [row.row for row in rows] == [1, 2]
    assert all(row.page == 1 for row in rows)
    assert [token.text for token in rows[0].tokens] == ["FECHA"]
    assert [token.text for token in rows[1].tokens] == ["03-Mar-26"]
