"""Tests for the Banco Provincia card importer wiring (T-06b).

The parser's own tests (``test_provincia_visa_parser.py``) prove it reads the format
and refuses a bad document. These prove the *ledger* side: each posting rule posts
where the design says, an installment row collapses to one entry with the plan as
metadata and a plan key that survives a later statement, a payment row emits
nothing, and an unclassified counterparty stops the import with the pinned refusal
shape.

Everything is synthetic. No test reads a real statement, and the text reader is
always injected, so the default run needs neither ``pypdfium2`` nor any private
file. Cases the fixtures don't carry (a ``first_due`` across a year boundary, the
same plan seen from two statements, an empty merchant identity) are covered with
in-test text literals built in the fixtures' own geometry.
"""

from __future__ import annotations

import traceback
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader
from beancount.core import data
from beancount.core.amount import Amount

from expensuchis.counterparties import CounterpartyMap
from expensuchis.importers import get_importers, provincia_visa
from expensuchis.importers.provincia_importer import (
    SOURCE,
    ProvinciaImporter,
    SourceAccountError,
)
from expensuchis.importers.provincia_visa import LiquidacionParseError, parse_liquidacion
from expensuchis.importers.provincia_visa_importer import (
    CounterpartyClassificationError,
    ProvinciaVisaImporter,
    StatementReadError,
    _redact_cuit,
    build_entries,
    derive_person,
)
from expensuchis.ledger import ENV_VAR
from expensuchis.pipeline import _format_entries

FIXTURES = Path(__file__).parent / "fixtures" / "provincia_visa"

FULL = (FIXTURES / "full_statement.txt").read_text(encoding="utf-8")
MINIMAL = (FIXTURES / "minimal_statement.txt").read_text(encoding="utf-8")
CROSS_YEAR = (FIXTURES / "cross_year_statement.txt").read_text(encoding="utf-8")
EXTRACTO = (FIXTURES.parent / "provincia" / "described_rows.txt").read_text(encoding="utf-8")

#: The fixtures' letterheads carry no title line, so the marker (which lives in the
#: real document's title, not in its data rows) is pinned with literals.
CARD_TITLE = "Banco Provincia Liquidación de Visa\n"

#: A statement closing Enero 26 with a ``C.03/12`` plan: the due month (Febrero 26)
#: minus two installments crosses into the previous calendar year.
YEAR_BOUNDARY = """(0051) Banco Zxqv Qwerty S.A. CUIT 30-99999999-9
IVA: Zxqv Qwerty Plugh Resp. Insc.
Periodo Zxqv CIERRE 28 Enero 26 VENCIMIENTO 02 Febrero 26
Domicilio Zxqv Qwerty SUC: 1234
Cierre Anterior Fec.: 28 Diciembre 25
Cierre Posterior Fec.: 28 Febrero 26
Zxqv Qwerty Plugh Xqvor seis
Zxqv Qwerty Plugh Xqvor siete
Zxqv Qwerty Plugh Xqvor ocho
Limite de Compras: COMPRA $ 137.284,59 Compras en USD $ 4.318,72
SALDO ANTERIOR 9.713,48 104,36
____________________________________________________________
26 Enero 09 400111 Zxqv Qwerty C.03/12 210,13
Totales 0007 Total Consumos de Tarjeta Zxqv 210,13 * 0,00 *
IMPUESTO DE SELLOS $ 0,00
"""

#: A later statement carrying the **next installment of the same plan** parsed from
#: ``FULL``: same purchase date, comprobante, per-installment amount and merchant,
#: one month further into the plan. The plan key must not change.
NEXT_MONTH = """(0051) Banco Zxqv Qwerty S.A. CUIT 30-99999999-9
IVA: Zxqv Qwerty Plugh Resp. Insc.
Periodo Zxqv CIERRE 28 Abril 26 VENCIMIENTO 02 Mayo 26
Domicilio Zxqv Qwerty SUC: 1234
Cierre Anterior Fec.: 28 Marzo 26
Cierre Posterior Fec.: 28 Mayo 26
Zxqv Qwerty Plugh Xqvor seis
Zxqv Qwerty Plugh Xqvor siete
Zxqv Qwerty Plugh Xqvor ocho
Limite de Compras: COMPRA $ 137.284,59 Compras en USD $ 4.318,72
SALDO ANTERIOR 4.618,73 53,17
____________________________________________________________
26 Marzo 14 400234 PAGOAPP* Zxqv Recarga C.04/12 200,34
Totales 0007 Total Consumos de Tarjeta Zxqv 200,34 * 0,00 *
IMPUESTO DE SELLOS $ 0,00
"""

#: Two unclassified merchants, one described with a CUIT-shaped 11-digit run.
UNCLASSIFIED = """(0051) Banco Zxqv Qwerty S.A. CUIT 30-99999999-9
IVA: Zxqv Qwerty Plugh Resp. Insc.
Periodo Zxqv CIERRE 28 Marzo 26 VENCIMIENTO 02 Abril 26
Domicilio Zxqv Qwerty SUC: 1234
Cierre Anterior Fec.: 28 Febrero 26
Cierre Posterior Fec.: 28 Abril 26
Zxqv Qwerty Plugh Xqvor seis
Zxqv Qwerty Plugh Xqvor siete
Zxqv Qwerty Plugh Xqvor ocho
Limite de Compras: COMPRA $ 137.284,59 Compras en USD $ 4.318,72
SALDO ANTERIOR 9.713,48 104,36
____________________________________________________________
26 Marzo 08 400111 Zxqv Qwerty 42,13
26 Marzo 09 400222 Plugh 20345678901 7,77
Totales 0007 Total Consumos de Tarjeta Zxqv 49,90 * 0,00 *
IMPUESTO DE SELLOS $ 0,00
"""

#: A charge row whose only content is the comprobante and the amount: no merchant.
EMPTY_IDENTITY = """(0051) Banco Zxqv Qwerty S.A. CUIT 30-99999999-9
IVA: Zxqv Qwerty Plugh Resp. Insc.
Periodo Zxqv CIERRE 28 Marzo 26 VENCIMIENTO 02 Abril 26
Domicilio Zxqv Qwerty SUC: 1234
Cierre Anterior Fec.: 28 Febrero 26
Cierre Posterior Fec.: 28 Abril 26
Zxqv Qwerty Plugh Xqvor seis
Zxqv Qwerty Plugh Xqvor siete
Zxqv Qwerty Plugh Xqvor ocho
Limite de Compras: COMPRA $ 137.284,59 Compras en USD $ 4.318,72
SALDO ANTERIOR 9.713,48 104,36
____________________________________________________________
26 Marzo 08 400111 42,13
Totales 0007 Total Consumos de Tarjeta Zxqv 42,13 * 0,00 *
IMPUESTO DE SELLOS $ 0,00
"""

#: The payment block with no charges at all: the sums reconcile at zero, and the
#: importer must derive no entry from the payment or balance rows. Row A is the
#: credit (trailing minus, as printed); row B is the balance row — label, previous
#: ARS, ``TC`` rate with three decimals, resulting ARS and USD with trailing
#: minuses — and the chain closes: 9.713,48 − 800,13 = 8.913,35, and the USD
#: balance restates unchanged at 100,00 because the credit carried no USD amount.
PAYMENTS_ONLY = """(0051) Banco Zxqv Qwerty S.A. CUIT 30-99999999-9
IVA: Zxqv Qwerty Plugh Resp. Insc.
Periodo Zxqv CIERRE 28 Marzo 26 VENCIMIENTO 02 Abril 26
Domicilio Zxqv Qwerty SUC: 1234
Cierre Anterior Fec.: 28 Febrero 26
Cierre Posterior Fec.: 28 Abril 26
Zxqv Qwerty Plugh Xqvor seis
Zxqv Qwerty Plugh Xqvor siete
Zxqv Qwerty Plugh Xqvor ocho
Limite de Compras: COMPRA $ 137.284,59 Compras en USD $ 4.318,72
SALDO ANTERIOR 9.713,48 104,36
26 Marzo 25 SU PAGO EN PESOS 800,13-
25 SU PAGO EN PESOS 9.713,48 TC1.455,987 8.913,35- 104,36-
____________________________________________________________
Totales 0007 Total Consumos de Tarjeta Zxqv 0,00 * 0,00 *
IMPUESTO DE SELLOS $ 0,00
IMPUESTO DE SELLOS USD 0,00
"""


class _StubMap:
    """A minimal stand-in for ``CounterpartyMap`` that records the lookups."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)
        self.calls: list[tuple[str, str]] = []

    def resolve(self, source: str, raw_name: str) -> str | None:
        self.calls.append((source, raw_name))
        return self._mapping.get(raw_name)


FULL_MAP = {
    # Printed identities: casing and punctuation kept, exactly what
    # ``_merchant_identity`` looks up — the extracto's own style, so one
    # ``counterparties.tsv`` row serves both Provincia products.
    "Zxqv Plugh": "expense:Expenses:Hogar",
    "Qwerty Plugh Uno": "expense:Expenses:Zxqv",
    "Plugh": "expense:Expenses:Compras",
    "Qwerty Cine Plugh": "expense:Expenses:Ocio",
    "Zxqv Qwerty Plugh": "expense:Expenses:Supermercado",
    "Zxqv Recarga": "expense:Expenses:Recargas",
    # Identities the cross-year and year-boundary statements carry.
    "Zxqv Qwerty": "expense:Expenses:Supermercado",
    "Qwerty Cine": "expense:Expenses:Ocio",
}


def _entries(text: str, mapping: dict[str, str] | None = None, person: str = "P2"):
    liquidacion = parse_liquidacion(text)
    return build_entries(liquidacion, person, _StubMap(FULL_MAP if mapping is None else mapping))


def _postings(entry) -> list[tuple[str, str, str]]:
    return [
        (posting.account, str(posting.units.number), posting.units.currency)
        for posting in entry.postings
    ]


# ------------------------------------------------------------------------- postings


def test_an_ars_charge_posts_the_expense_positive_and_the_liability_negative() -> None:
    entries = _entries(FULL)
    entry = next(e for e in entries if e.payee == "Zxqv Qwerty Plugh")
    assert _postings(entry) == [
        ("Expenses:Supermercado", "1000.12", "ARS"),
        ("Liabilities:Provincia:P2:Visa", "-1000.12", "ARS"),
    ]
    # The payee is the printed (noise- and CUIT-stripped) identity; the narration
    # is verbatim.
    assert entry.payee == "Zxqv Qwerty Plugh"
    assert entry.narration == "Zxqv Qwerty Plugh"


def test_a_usd_charge_posts_usd_with_no_price_against_the_usd_liability() -> None:
    """Model case (b): the card holds a USD balance, so there is no conversion.

    Deriving a price from a market rate is what the accounting model forbids; a
    mutant that adds an ``@`` price or posts to the ARS liability fails here.
    """
    entries = _entries(FULL)
    entry = next(e for e in entries if e.payee == "Qwerty Cine Plugh")
    assert _postings(entry) == [
        ("Expenses:Ocio", "512.34", "USD"),
        ("Liabilities:Provincia:P2:VisaUSD", "-512.34", "USD"),
    ]
    assert all(posting.price is None for posting in entry.postings)
    assert all(posting.cost is None for posting in entry.postings)


def test_an_installment_row_collapses_to_one_entry_for_the_whole_plan() -> None:
    """The ``C.03/12`` row is one entry for 12 × the per-installment amount.

    The three metadata keys are the model's contract, and the amount carries its
    currency as an ``Amount``, never a bare number.
    """
    entries = _entries(FULL)
    plan_entries = [e for e in entries if e.payee == "Zxqv Recarga"]
    assert len(plan_entries) == 1
    entry = plan_entries[0]
    assert _postings(entry) == [
        ("Expenses:Recargas", "2404.08", "ARS"),
        ("Liabilities:Provincia:P2:Visa", "-2404.08", "ARS"),
    ]
    assert entry.meta["installments"] == Decimal(12)
    assert type(entry.meta["installments"]) is Decimal
    assert entry.meta["first_due"] == "2026-02"
    assert entry.meta["installment_amount"] == Amount(Decimal("200.34"), "ARS")
    assert set(entry.meta) == {"key", "installments", "first_due", "installment_amount"}


def test_first_due_crosses_a_year_boundary() -> None:
    """Due Febrero 26 minus two installments lands in Diciembre 25."""
    entries = _entries(YEAR_BOUNDARY)
    plan = next(e for e in entries if "installments" in e.meta)
    assert plan.meta["first_due"] == "2025-12"


def test_a_plan_bought_in_a_previous_year_keeps_its_purchase_date_and_key() -> None:
    """The cross-year fixture's ``C.09/12`` row is dated 2025 on a 2026 statement.

    The purchase date (not the statement's year) enters the plan key, and the
    first due month comes from the statement's VENCIMIENTO (Septiembre 26) minus
    eight installments: a Noviembre 25 purchase is the ninth billing on a
    statement closing Agosto 26 — the distance is ``NN - 1`` months.
    """
    entries = _entries(CROSS_YEAR)
    plan = [e for e in entries if "installments" in e.meta]
    assert len(plan) == 1
    assert plan[0].date.isoformat() == "2025-11-12"
    assert plan[0].meta["first_due"] == "2026-01"
    assert plan[0].meta["key"] == "provincia-visa:P2:2025-11-12:12:ARS:zxqv qwerty"


def test_the_plan_key_is_identical_for_the_same_plan_in_two_statements() -> None:
    """A plan repeated on a later statement must produce the key the ledger has.

    The later statement's row carries the next installment number (``C.04/12``)
    and the document's own due month differs; neither may enter the key, or the
    pipeline would charge the plan a second time instead of dropping the repeat.
    """
    first = _entries(FULL)
    second = _entries(NEXT_MONTH)
    first_key = next(e for e in first if e.payee == "Zxqv Recarga").meta["key"]
    second_key = next(e for e in second if e.payee == "Zxqv Recarga").meta["key"]
    assert first_key == second_key
    assert first_key == "provincia-visa:P2:2026-03-14:12:ARS:zxqv recarga"


def test_a_plan_key_differs_from_a_per_row_key() -> None:
    entries = _entries(FULL)
    keys = [entry.meta["key"] for entry in entries if entry.payee != SOURCE]
    plan_keys = [key for key in keys if key.startswith("provincia-visa:")]
    row_keys = [key for key in keys if not key.startswith("provincia-visa:")]
    assert len(plan_keys) == 3  # the three installment rows of the full fixture
    assert row_keys == [
        "2026-02-05:400345:300.56",
        "2026-02-27:400456:512.34",
        "2026-03-03:400123:1000.12",
    ]

def test_surcharges_post_per_kind_and_per_currency_with_no_map_lookup() -> None:
    """``SELLOS`` and ``PERCEPCION`` are taxes on the card's own operation.

    They resolve with no counterparty lookup and post against the liability of
    their own currency: ARS against ``Visa``, USD against ``VisaUSD``.
    """
    stub = _StubMap(FULL_MAP)
    entries = build_entries(parse_liquidacion(FULL), "P2", stub)
    map_calls_before = len(stub.calls)
    sellos_ars = next(
        e for e in entries if e.narration.endswith("SELLOS") and _postings(e)[0][2] == "ARS"
    )
    sellos_usd = next(
        e for e in entries if e.narration.endswith("SELLOS") and _postings(e)[0][2] == "USD"
    )
    percepcion = next(e for e in entries if e.narration.startswith("IIBB"))
    assert _postings(sellos_ars) == [
        ("Expenses:Impuestos:Sellos", "12.34", "ARS"),
        ("Liabilities:Provincia:P2:Visa", "-12.34", "ARS"),
    ]
    assert _postings(sellos_usd) == [
        ("Expenses:Impuestos:Sellos", "3.45", "USD"),
        ("Liabilities:Provincia:P2:VisaUSD", "-3.45", "USD"),
    ]
    assert _postings(percepcion) == [
        ("Expenses:Impuestos:Percepciones", "75.00", "ARS"),
        ("Liabilities:Provincia:P2:Visa", "-75.00", "ARS"),
    ]
    assert sellos_ars.payee == SOURCE == "Provincia"
    assert percepcion.payee == SOURCE
    # The surcharge rows never resolved through the map.
    assert len(stub.calls) == map_calls_before


def test_surcharge_keys_are_unique_inside_a_batch_and_suffixed_on_repeats() -> None:
    """Two identical SELLOS rows in one document: the second carries ``#2``.

    The key carries the currency, so an ARS row and a USD row of the same kind
    and amount do **not** collide — a zero-amount SELLOS pair appears in every
    document and must never force a suffix; only a genuine repeat is suffixed.
    """
    duplicated = FULL.replace(
        "IMPUESTO DE SELLOS $ 12,34",
        "IMPUESTO DE SELLOS $ 12,34\nIMPUESTO DE SELLOS $ 12,34",
        1,
    )
    entries = _entries(duplicated)
    keys = [entry.meta["key"] for entry in entries if "sellos" in entry.meta["key"]]
    assert keys == [
        "provincia-visa:P2:2026-03-01:sellos:12.34:ARS",
        "provincia-visa:P2:2026-03-01:sellos:12.34:ARS#2",
        "provincia-visa:P2:2026-03-01:sellos:3.45:USD",
    ]


def test_zero_ars_and_zero_usd_sellos_rows_get_distinct_keys_without_suffix() -> None:
    """The minimal fixture's ARS and USD ``0,00`` SELLOS rows must not collide.

    Before the currency joined the key, this pair forced a ``#2`` suffix inside a
    single document — a mutation that would make a repeated import of the same
    document drop a row the first import had kept.
    """
    entries = _entries(MINIMAL)
    keys = [entry.meta["key"] for entry in entries if entry.payee == SOURCE]
    assert keys == [
        "provincia-visa:P2:2026-03-01:sellos:0.00:ARS",
        "provincia-visa:P2:2026-03-01:sellos:0.00:USD",
    ]


def test_surcharge_keys_carry_the_person_and_repeats_stay_suffixed() -> None:
    """R4-1: the person is part of the surcharge key, as in the plan key.

    Two persons' surcharges that share closing month, kind, amount and currency
    must never share a key — a ledger dedup by key would silently drop the second
    person's row — while two identical rows inside one document keep their ``#N``
    suffixes instead of colliding.
    """
    keys_by_person = {
        person: sorted(
            entry.meta["key"]
            for entry in build_entries(parse_liquidacion(MINIMAL), person, _StubMap(FULL_MAP))
            if entry.payee == SOURCE
        )
        for person in ("P1", "P2")
    }
    assert keys_by_person["P1"] == [
        "provincia-visa:P1:2026-03-01:sellos:0.00:ARS",
        "provincia-visa:P1:2026-03-01:sellos:0.00:USD",
    ]
    assert keys_by_person["P2"] == [
        "provincia-visa:P2:2026-03-01:sellos:0.00:ARS",
        "provincia-visa:P2:2026-03-01:sellos:0.00:USD",
    ]
    # Within one document, a genuine repeat suffixes instead of colliding.
    duplicated = FULL.replace(
        "IMPUESTO DE SELLOS $ 12,34",
        "IMPUESTO DE SELLOS $ 12,34\nIMPUESTO DE SELLOS $ 12,34",
        1,
    )
    repeat_keys = [e.meta["key"] for e in _entries(duplicated) if "sellos" in e.meta["key"]]
    assert len(repeat_keys) == len(set(repeat_keys)) == 3


def test_a_payment_row_emits_nothing() -> None:
    """``SU PAGO EN PESOS`` is the settlement, not a consumption.

    The account extracto's ``pago visa`` already posts it against the same
    liability; an entry here would reduce the liability twice. The balance row is
    a restatement of the block, never a posting. A statement carrying only a
    payment and its balance row, with the (zero) totals, produces no entry from
    the payment block.
    """
    entries = _entries(PAYMENTS_ONLY)
    assert len(entries) == 2  # the two SELLOS rows, nothing else
    assert all("pago" not in entry.narration.lower() for entry in entries)
    assert all(entry.payee == SOURCE for entry in entries)


def test_the_full_fixture_yields_nine_entries_in_document_order() -> None:
    """Six charges (three collapsed to plans) plus three surcharges, sorted."""
    entries = _entries(FULL)
    assert len(entries) == 9
    assert [(entry.date.isoformat(), entry.meta["key"]) for entry in entries] == [
        ("2026-01-09", "provincia-visa:P2:2026-01-09:6:ARS:zxqv plugh"),
        ("2026-01-21", "provincia-visa:P2:2026-01-21:3:ARS:qwerty plugh uno"),
        ("2026-02-05", "2026-02-05:400345:300.56"),
        ("2026-02-27", "2026-02-27:400456:512.34"),
        ("2026-03-01", "provincia-visa:P2:2026-03-01:sellos:12.34:ARS"),
        ("2026-03-01", "provincia-visa:P2:2026-03-01:sellos:3.45:USD"),
        ("2026-03-03", "2026-03-03:400123:1000.12"),
        ("2026-03-14", "provincia-visa:P2:2026-03-14:12:ARS:zxqv recarga"),
        ("2026-03-27", "provincia-visa:P2:2026-03-27:percepcion:75.00:ARS"),
    ]


def test_processor_prefixes_and_noise_tokens_leave_one_map_key_per_merchant() -> None:
    """``PAGOAPP*``, ``SHOPONLINE*`` and digit noise never split a merchant."""
    stub = _StubMap(FULL_MAP)
    build_entries(parse_liquidacion(FULL), "P2", stub)
    assert set(stub.calls) == {
        (SOURCE, "Zxqv Plugh"),
        (SOURCE, "Qwerty Plugh Uno"),
        (SOURCE, "Plugh"),
        (SOURCE, "Qwerty Cine Plugh"),
        (SOURCE, "Zxqv Qwerty Plugh"),
        (SOURCE, "Zxqv Recarga"),
    }
    assert stub.calls.count((SOURCE, "Plugh")) == 1
    assert stub.calls.count((SOURCE, "Zxqv Recarga")) == 1


# ------------------------------------------------------------------------- refusal


def test_the_unclassified_refusal_lists_every_identity_and_the_append_line() -> None:
    """Full message equality: one row per unique identity, with the pinned suffix."""
    with pytest.raises(CounterpartyClassificationError) as excinfo:
        _entries(UNCLASSIFIED, mapping={})
    assert str(excinfo.value) == (
        "2 counterparties are not classified.\n"
        '  2026-03-08  42.13 ARS  "Zxqv Qwerty"  "Zxqv Qwerty"\n'
        "    append to counterparties.tsv:\n"
        "      Provincia\tZxqv Qwerty\texpense:<category>\n"
        '  2026-03-09  7.77 ARS  "Plugh <cuit>"  "Plugh"\n'
        "    append to counterparties.tsv:\n"
        "      Provincia\tPlugh\texpense:<category>"
    )


def test_the_refusal_redacts_a_cuit_shaped_run() -> None:
    """An 11-digit run in the verbatim description never reaches the CLI message."""
    with pytest.raises(CounterpartyClassificationError) as excinfo:
        _entries(UNCLASSIFIED, mapping={})
    message = str(excinfo.value)
    assert "20345678901" not in message
    assert "<cuit>" in message
    # The identity itself (the map key) stays readable and CUIT-free.
    assert "Provincia\tPlugh\texpense:<category>" in message


def test_an_empty_merchant_identity_refuses_loudly() -> None:
    """A charge with no identity has no one to classify; the import refuses."""
    with pytest.raises(provincia_visa.LiquidacionParseError) as excinfo:
        _entries(EMPTY_IDENTITY, mapping={})
    message = str(excinfo.value)
    assert "merchant identity" in message
    # Masked: page and line only, never the comprobante or the amount.
    assert "400111" not in message
    assert "42,13" not in message and "42.13" not in message


def test_an_installment_entry_survives_the_pipeline_renderer() -> None:
    """A plan entry renders through the pipeline's formatter and round-trips.

    ``installments`` must be a ``Decimal``: the pipeline renders every kept entry
    with beancount's own printer outside any ``try``, and that printer rejects a
    bare ``int`` in metadata — with one, the real import breaks for every
    installment plan while plan-less statements sail through. Render, reload with
    beancount's own loader, and assert the plan metadata survives intact.
    """
    plan = next(e for e in _entries(FULL) if e.meta.get("installments"))
    rendered = _format_entries([plan]).decode("utf-8")
    # The accounts must be open for the loader's validation to run clean, exactly
    # as in a real ledger; the transactions are dated after the opens.
    opens = "\n".join(
        f"2026-01-01 open {account}" for account, _number, _currency in _postings(plan)
    )
    parsed, errors, _options = loader.load_string(opens + "\n" + rendered)
    assert not errors
    assert len([e for e in parsed if isinstance(e, data.Transaction)]) == 1
    entry = next(e for e in parsed if e.meta.get("installments"))
    assert entry.meta["installments"] == Decimal(6)
    # The type itself is load-bearing: a bare int compares equal by value but the
    # printer rejects it, so pin the class, not just the value.
    assert type(entry.meta["installments"]) is Decimal
    assert "installments: 6" in rendered
    assert entry.meta["first_due"] == "2026-03"
    assert entry.meta["installment_amount"] == Amount(Decimal("150.78"), "ARS")
    assert entry.meta["key"] == "provincia-visa:P2:2026-01-09:6:ARS:zxqv plugh"


def test_two_different_plans_sharing_the_key_fields_collapse_to_one_key() -> None:
    """N9's accepted cost, pinned as deliberate: the key carries no amount.

    Two plans that share person, purchase date, total, currency and canonical
    merchant produce the same key (their per-installment amounts differ): inside
    one batch the pipeline refuses loudly with ``KEY_DUPLICATE``; on a later
    statement the second plan is dropped silently. The parsed data carries no
    stable plan identifier, so the collapse is the price of not splitting a plan
    on a cent of rounding.
    """
    statement = _plan_statement("Zxqv Recarga").replace(
        "26 Marzo 14 400234 Zxqv Recarga C.03/12 200,34",
        "26 Marzo 14 400234 Zxqv Recarga C.03/12 200,34\n"
        "26 Marzo 14 400999 Zxqv Recarga C.03/12 111,11",
        1,
    ).replace("200,34 * 0,00", "311,45 * 0,00", 1)
    entries = _entries(statement)
    plan_keys = [e.meta["key"] for e in entries if "installments" in e.meta]
    assert len(plan_keys) == 2
    assert plan_keys[0] == plan_keys[1] == "provincia-visa:P2:2026-03-14:12:ARS:zxqv recarga"


def test_a_plan_key_survives_casing_and_attached_punctuation_drift() -> None:
    """Casing and attached punctuation never split one merchant into two plan keys.

    The bank's own text drifts between statements; the canonical identity inside
    the plan key must not drift with it — even though the map key and the payee
    (the printed identities below) differ between the variants.
    """
    mapping = {
        **FULL_MAP,
        "ZXQV RECARGA": "expense:Expenses:Recargas",
        "zxqv recarga.": "expense:Expenses:Recargas",
        "Zxqv-Recarga": "expense:Expenses:Recargas",
    }
    for description in (
        "PAGOAPP* Zxqv Recarga",
        "PAGOAPP* ZXQV RECARGA",
        "PAGOAPP* zxqv recarga.",
        "PAGOAPP* Zxqv-Recarga",
    ):
        entries = _entries(_plan_statement(description), mapping=mapping)
        plan = next(e for e in entries if "installments" in e.meta)
        assert plan.meta["key"] == "provincia-visa:P2:2026-03-14:12:ARS:zxqv recarga"


def test_an_extra_word_between_months_still_splits_the_plan_key() -> None:
    """The documented residual: a suffix the canonical form cannot know splits.

    ``S.A.`` and ``Nro 4321`` carry real extra tokens, so their keys differ from
    the plain merchant's. This is pinned as deliberate, not as an accident: the
    alternative (guessing which words are suffixes) would silently merge two
    different merchants into one.
    """
    keys = {
        next(
            e
            for e in _entries(
                _plan_statement(description),
                mapping={
                    **FULL_MAP,
                    "Zxqv Recarga S.A.": "expense:Expenses:Recargas",
                    "Zxqv Recarga Nro": "expense:Expenses:Recargas",
                },
            )
            if "installments" in e.meta
        ).meta["key"]
        for description in ("PAGOAPP* Zxqv Recarga S.A.", "PAGOAPP* Zxqv Recarga Nro 4321")
    }
    assert keys == {
        "provincia-visa:P2:2026-03-14:12:ARS:zxqv recarga s a",
        "provincia-visa:P2:2026-03-14:12:ARS:zxqv recarga nro",
    }


def _plan_statement(description: str) -> str:
    """A one-charge ``C.03/12`` statement whose description is the drift under test."""
    return f"""(0051) Banco Zxqv Qwerty S.A. CUIT 30-99999999-9
IVA: Zxqv Qwerty Plugh Resp. Insc.
Periodo Zxqv CIERRE 28 Marzo 26 VENCIMIENTO 02 Abril 26
Domicilio Zxqv Qwerty SUC: 1234
Cierre Anterior Fec.: 28 Febrero 26
Cierre Posterior Fec.: 28 Abril 26
Zxqv Qwerty Plugh Xqvor seis
Zxqv Qwerty Plugh Xqvor siete
Zxqv Qwerty Plugh Xqvor ocho
Limite de Compras: COMPRA $ 137.284,59 Compras en USD $ 4.318,72
SALDO ANTERIOR 9.713,48 104,36
____________________________________________________________
26 Marzo 14 400234 {description} C.03/12 200,34
Totales 0007 Total Consumos de Tarjeta Zxqv 200,34 * 0,00 *
IMPUESTO DE SELLOS $ 0,00
"""


def _cuit_probe_statement(description: str) -> str:
    """A one-charge statement whose merchant description carries the run under test."""
    return f"""(0051) Banco Zxqv Qwerty S.A. CUIT 30-99999999-9
IVA: Zxqv Qwerty Plugh Resp. Insc.
Periodo Zxqv CIERRE 28 Marzo 26 VENCIMIENTO 02 Abril 26
Domicilio Zxqv Qwerty SUC: 1234
Cierre Anterior Fec.: 28 Febrero 26
Cierre Posterior Fec.: 28 Abril 26
Zxqv Qwerty Plugh Xqvor seis
Zxqv Qwerty Plugh Xqvor siete
Zxqv Qwerty Plugh Xqvor ocho
Limite de Compras: COMPRA $ 137.284,59 Compras en USD $ 4.318,72
SALDO ANTERIOR 9.713,48 104,36
____________________________________________________________
26 Marzo 08 400111 {description} 42,13
Totales 0007 Total Consumos de Tarjeta Zxqv 42,13 * 0,00 *
IMPUESTO DE SELLOS $ 0,00
"""


@pytest.mark.parametrize(
    ("description", "printed_identity"),
    [
        ("Zxqv Qwerty", "Zxqv Qwerty"),  # the plain shape, for the baseline
        ("Plugh 20-12345678-9", "Plugh"),  # the hyphenated form: dropped from the identity
        ("Zxqv 20.12345678.9", "Zxqv"),  # the dotted form: dropped from the identity
    ],
)
def test_the_refusal_redacts_a_cuit_shaped_run_and_prints_a_usable_identity(
    description: str, printed_identity: str
) -> None:
    """Every interpolated field of the refusal passes through the redactor.

    The CUIT-shaped runs are dropped from the identity at the noise-token stage (on
    a card description the run is the merchant's tax id — per-row noise), so the
    identity the refusal prints is already identifier-free; the redaction on the
    displayed fields (description and identity, both occurrences) is belt. The
    append row is exactly the lookup key.
    """
    with pytest.raises(CounterpartyClassificationError) as excinfo:
        _entries(_cuit_probe_statement(description), mapping={})
    message = str(excinfo.value)
    assert "20345678901" not in message
    assert "12345678" not in message
    assert "20-12345678-9" not in message
    assert "20.12345678.9" not in message
    assert f'"{_redact_cuit(description)}"  "{printed_identity}"' in message
    assert f"{SOURCE}\t{printed_identity}\texpense:<category>" in message


@pytest.mark.parametrize(
    "description",
    ["20345678901", "X20345678901q"],
    ids=["identifier-only", "cuit-glued-inside-name"],
)
def test_an_identity_that_is_an_identifier_refuses_loudly(description: str) -> None:
    """A row whose identity is an identifier, not a merchant name, refuses.

    The raw-description fallback would put the CUIT back into the key and print an
    append row the lookup can never match, so the parser refuses instead: masked
    message, page and line only, no identity echoed, and a pointer at the
    deliberate code decision needed to accept such a row.
    """
    with pytest.raises(LiquidacionParseError) as excinfo:
        _entries(_cuit_probe_statement(description), mapping={})
    message = str(excinfo.value)
    assert "identifier" in message and "deliberate code decision" in message
    assert "page" in message and "line" in message
    assert "20345678901" not in message and "12345678" not in message


def test_the_printed_append_row_classifies_on_a_real_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2's pin: the printed remediation actually works.

    The append row the refusal advertises is written to a **real**
    ``CounterpartyMap`` (a temporary ledger directory through the environment
    seam), and ``build_entries`` is re-run over the same statement **through that
    real map**: the row must classify — no refusal, the entry posted. This is the
    assertion that dies if the printed identity ever diverges from the lookup key
    again.
    """
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))

    with pytest.raises(CounterpartyClassificationError) as excinfo:
        _entries(_cuit_probe_statement("Zxqv 20.12345678.9"), mapping={})
    append_lines = [
        line.strip()
        for line in str(excinfo.value).splitlines()
        if line.strip().startswith(f"{SOURCE}\t")
    ]
    assert len(append_lines) == 1
    source, raw_name, destination = append_lines[0].split("\t")
    assert (source, raw_name) == (SOURCE, "Zxqv")

    map_ = CounterpartyMap()
    map_.record(source, raw_name, destination.replace("<category>", "Expenses:Hogar"))
    assert map_.resolve(source, raw_name) == "expense:Expenses:Hogar"

    entries = build_entries(
        parse_liquidacion(_cuit_probe_statement("Zxqv 20.12345678.9")), "P2", map_
    )
    charges = [entry for entry in entries if entry.payee == "Zxqv"]
    assert len(charges) == 1
    assert _postings(charges[0]) == [
        ("Expenses:Hogar", "42.13", "ARS"),
        ("Liabilities:Provincia:P2:Visa", "-42.13", "ARS"),
    ]


#: Every CUIT shape, as ``(description, printed identity)``; ``None`` marks the
#: embedded form, which refuses loudly and prints no identity at all.
CUIT_SHAPES = [
    ("Plugh 20345678901", "Plugh"),  # bare
    ("Plugh 20.12345678.9", "Plugh"),  # dotted
    ("Plugh 20-12345678-9", "Plugh"),  # hyphenated
    ("Plugh,20345678901", "Plugh,"),  # glued to punctuation
    ("Zxqv 20.12345678 9", "Zxqv"),  # mixed separators, space before the last digit
    ("X20345678901q", None),  # embedded inside name characters
]

_CUIT = "20345678901"
_CUIT_PARTS = [_CUIT[i : i + 4] for i in range(len(_CUIT) - 3)]


@pytest.mark.parametrize(
    ("description", "identity"),
    CUIT_SHAPES,
    ids=["bare", "dotted", "hyphenated", "glued", "mixed-space", "embedded"],
)
def test_no_cuit_shape_leaves_digits_in_the_printed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, description: str, identity: str | None
) -> None:
    """R3-001: no part of a CUIT reaches the identity the refusal prints.

    The embedded form refuses loudly instead; every other shape must print an
    identity free of digits and an ``append`` row that classifies on a real map.
    """
    if identity is None:
        with pytest.raises(LiquidacionParseError) as excinfo:
            _entries(_cuit_probe_statement(description), mapping={})
        assert not any(part in str(excinfo.value) for part in _CUIT_PARTS)
        return
    with pytest.raises(CounterpartyClassificationError) as excinfo:
        _entries(_cuit_probe_statement(description), mapping={})
    append = next(
        line.strip()
        for line in str(excinfo.value).splitlines()
        if line.strip().startswith(f"{SOURCE}\t")
    )
    printed = append.split("\t")[1]
    assert printed == identity
    assert not any(part in printed for part in _CUIT_PARTS)
    assert not any(char.isdigit() for char in printed)
    # Fed back to a real CounterpartyMap, the printed row classifies.
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))
    map_ = CounterpartyMap()
    map_.record(SOURCE, printed, "expense:Expenses:Hogar")
    entries = build_entries(parse_liquidacion(_cuit_probe_statement(description)), "P2", map_)
    assert [entry.payee for entry in entries if entry.payee != SOURCE] == [identity]


# ------------------------------------------------------------------ identify / paths


def test_identify_claims_the_card_marker_in_any_casing() -> None:
    """The statement's own title casing varies between renderings, so the match folds."""
    for title in (CARD_TITLE, CARD_TITLE.replace("Liquidación", "LIQUIDACION")):
        importer = ProvinciaVisaImporter(text_reader=lambda _p, _t=title: _t)
        assert importer.identify("/tmp/x.pdf") is True


def test_the_two_provincia_importers_never_claim_each_others_files() -> None:
    assert not ProvinciaVisaImporter(text_reader=lambda _p: EXTRACTO).identify("/tmp/x.pdf")
    assert not ProvinciaVisaImporter(text_reader=lambda _p: FULL).identify("/tmp/x.pdf")
    assert not ProvinciaImporter(text_reader=lambda _p: CARD_TITLE + FULL).identify("/tmp/x.pdf")


def test_identify_returns_false_when_extraction_raises() -> None:
    def boom(_path: str) -> str:
        raise OSError("unreadable for the test")

    assert ProvinciaVisaImporter(text_reader=boom).identify("/tmp/whatever.pdf") is False


def test_the_account_is_the_ars_card_liability() -> None:
    assert ProvinciaVisaImporter().account("/s/Provincia/P2/x.pdf") == (
        "Liabilities:Provincia:P2:Visa"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/statements/Provincia/statement.pdf",  # missing person folder
        "/statements/Provincia/P2/nested/statement.pdf",  # extra nesting after person
        "/statements/Provincia/a:b/statement.pdf",
        "/statements/Provincia/a b/statement.pdf",
        "/statements/Provincia/./statement.pdf",
        "/statements/OtherSource/P2/statement.pdf",
    ],
)
def test_a_path_outside_the_layout_is_refused_without_echoing_it(path: str) -> None:
    with pytest.raises(SourceAccountError) as excinfo:
        derive_person(path)
    message = str(excinfo.value)
    assert path not in message
    assert Path(path).name not in message
    assert "Provincia/<person>/<file>.pdf" in message


# ------------------------------------------------------------------ sort and reader


def test_sort_orders_by_date_then_by_key() -> None:
    entries = _entries(FULL)
    entries.reverse()
    ProvinciaVisaImporter().sort(entries)
    assert [(e.date, e.meta["key"]) for e in entries] == sorted(
        (e.date, e.meta["key"]) for e in _entries(FULL)
    )

    reversed_entries = _entries(FULL)
    ProvinciaVisaImporter().sort(reversed_entries, reverse=True)
    expected = sorted(
        ((e.date, e.meta["key"]) for e in _entries(FULL)),
        reverse=True,
    )
    assert [(e.date, e.meta["key"]) for e in reversed_entries] == expected


def test_a_reader_failure_is_wrapped_without_leaking_the_path() -> None:
    """A reader error carries the real path; ``extract`` must replace it with the class."""
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> str:
        raise FileNotFoundError(sensitive)

    importer = ProvinciaVisaImporter(text_reader=boom)

    with pytest.raises(StatementReadError) as excinfo:
        importer.extract("/statements/Provincia/P2/statement.pdf", [])

    message = str(excinfo.value)
    for token in (sensitive, "statement.pdf", "Nombre", "Apellido", "/statements/Provincia"):
        assert token not in message, (token, message)
    assert "FileNotFoundError" in message
    assert excinfo.value.__cause__ is None


def test_the_reader_failure_suppresses_its_context_in_a_traceback() -> None:
    """``from None`` is load-bearing: the suppressed context holds the real path."""
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> str:
        raise FileNotFoundError(sensitive)

    importer = ProvinciaVisaImporter(text_reader=boom)
    caught: StatementReadError | None = None
    try:
        importer.extract("/statements/Provincia/P2/synthetic.pdf", [])
    except StatementReadError as exc:
        caught = exc
        rendered = "".join(traceback.format_exception(exc))

    assert caught is not None
    assert caught.__suppress_context__ is True
    assert caught.__cause__ is None
    for token in (sensitive, "Nombre", "Apellido"):
        assert token not in rendered, (token, rendered)


def test_a_reconciliation_failure_propagates_untouched() -> None:
    """The parser's gate owns a bad document; extract must not mask it."""
    mutated = MINIMAL.replace(" 42,13", " 42,14", 1)
    importer = ProvinciaVisaImporter(text_reader=lambda _path: mutated)

    with pytest.raises(provincia_visa.ReconciliationError) as excinfo:
        importer.extract("/statements/Provincia/P2/statement.pdf", [])

    assert "charge-sums-ars" in {check.name for check in excinfo.value.failures}


# ------------------------------------------------------------------------- registration


def test_the_card_importer_is_registered_without_a_ledger() -> None:
    importers = get_importers()
    names = [importer.name for importer in importers]
    # The visible identity names the product (``ProvinciaVisa``), so the pipeline's
    # refusals and the CLI's claimed-file line stay unambiguous; the counterparty
    # map's source stays ``Provincia`` for both Provincia products.
    assert names == ["MercadoPago", "Provincia", "ProvinciaVisa", "Brubank"]
    assert isinstance(importers[2], ProvinciaVisaImporter)
    # The map source is still the bank, for both products.
    assert SOURCE == "Provincia"
