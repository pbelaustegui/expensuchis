"""Tests for the BBVA Visa/Mastercard card importer wiring (T-07e).

The parser's own tests (``test_bbva_card_parser.py``) prove it reads the format and
refuses a bad document. These prove the *ledger* side: each posting rule posts
where the design says, an installment row collapses to one plan entry (T-06b's
precedent, carried over per the T-07 owner decisions), a payment row emits
nothing, the additional cardholder's rows carry ``holder`` metadata and the
holder's don't, and an unclassified merchant stops the import with the pinned
refusal shape.

Everything is synthetic. No test reads a real statement; ``rows_reader`` (for
``extract``) and ``text_reader`` (for ``identify``) are always injected, so the
default run needs neither ``pypdfium2`` nor any private file. The positioned-row
fixtures are the parser suite's own (``tests/fixtures/bbva_card/*.txt``), loaded
with a small private loader duplicated from ``test_bbva_card_parser.py`` --
private test infrastructure stays private, exactly like the production modules'
own convention of duplicating rather than importing a sibling's private helper.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core.amount import Amount

from expensuchis.counterparties import CounterpartyMap
from expensuchis.importers import bbva, get_importers
from expensuchis.importers.bbva_card import CardLiquidacionParseError, parse_card_liquidacion
from expensuchis.importers.bbva_card_importer import (
    ADDITIONAL_HOLDER_TAG,
    NAME,
    BBVACardImporter,
    CounterpartyClassificationError,
    StatementReadError,
    _merchant_identity,
    build_entries,
)
from expensuchis.importers.bbva_importer import SOURCE, SourceAccountError, derive_person
from expensuchis.importers.pdf import PAGE_SEPARATOR, PositionedRow, PositionedToken

FIXTURES = Path(__file__).parent / "fixtures" / "bbva_card"


def _load(name: str) -> tuple[PositionedRow, ...]:
    """Parse a fixture's custom positioned-row text format (see the parser suite)."""
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
    """Rebuild BBVA-style flat text from positioned rows, for identify tests."""
    pages: dict[int, list[str]] = {}
    for row in rows:
        pages.setdefault(row.page, []).append(" ".join(token.text for token in row.tokens))
    return PAGE_SEPARATOR.join("\n".join(pages[page]) for page in sorted(pages))


VISA_FULL = _load("visa_full.txt")
MASTERCARD_FULL = _load("mastercard_full.txt")

VISA_MAP = {
    "FULANO SERVICIOS": "expense:Expenses:Compras",
    "ZUTANO COMERCIO": "expense:Expenses:Compras",
    "MENGANO KIOSCO": "expense:Expenses:Compras",
    "PLUGH GLOMPH": "expense:Expenses:Compras",
    "QWERTY FARMACIA": "expense:Expenses:Salud",
    "XYZZY LIBRERIA": "expense:Expenses:Educacion",
}

MASTERCARD_MAP = {
    "FULANO ALMACEN": "expense:Expenses:Supermercado",
    "ZUTANO TALLER": "expense:Expenses:Auto",
    "MENGANO FARMACIA": "expense:Expenses:Salud",
}


class _StubMap:
    """A minimal stand-in for ``CounterpartyMap`` that records the lookups."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)
        self.calls: list[tuple[str, str]] = []

    def resolve(self, source: str, raw_name: str) -> str | None:
        self.calls.append((source, raw_name))
        return self._mapping.get(raw_name)


def _entries(rows: tuple[PositionedRow, ...], mapping: dict[str, str], person: str = "P2") -> list:
    liquidacion = parse_card_liquidacion(rows)
    return build_entries(liquidacion, person, _StubMap(mapping))


def _postings(entry) -> list[tuple[str, str, str]]:
    return [
        (posting.account, str(posting.units.number), posting.units.currency)
        for posting in entry.postings
    ]


# ------------------------------------------------------------------------- postings


def test_an_ars_purchase_posts_the_expense_positive_and_the_visa_liability_negative() -> None:
    entries = _entries(VISA_FULL, VISA_MAP)
    entry = next(e for e in entries if e.payee == "FULANO SERVICIOS")
    assert _postings(entry) == [
        ("Expenses:Compras", "234.56", "ARS"),
        ("Liabilities:BBVA:P2:Visa", "-234.56", "ARS"),
    ]
    assert "holder" not in entry.meta


def test_a_usd_purchase_posts_usd_with_no_price_against_the_visa_usd_liability() -> None:
    """Model case (b): the card holds a USD balance, so there is no conversion.

    A mutant that adds an ``@`` price or posts to the ARS liability fails here.
    The payee is ``PLUGH GLOMPH``, not ``PLUGH GLOMPH USD 60,00``: the BBVA-
    specific noise cleanup (T-07e follow-up) drops the inline original-currency
    ``USD 60,00`` restatement from the description, closing what the first cut
    of this importer left as an open question.
    """
    entries = _entries(VISA_FULL, VISA_MAP)
    entry = next(e for e in entries if e.payee == "PLUGH GLOMPH")
    assert _postings(entry) == [
        ("Expenses:Compras", "60.00", "USD"),
        ("Liabilities:BBVA:P2:VisaUSD", "-60.00", "USD"),
    ]
    assert all(posting.price is None for posting in entry.postings)
    assert all(posting.cost is None for posting in entry.postings)


def test_a_mastercard_purchase_posts_against_the_mastercard_liability() -> None:
    entries = _entries(MASTERCARD_FULL, MASTERCARD_MAP)
    entry = next(e for e in entries if e.payee == "FULANO ALMACEN")
    assert _postings(entry) == [
        ("Expenses:Supermercado", "45.30", "ARS"),
        ("Liabilities:BBVA:P2:Mastercard", "-45.30", "ARS"),
    ]


def test_an_installment_row_collapses_to_one_plan_entry() -> None:
    """T-06b's precedent carries over (T-07 owner decisions, 2026-09-27): one
    entry for the whole plan, dated at the row's own date, with the three
    metadata keys ``docs/accounting-model.md`` fixes."""
    entries = _entries(VISA_FULL, VISA_MAP)
    plan_entries = [e for e in entries if e.payee == "ZUTANO COMERCIO"]
    assert len(plan_entries) == 1
    entry = plan_entries[0]
    assert entry.date == dt.date(2026, 3, 6)
    assert _postings(entry) == [
        ("Expenses:Compras", "528.60", "ARS"),
        ("Liabilities:BBVA:P2:Visa", "-528.60", "ARS"),
    ]
    assert entry.meta["installments"] == Decimal(6)
    assert type(entry.meta["installments"]) is Decimal
    assert entry.meta["installment_amount"] == Amount(Decimal("88.10"), "ARS")
    # first_due: the parser never reads VENCIMIENTO (furniture); the close date
    # (28-Mar-26) stands in, minus (installment_number - 1) = 1 month.
    assert entry.meta["first_due"] == "2026-02"
    assert set(entry.meta) == {"key", "installments", "first_due", "installment_amount"}


def test_a_first_installment_plans_first_due_at_the_close_month() -> None:
    entries = _entries(MASTERCARD_FULL, MASTERCARD_MAP)
    plan = next(e for e in entries if e.payee == "ZUTANO TALLER")
    assert plan.meta["first_due"] == "2026-03"
    assert plan.meta["installments"] == Decimal(3)
    assert plan.meta["installment_amount"] == Amount(Decimal("60.00"), "ARS")


def test_the_plan_key_is_stable_and_independent_of_the_row_installment_counter() -> None:
    """A later statement's ``C.03/06`` row of the same plan must key identically."""
    entries = _entries(VISA_FULL, VISA_MAP)
    plan = next(e for e in entries if e.payee == "ZUTANO COMERCIO")
    assert plan.meta["key"] == "bbva-card:P2:visa:2026-03-06:6:ARS:zutano comercio"


def test_a_plan_key_differs_from_a_per_row_purchase_key() -> None:
    entries = _entries(VISA_FULL, VISA_MAP)
    fulano = next(e for e in entries if e.payee == "FULANO SERVICIOS")
    assert fulano.meta["key"] == "bbva-card:P2:visa:2026-03-03:412233"


def test_the_additional_holders_purchases_carry_holder_metadata() -> None:
    """The additional cardholder's rows carry ``holder: "P2"``; the holder's
    own rows carry no ``holder`` key at all (T-07 owner decisions, point 2)."""
    entries = _entries(VISA_FULL, VISA_MAP)
    qwerty = next(e for e in entries if e.payee == "QWERTY FARMACIA")
    xyzzy = next(e for e in entries if e.payee == "XYZZY LIBRERIA")
    assert qwerty.meta["holder"] == ADDITIONAL_HOLDER_TAG == "P2"
    assert xyzzy.meta["holder"] == ADDITIONAL_HOLDER_TAG
    assert _postings(qwerty) == [
        ("Expenses:Salud", "45.00", "ARS"),
        ("Liabilities:BBVA:P2:Visa", "-45.00", "ARS"),
    ]
    holder_rows = [e for e in entries if e.payee in {"FULANO SERVICIOS", "MENGANO KIOSCO"}]
    assert holder_rows
    for entry in holder_rows:
        assert "holder" not in entry.meta


def test_charges_post_per_class_with_no_map_lookup() -> None:
    """``IIBB PERCEP-*`` and ``DB.RG`` are PERCEPCION; ``IVA RG`` is IVA -- both
    resolve with no counterparty lookup, against the ARS Visa liability."""
    stub = _StubMap(VISA_MAP)
    entries = build_entries(parse_card_liquidacion(VISA_FULL), "P2", stub)
    calls_before = len(stub.calls)
    percepciones = [
        e for e in entries if e.payee == SOURCE and "Percepciones" in _postings(e)[0][0]
    ]
    iva = next(e for e in entries if e.payee == SOURCE and "IVA" in _postings(e)[0][0])
    assert len(percepciones) == 2
    assert {_postings(e)[0][1] for e in percepciones} == {"12.40", "3.15"}
    for entry in percepciones:
        assert _postings(entry)[0][0] == "Expenses:Impuestos:Percepciones"
        assert _postings(entry)[1][0] == "Liabilities:BBVA:P2:Visa"
    assert _postings(iva) == [
        ("Expenses:Impuestos:IVA", "22.08", "ARS"),
        ("Liabilities:BBVA:P2:Visa", "-22.08", "ARS"),
    ]
    assert len(stub.calls) == calls_before


def test_a_payment_row_emits_nothing() -> None:
    """``SU PAGO EN PESOS``/``SU PAGO EN USD`` are the settlement, not spending:
    the account extracto owns the cash movement (T-06b precedent, carried over).
    """
    entries = _entries(VISA_FULL, VISA_MAP)
    assert all("pago" not in entry.narration.lower() for entry in entries)
    mastercard_entries = _entries(MASTERCARD_FULL, MASTERCARD_MAP)
    assert len(mastercard_entries) == 3  # 3 purchases (1 a plan); the payment emits nothing


def test_the_visa_fixture_yields_nine_entries() -> None:
    """6 consumption rows (1 collapsed to a plan) + 3 charges; the 2 payments emit nothing."""
    entries = _entries(VISA_FULL, VISA_MAP)
    assert len(entries) == 9


# ------------------------------------------------------------------------- dedup / keys


def test_purchase_keys_are_unique_across_holder_and_additional_holder_sections() -> None:
    entries = _entries(VISA_FULL, VISA_MAP)
    keys = [entry.meta["key"] for entry in entries]
    assert len(keys) == len(set(keys))


def test_purchase_keys_are_unique_across_both_card_fixtures() -> None:
    """Brand is part of the key: a Visa and a Mastercard comprobante never collide."""
    visa_keys = {e.meta["key"] for e in _entries(VISA_FULL, VISA_MAP)}
    mastercard_keys = {e.meta["key"] for e in _entries(MASTERCARD_FULL, MASTERCARD_MAP)}
    assert visa_keys.isdisjoint(mastercard_keys)


def test_charge_keys_carry_the_person_and_brand() -> None:
    entries = _entries(VISA_FULL, VISA_MAP)
    charge_keys = [e.meta["key"] for e in entries if e.payee == SOURCE]
    assert all(key.startswith("bbva-card:P2:visa:") for key in charge_keys)
    assert len(charge_keys) == len(set(charge_keys))


def test_two_persons_charge_keys_never_collide() -> None:
    p1_keys = {
        e.meta["key"]
        for e in build_entries(parse_card_liquidacion(VISA_FULL), "P1", _StubMap(VISA_MAP))
        if e.payee == SOURCE
    }
    p2_keys = {
        e.meta["key"]
        for e in build_entries(parse_card_liquidacion(VISA_FULL), "P2", _StubMap(VISA_MAP))
        if e.payee == SOURCE
    }
    assert p1_keys.isdisjoint(p2_keys)


# --------------------------------------------------------------- merchant identity cleaning
#
# T-07e follow-up (owner decisions, "clean the noise"): the real Visa refused on
# 6 purchase rows the first cut of this importer had not modelled. The masked
# shapes below (digits->9, letters->a) are the parent's own description of the
# refused rows; each test reproduces the *shape* with synthetic placeholder
# words and irregular, non-round digits -- never a real statement token.


def test_shape_a_a_reference_with_hyphen_separators_is_dropped_whole() -> None:
    """``aaaaaaaaaa 9999999999999-999-999``: a word, then a hyphenated reference
    whose digits (separators ignored) run to 13 -- dropped in full."""
    assert _merchant_identity("WOMBAT 4785213-663-091", 1, 1) == "WOMBAT"


def test_shape_b_letters_glued_to_a_long_digit_run_keep_the_letters() -> None:
    """``aaaaaaaaaaaaaa999999999999999``: one token, letters directly followed
    by a 15-digit run -- the letters survive, the digits don't."""
    assert _merchant_identity("SPLORTKRELM483759226104837", 1, 1) == "SPLORTKRELM"


def test_shape_c_a_word_plus_an_alphanumeric_reference_and_an_amount() -> None:
    """``aaaaaaaaa a99999999aaa 9,99``: a word, a letter-digit-letter reference
    (not the pure ``letters-then-digits`` shape of (b), so dropped whole, not
    letter-preserved), and an inline original-currency amount."""
    assert _merchant_identity("WOMBAT REF4527918KX 4,57", 1, 1) == "WOMBAT"


def test_shape_d_a_name_before_an_embedded_asterisk_when_the_after_segment_empties() -> None:
    """``aaaaa*99-99999-99999 aaa 999,99``: the segment after the last ``*`` is a
    hyphenated reference plus a currency-code/amount pair -- once cleaned it
    carries no letter at all, so the segment *before* the ``*`` is used instead."""
    assert _merchant_identity("KRELM*77-45219-30582 USD 719,44", 1, 1) == "KRELM"
    # A second occurrence of the same shape, different digits and currency code.
    assert _merchant_identity("ZOBBIX*4-982175-604 ARS 88,03", 1, 1) == "ZOBBIX"


def test_shape_e_a_dotted_name_survives_a_reference_and_an_amount() -> None:
    """``aaaaaaa.aaa 999999999aaa 99,99``: a dotted merchant name (kept whole --
    the dot is not noise), a digit-then-letters reference (dropped whole, the
    mirror image of (b)), and an amount."""
    assert _merchant_identity("WOMBAT.KRELM 837491052SX 55,18", 1, 1) == "WOMBAT.KRELM"


def test_the_ordinary_after_asterisk_rule_is_unchanged() -> None:
    """A plain ``PROC*Merchant`` row, with nothing to clean, still takes the
    segment after the ``*`` -- the existing rule for the real purchases'
    ``MERCHANTPROC*MERCHANT`` shape is untouched by the new cleaning rules."""
    assert _merchant_identity("*FULANO SERVICIOS", 1, 1) == "FULANO SERVICIOS"
    assert _merchant_identity("PROC*ZUTANO COMERCIO", 1, 1) == "ZUTANO COMERCIO"


def test_a_row_with_no_letters_at_all_still_refuses_as_an_identifier() -> None:
    """No ``*``, and nothing survives the cleanup: a reference and an amount,
    no merchant name anywhere -- still a masked refusal, never a fallback to
    the raw text."""
    with pytest.raises(CardLiquidacionParseError) as excinfo:
        _merchant_identity("4785213-663-091 719,44", 1, 1)
    message = str(excinfo.value)
    assert "identifier" in message
    assert "4785213" not in message and "719,44" not in message


def test_provincia_visa_importer_is_untouched_by_the_bbva_noise_rules() -> None:
    """The owner decision is BBVA-only: Provincia's own normalization must not
    gain the amount/currency-code/long-digit-run rules."""
    from expensuchis.importers.provincia_visa_importer import _merchant_identity as _pv_identity

    # Provincia's own rule has no amount-token or currency-code handling: an
    # inline amount-shaped token is kept exactly as BBVA's used to be, before
    # this follow-up -- pinning that Provincia's file was never touched.
    assert _pv_identity("PLUGH GLOMPH USD 60,00", 1, 1) == "PLUGH GLOMPH USD 60,00"


# ------------------------------------------------------------------------- refusal


def test_an_unclassified_merchant_refuses_and_collects_every_identity() -> None:
    with pytest.raises(CounterpartyClassificationError) as excinfo:
        _entries(VISA_FULL, mapping={})
    message = str(excinfo.value)
    assert "FULANO SERVICIOS" in message
    assert "ZUTANO COMERCIO" in message
    assert "counterparties.tsv" in message
    assert f"{SOURCE}\tFULANO SERVICIOS\texpense:<category>" in message


def test_the_refusal_lists_every_unique_identity_once() -> None:
    with pytest.raises(CounterpartyClassificationError) as excinfo:
        _entries(VISA_FULL, mapping={})
    message = str(excinfo.value)
    assert message.startswith("6 counterparties are not classified.")


def test_the_printed_append_row_classifies_on_a_real_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2's pin, carried over from Provincia's own test: the printed remediation
    actually works, fed back through a **real** ``CounterpartyMap``."""
    from expensuchis.ledger import ENV_VAR

    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))

    with pytest.raises(CounterpartyClassificationError) as excinfo:
        _entries(MASTERCARD_FULL, mapping={})
    line = next(
        line.strip()
        for line in str(excinfo.value).splitlines()
        if line.strip().startswith(f"{SOURCE}\tFULANO ALMACEN")
    )
    source, raw_name, destination = line.split("\t")

    real_map = CounterpartyMap()
    real_map.record(source, raw_name, destination.replace("<category>", "Expenses:Hogar"))
    real_map.record(SOURCE, "ZUTANO TALLER", "expense:Expenses:Auto")
    real_map.record(SOURCE, "MENGANO FARMACIA", "expense:Expenses:Salud")
    entries = build_entries(parse_card_liquidacion(MASTERCARD_FULL), "P2", real_map)
    entry = next(e for e in entries if e.payee == "FULANO ALMACEN")
    assert _postings(entry)[0] == ("Expenses:Hogar", "45.30", "ARS")


# ------------------------------------------------------------------------ identify / paths


def test_identify_claims_a_real_card_statement() -> None:
    importer = BBVACardImporter(text_reader=lambda _p: _flatten(VISA_FULL))
    assert importer.identify("/statements/Bbva/P2/x.pdf") is True
    importer = BBVACardImporter(text_reader=lambda _p: _flatten(MASTERCARD_FULL))
    assert importer.identify("/statements/Bbva/P2/x.pdf") is True


def test_the_card_importer_never_claims_the_account_extracto() -> None:
    account_text = (
        "Extracto consolidado\n"
        "CC $ 000123456789 CUENTA FINAL\n"
        "FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
        "SALDO ANTERIOR 1.000,00\n"
    )
    assert BBVACardImporter(text_reader=lambda _p: account_text).identify("/x.pdf") is False


def test_the_account_importer_never_claims_a_card_statement() -> None:
    from expensuchis.importers.bbva_importer import BBVAImporter

    card_text = _flatten(VISA_FULL)
    assert BBVAImporter(text_reader=lambda _p: card_text).identify("/x.pdf") is False
    assert bbva.is_bbva_extracto(card_text) is False


def test_identify_returns_false_when_extraction_raises() -> None:
    def boom(_path: str) -> str:
        raise OSError("unreadable for the test")

    assert BBVACardImporter(text_reader=boom).identify("/tmp/whatever.pdf") is False


def test_a_path_outside_the_bbva_layout_is_refused_without_echoing_it() -> None:
    path = "/statements/Bbva/statement.pdf"  # missing person folder
    with pytest.raises(SourceAccountError) as excinfo:
        derive_person(path)
    assert path not in str(excinfo.value)


def test_extract_derives_the_person_from_the_path() -> None:
    importer = BBVACardImporter(
        rows_reader=lambda _p: VISA_FULL,
        text_reader=lambda _p: "unused",
        counterparty_map=_StubMap(VISA_MAP),
    )
    entries = importer.extract("/statements/Bbva/P3/x.pdf", [])
    for entry in entries:
        if entry.payee == SOURCE:
            continue
        assert "P3" in _postings(entry)[1][0]  # the liability account names the person


def test_extract_raises_source_account_error_for_a_bad_path() -> None:
    importer = BBVACardImporter(rows_reader=lambda _p: VISA_FULL)
    with pytest.raises(SourceAccountError):
        importer.extract("/statements/Bbva/statement.pdf", [])


# ------------------------------------------------------------------ reader / sort


def test_a_rows_reader_failure_is_wrapped_without_leaking_the_path() -> None:
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> str:
        raise FileNotFoundError(sensitive)

    importer = BBVACardImporter(rows_reader=boom)

    with pytest.raises(StatementReadError) as excinfo:
        importer.extract("/statements/Bbva/P2/statement.pdf", [])

    message = str(excinfo.value)
    for token in (sensitive, "Nombre", "Apellido"):
        assert token not in message
    assert "FileNotFoundError" in message
    assert excinfo.value.__cause__ is None


def test_a_reconciliation_failure_propagates_untouched() -> None:
    """The parser's gate owns a bad document; extract must not mask it."""
    from expensuchis.importers.bbva_card import ReconciliationError

    mutated_rows = tuple(
        PositionedRow(
            page=row.page,
            row=row.row,
            tokens=tuple(
                PositionedToken(t.x0, t.x1, "234,57") if t.text == "234,56" else t
                for t in row.tokens
            ),
        )
        for row in VISA_FULL
    )
    importer = BBVACardImporter(rows_reader=lambda _p: mutated_rows)
    with pytest.raises(ReconciliationError):
        importer.extract("/statements/Bbva/P2/statement.pdf", [])


def test_sort_orders_by_date_then_by_key() -> None:
    entries = _entries(VISA_FULL, VISA_MAP)
    reversed_entries = list(reversed(entries))
    BBVACardImporter().sort(reversed_entries)
    assert [(e.date, e.meta["key"]) for e in reversed_entries] == sorted(
        (e.date, e.meta["key"]) for e in entries
    )


def test_account_returns_a_fixed_archival_visa_account() -> None:
    assert BBVACardImporter().account("/statements/Bbva/P2/x.pdf") == "Liabilities:BBVA:P2:Visa"


# ------------------------------------------------------------------------- registration


def test_the_card_importer_is_registered_without_a_ledger() -> None:
    importers = get_importers()
    names = [importer.name for importer in importers]
    assert names[-1] == NAME == "BBVACard"
    assert isinstance(importers[-1], BBVACardImporter)


def test_the_two_bbva_importers_are_registered_with_distinct_names() -> None:
    importers = get_importers()
    names = [importer.name for importer in importers]
    assert names.count("BBVA") == 1
    assert names.count("BBVACard") == 1
