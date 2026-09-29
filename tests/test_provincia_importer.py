"""Tests for the Banco Provincia importer wiring (T-06a).

The parser's own tests (``test_provincia_parser.py``) prove it reads the format
and refuses a bad document. These prove the *ledger* side: the per-person path
declares the cash account, each movement kind posts where the design says, the
natural key is the parser's identity, and an unclassified counterparty stops the
import with the pinned refusal shape.

Everything is synthetic. No test reads a real statement, and the text reader is
always injected, so the default run needs neither ``pypdfium2`` nor any private
file. Registration in ``get_importers()`` is deliberately **not** pinned here:
T-05b pinned that list to exactly ``["MercadoPago"]`` in
``test_mercadopago_importer.py``, and the registration decision for this task is
recorded in the handoff, not silently forced through another test file.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import traceback
from decimal import Decimal
from pathlib import Path

import pytest

from expensuchis import pipeline
from expensuchis.counterparties import CounterpartyMap
from expensuchis.importers import get_importers
from expensuchis.importers.mercadopago_importer import MercadoPagoImporter
from expensuchis.importers.provincia import (
    ExtractoParseError,
    Movement,
    MovementKind,
    ReconciliationError,
)
from expensuchis.importers.provincia_importer import (
    SOURCE,
    CounterpartyClassificationError,
    ProvinciaImporter,
    SourceAccountError,
    StatementReadError,
    build_entries,
    derive_person,
)
from expensuchis.ledger import ENV_VAR
from expensuchis.paths import LedgerPaths

FIXTURES = Path(__file__).parent / "fixtures" / "provincia"
DESCRIBED = (FIXTURES / "described_rows.txt").read_text(encoding="utf-8")
REFERENCE_ROWS = (FIXTURES / "reference_rows.txt").read_text(encoding="utf-8")
WRAPPED = (FIXTURES / "wrapped_rows.txt").read_text(encoding="utf-8")
SHAPES = (FIXTURES / "shapes_rows.txt").read_text(encoding="utf-8")

BBVA_FIXTURES = Path(__file__).parent / "fixtures" / "bbva"
BBVA_FULL_STATEMENT = (BBVA_FIXTURES / "full_statement.txt").read_text(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[1]


class _StubMap:
    """A minimal stand-in for ``CounterpartyMap`` that records the lookups."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)
        self.calls: list[tuple[str, str]] = []

    def resolve(self, source: str, raw_name: str) -> str | None:
        self.calls.append((source, raw_name))
        return self._mapping.get(raw_name)


def _movement(
    amount: str,
    kind: MovementKind,
    *,
    description: str = "",
    reference: str = "",
    date: dt.date = dt.date(2026, 1, 1),
    line: int = 1,
) -> Movement:
    return Movement(
        date=date,
        description=description,
        reference=reference,
        amount=Decimal(amount),
        value_date="01-01",
        balance=Decimal("0.00"),
        page=1,
        line=line,
        kind=kind,
    )


def _extracto(*movements: Movement):
    from expensuchis.importers.provincia import Extracto, movement_keys

    movements = tuple(movements)
    return Extracto(
        opening=Decimal("0.00"),
        closing=Decimal("0.00"),
        declared_debits=Decimal("0.00"),
        movements=movements,
        checks=tuple(
            zip(
                ("running-balance-chain", "keys-unique", "vocabulary-known"),
                (True, len(set(movement_keys(movements))) == len(movements), True),
                ("synthetic", "synthetic", "synthetic"),
            )
        ),
    )


def _normalize(entries) -> list[tuple]:
    return [
        (
            entry.date.isoformat(),
            entry.payee,
            entry.narration,
            entry.meta["key"],
            tuple(
                (posting.account, str(posting.units.number), posting.units.currency)
                for posting in entry.postings
            ),
        )
        for entry in entries
    ]


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LedgerPaths:
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))
    paths = LedgerPaths()
    paths.ensure()
    return paths


# ------------------------------------------------------------------ path derivation


def test_a_valid_path_derives_the_person_cash_account() -> None:
    importer = ProvinciaImporter()
    assert derive_person("/statements/Provincia/P2/statement.pdf") == "P2"
    assert importer.account("/statements/Provincia/P2/statement.pdf") == (
        "Assets:Provincia:P2:Caja"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/statements/Provincia/statement.pdf",  # missing person folder
        "/statements/Provincia/P2/nested/statement.pdf",  # extra nesting after person
        "/statements/Provincia/a:b/statement.pdf",
        "/statements/Provincia/a b/statement.pdf",
        "/statements/Provincia/./statement.pdf",
        "/statements/Provincia/../statement.pdf",
        "/statements/Provincia//statement.pdf",  # empty token
        "/statements/OtherSource/P2/statement.pdf",  # no Provincia segment
    ],
)
def test_an_invalid_path_is_refused_without_echoing_it(path: str) -> None:
    with pytest.raises(SourceAccountError) as excinfo:
        derive_person(path)
    message = str(excinfo.value)
    assert path not in message
    assert Path(path).name not in message
    assert "Zxqv" not in message
    # The refusal names the convention the user must follow instead.
    assert "Provincia/<person>/<file>.pdf" in message
    assert "Assets:Provincia:<person>:Caja" in message


def test_the_importer_account_is_the_derived_cash_account() -> None:
    assert ProvinciaImporter().account("/s/Provincia/P3/x.pdf") == "Assets:Provincia:P3:Caja"


# ------------------------------------------------------------------- posting table


def test_every_described_kind_in_the_fixture_posts_to_its_decided_account() -> None:
    """The described fixture, checked entry by entry: accounts, signs, keys, payees.

    Purchases, debit-card purchases and bill payments resolve through the map; only
    the ``pago visa`` settlement posts to the card liability; income kinds post to
    their coarse income accounts; a positive ``intereses`` is income.
    """
    mapping = {
        "Zxqv Uno": "expense:Expenses:Otros",
        "Zxqv Servicio": "expense:Expenses:Servicios",
        "Zxqv": "expense:Expenses:Supermercado",
    }
    expected = [
        (
            "2026-01-01",
            "Provincia",
            "sueldo Zxqv Qwerty",
            "2026-01-01:sueldo Zxqv Qwerty:2500.13",
            (
                ("Assets:Provincia:P2:Caja", "2500.13", "ARS"),
                ("Income:P2:Sueldo", "-2500.13", "ARS"),
            ),
        ),
        (
            "2026-01-02",
            "Provincia",
            "deposito Zxqv",
            "2026-01-02:deposito Zxqv:320.09",
            (
                ("Assets:Provincia:P2:Caja", "320.09", "ARS"),
                ("Income:P2:Depositos", "-320.09", "ARS"),
            ),
        ),
        (
            "2026-01-03",
            "Provincia",
            "credito Zxqv Plugh",
            "2026-01-03:credito Zxqv Plugh:77.51",
            (
                ("Assets:Provincia:P2:Caja", "77.51", "ARS"),
                ("Income:P2:Creditos", "-77.51", "ARS"),
            ),
        ),
        (
            "2026-01-04",
            "Provincia",
            "haberes Zxqv",
            "2026-01-04:haberes Zxqv:1250.63",
            (
                ("Assets:Provincia:P2:Caja", "1250.63", "ARS"),
                ("Income:P2:Sueldo", "-1250.63", "ARS"),
            ),
        ),
        (
            "2026-01-05",
            "Zxqv Uno",
            "compra Zxqv Uno",
            "2026-01-05:compra Zxqv Uno:-150.07",
            (
                ("Assets:Provincia:P2:Caja", "-150.07", "ARS"),
                ("Expenses:Otros", "150.07", "ARS"),
            ),
        ),
        (
            "2026-01-06",
            "Zxqv",
            "compra tarjeta Zxqv",
            "2026-01-06:compra tarjeta Zxqv:-400.11",
            (
                ("Assets:Provincia:P2:Caja", "-400.11", "ARS"),
                ("Expenses:Supermercado", "400.11", "ARS"),
            ),
        ),
        (
            "2026-01-07",
            "Zxqv Servicio",
            "pago Zxqv Servicio",
            "2026-01-07:pago Zxqv Servicio:-89.31",
            (
                ("Assets:Provincia:P2:Caja", "-89.31", "ARS"),
                ("Expenses:Servicios", "89.31", "ARS"),
            ),
        ),
        (
            "2026-01-08",
            "Provincia",
            "pago visa Zxqv",
            "2026-01-08:pago visa Zxqv:-700.23",
            (
                ("Assets:Provincia:P2:Caja", "-700.23", "ARS"),
                ("Liabilities:Provincia:P2:Visa", "700.23", "ARS"),
            ),
        ),
        (
            "2026-01-09",
            "Provincia",
            "intereses",
            "2026-01-09:intereses:12.41",
            (
                ("Assets:Provincia:P2:Caja", "12.41", "ARS"),
                ("Income:P2:Intereses", "-12.41", "ARS"),
            ),
        ),
    ]
    importer = ProvinciaImporter(
        counterparty_map=_StubMap(mapping),
        text_reader=lambda _path, _text=DESCRIBED: _text,
    )
    entries = importer.extract("/statements/Provincia/P2/statement.pdf", [])
    assert _normalize(entries) == expected


@pytest.mark.parametrize(
    ("kind", "amount", "expected_account"),
    [
        (MovementKind.CARD_SETTLEMENT, "-20.55", "Liabilities:Provincia:P2:Visa"),
        (MovementKind.REFERENCE, "-30.07", "Assets:TransferenciaEnTransito"),
        (MovementKind.REFERENCE, "40.09", "Assets:TransferenciaEnTransito"),
        (MovementKind.INTEREST, "-2.22", "Expenses:CargosBancarios"),
        (MovementKind.FEES, "-15.25", "Expenses:CargosBancarios"),
        (MovementKind.CHARGES, "-8.08", "Expenses:CargosBancarios"),
        (MovementKind.DEBIT, "-33.33", "Expenses:CargosBancarios"),
        (MovementKind.TOPUP, "-55.44", "Expenses:Otros"),
        (MovementKind.REFUND, "5.55", "Income:P2:Devoluciones"),
        (MovementKind.CREDIT, "77.51", "Income:P2:Creditos"),
    ],
    ids=[
        "card-settlement",
        "reference-debit",
        "reference-credit",
        "interest-negative-is-a-fee",
        "fees",
        "charges",
        "debit",
        "topup",
        "refund",
        "credit",
    ],
)
def test_fixed_kinds_post_to_their_decided_account(
    kind: MovementKind, amount: str, expected_account: str
) -> None:
    movement = (
        _movement(amount, kind, reference="Nº.1 2:3")
        if kind is MovementKind.REFERENCE
        else _movement(amount, kind, description=f"{kind.value} Zxqv")
    )
    entries = build_entries(_extracto(movement), "P2", _StubMap({}))
    assert entries[0].postings[1].account == expected_account


def test_a_card_purchase_posts_through_the_counterparty_map_like_any_compra() -> None:
    """User-confirmed: ``compra tarjeta`` is a debit-card purchase, not a card accrual.

    The old posting sent it to the card liability; it now resolves through the map
    exactly as ``compra`` does, so a mutant restoring the liability leg fails here.
    The ``TARJETA`` token is per-row noise, not merchant identity, so the map key
    and the payee are the merchant words alone.
    """
    movement = _movement("-400.11", MovementKind.CARD_PURCHASE, description="compra tarjeta Zxqv")
    entries = build_entries(
        _extracto(movement), "P2", _StubMap({"Zxqv": "expense:Expenses:Supermercado"})
    )
    assert entries[0].postings[1].account == "Expenses:Supermercado"
    assert entries[0].payee == "Zxqv"


# ------------------------------------------------------- normalized counterparty identity


def test_the_payee_is_the_merchant_identity_not_the_raw_noise() -> None:
    """The map key and payee are the merchant words, never the per-row structure.

    The description's ``DE`` marker, the parenthesized token (which can be the
    counterparty's CUIT/CUIL — synthetic here, and never a module or probe
    diagnostic), the label and the digit/date/time tokens are per-row noise.
    """
    movement = _movement(
        "-10.00", MovementKind.PURCHASE, description="compra DE Zxqv Uno (20345678901) Qwerty"
    )
    entries = build_entries(
        _extracto(movement), "P2", _StubMap({"Zxqv Uno Qwerty": "expense:Expenses:Otros"})
    )
    assert entries[0].payee == "Zxqv Uno Qwerty"
    assert entries[0].narration == "compra DE Zxqv Uno (20345678901) Qwerty"


def test_the_same_merchant_with_two_ids_resolves_through_one_map_key() -> None:
    """Rows of one merchant differing only in the per-row id share one identity.

    Before the correction the parenthesized token leaked into the key, so the
    second row of the same merchant raised ``CounterpartyClassificationError``.
    Each distinct merchant identity is looked up exactly twice here, from one
    map entry per merchant.
    """
    first = _movement(
        "-150.07",
        MovementKind.PURCHASE,
        description="compra DE Zxqv Uno (20345678901) Qwerty",
        date=dt.date(2026, 2, 1),
    )
    second = _movement(
        "-60.13",
        MovementKind.PURCHASE,
        description="compra DE Zxqv Uno (20345678902) Qwerty",
        date=dt.date(2026, 2, 2),
    )
    stub = _StubMap({"Zxqv Uno Qwerty": "expense:Expenses:Otros"})
    entries = build_entries(_extracto(first, second), "P2", stub)
    assert stub.calls == [
        (SOURCE, "Zxqv Uno Qwerty"),
        (SOURCE, "Zxqv Uno Qwerty"),
    ]
    assert len(entries) == 2


def test_the_same_merchant_with_two_card_times_resolves_through_one_map_key() -> None:
    """``compra TARJETA`` rows differ in the purchase's date and time, not the merchant."""
    first = _movement(
        "-88.41",
        MovementKind.CARD_PURCHASE,
        description="compra TARJETA 05/12/25 18:30 Zxqv. Comercio Quince",
        date=dt.date(2026, 2, 4),
    )
    second = _movement(
        "-12.09",
        MovementKind.CARD_PURCHASE,
        description="compra TARJETA 06/12/25 09:15 Zxqv. Comercio Quince",
        date=dt.date(2026, 2, 5),
    )
    stub = _StubMap({"Zxqv. Comercio Quince": "expense:Expenses:Supermercado"})
    entries = build_entries(_extracto(first, second), "P2", stub)
    assert stub.calls == [
        (SOURCE, "Zxqv. Comercio Quince"),
        (SOURCE, "Zxqv. Comercio Quince"),
    ]
    assert [entry.payee for entry in entries] == ["Zxqv. Comercio Quince"] * 2


def test_every_observed_description_shape_resolves_to_its_merchant_identity(
    ledger: LedgerPaths,
) -> None:
    """The shapes from the masked geometry dump all normalize stably.

    The shapes fixture carries one row per observed shape; a real map with one
    entry per merchant identity resolves all eight movements, and each payee is
    the merchant identity the ``counterparties.tsv`` row names.
    """
    counterparties = CounterpartyMap()
    for name in (
        "Zxqv Uno Qwerty",
        "Zxqv Uno A Qwerty",
        "Zxqv. Comercio Quince",
        "Zxqv/Plugh Qwerty",
        "Plugh efectivo Qwerty Zxqv",
        "Zxqv,Plugh Qwerty",
    ):
        counterparties.record(SOURCE, name, "expense:Expenses:Otros")

    entries = ProvinciaImporter(
        counterparty_map=counterparties,
        text_reader=lambda _path, _text=SHAPES: _text,
    ).extract("/statements/Provincia/P2/statement.pdf", [])

    assert [entry.payee for entry in entries] == [
        "Zxqv Uno Qwerty",
        "Zxqv Uno Qwerty",
        "Zxqv Uno A Qwerty",
        "Zxqv. Comercio Quince",
        "Zxqv. Comercio Quince",
        "Zxqv/Plugh Qwerty",
        "Plugh efectivo Qwerty Zxqv",
        "Zxqv,Plugh Qwerty",
    ]


def test_the_unclassified_refusal_names_the_normalized_key() -> None:
    """The suggested ``counterparties.tsv`` row matches the key the map will see.

    Two movements of one merchant with different per-row ids must refuse **once**
    — one unique counterparty — with the normalized name in both the display and
    the append suggestion.
    """
    first = _movement(
        "-150.07",
        MovementKind.PURCHASE,
        description="compra DE Zxqv Uno (20345678901) Qwerty",
        date=dt.date(2026, 2, 1),
        line=4,
    )
    second = _movement(
        "-60.13",
        MovementKind.PURCHASE,
        description="compra DE Zxqv Uno (20345678902) Qwerty",
        date=dt.date(2026, 2, 2),
        line=5,
    )

    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(_extracto(first, second), "P2", _StubMap({}))

    message = str(excinfo.value)
    assert message == (
        "1 counterparties are not classified.\n"
        '  2026-02-01  -150.07 ARS  "compra DE Zxqv Uno (<cuit>) Qwerty"  "Zxqv Uno Qwerty"\n'
        "    append to counterparties.tsv:\n"
        "      Provincia\tZxqv Uno Qwerty\texpense:<category>"
    )


def test_the_unclassified_refusal_redacts_the_counterparty_cuit() -> None:
    """No CUIT/CUIL-shaped 11-digit run survives into the CLI-printed refusal.

    The message is human-facing and the CLI prints it, so an 11-digit run in the
    raw description must be redacted rather than reach an agent's transcript.
    The merchant identity stays readable.
    """
    movement = _movement(
        "-150.07",
        MovementKind.PURCHASE,
        description="compra DE Zxqv Uno (20345678901) Qwerty",
        date=dt.date(2026, 2, 1),
        line=4,
    )

    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(_extracto(movement), "P2", _StubMap({}))

    message = str(excinfo.value)
    assert "20345678901" not in message
    assert "(<cuit>)" in message
    assert "Zxqv Uno Qwerty" in message


def test_a_description_that_normalizes_to_nothing_is_refused() -> None:
    """A purchase whose only content is noise has no merchant identity to look up."""
    movement = _movement(
        "-10.00", MovementKind.PURCHASE, description="compra (20345678901)", line=7
    )

    with pytest.raises(ExtractoParseError) as excinfo:
        build_entries(_extracto(movement), "P2", _StubMap({}))

    message = str(excinfo.value)
    assert "movement 1" in message and "line 7" in message
    assert "20345678901" not in message


def test_a_card_settlement_never_reaches_the_counterparty_map() -> None:
    """Only the exact ``pago visa`` token pair is a settlement — and it bypasses the map."""
    movement = _movement("-700.23", MovementKind.CARD_SETTLEMENT, description="pago visa Zxqv")
    stub = _StubMap({})
    entries = build_entries(_extracto(movement), "P2", stub)
    assert entries[0].postings[1].account == "Liabilities:Provincia:P2:Visa"
    assert stub.calls == []


def test_sort_orders_by_date_then_by_key() -> None:
    """Document order is chronological, but the sort must not depend on it.

    Mirrors the pipeline's month grouping: a total, deterministic date-then-key
    order, with the natural key as the tiebreaker and ``reverse`` honoured.
    """
    movements = (
        _movement(
            "10.00", MovementKind.SALARY, description="sueldo Zxqv", date=dt.date(2026, 1, 5)
        ),
        _movement(
            "30.00", MovementKind.SALARY, description="sueldo Plugh", date=dt.date(2026, 1, 2)
        ),
        _movement(
            "20.00", MovementKind.SALARY, description="sueldo Qwerty", date=dt.date(2026, 1, 2)
        ),
    )
    entries = build_entries(_extracto(*movements), "P2", _StubMap({}))
    ProvinciaImporter().sort(entries)
    assert [entry.meta["key"] for entry in entries] == [
        "2026-01-02:sueldo Plugh:30.00",
        "2026-01-02:sueldo Qwerty:20.00",
        "2026-01-05:sueldo Zxqv:10.00",
    ]

    reversed_entries = build_entries(_extracto(*movements), "P2", _StubMap({}))
    ProvinciaImporter().sort(reversed_entries, reverse=True)
    assert [entry.meta["key"] for entry in reversed_entries] == [
        "2026-01-05:sueldo Zxqv:10.00",
        "2026-01-02:sueldo Qwerty:20.00",
        "2026-01-02:sueldo Plugh:30.00",
    ]


def test_reference_rows_post_to_the_clearing_account_with_their_reference_narration() -> None:
    movement = _movement(
        "-50.07", MovementKind.REFERENCE, reference="Nº.12345 78/78-1.234567 9:12345678901"
    )
    entries = build_entries(_extracto(movement), "P2", _StubMap({}))
    assert [posting.account for posting in entries[0].postings] == [
        "Assets:Provincia:P2:Caja",
        "Assets:TransferenciaEnTransito",
    ]
    assert entries[0].narration == "Nº.12345 78/78-1.234567 9:12345678901"


def test_entry_meta_carries_only_the_key() -> None:
    entries = ProvinciaImporter(
        counterparty_map=_StubMap({}),
        text_reader=lambda _path: REFERENCE_ROWS,
    ).extract("/statements/Provincia/P2/statement.pdf", [])
    assert all(set(entry.meta) == {"key"} for entry in entries)
    assert entries[0].meta["key"] == ("2026-01-10:12345678901:-50.07")


def test_the_key_zero_pads_a_whole_peso_amount_to_two_decimals() -> None:
    """``amount:.2f`` is the contract; ``str(amount)`` would write ``100`` and break dedup."""
    movement = _movement(
        "100",
        MovementKind.SALARY,
        description="sueldo Zxqv",
        date=dt.date(2026, 1, 1),
    )
    entries = build_entries(_extracto(movement), "P2", _StubMap({}))
    assert entries[0].meta["key"] == "2026-01-01:sueldo Zxqv:100.00"


def test_the_reader_seam_supplies_the_statement_text() -> None:
    calls: list[str] = []

    def reader(path: str) -> str:
        calls.append(path)
        return REFERENCE_ROWS

    importer = ProvinciaImporter(counterparty_map=_StubMap({}), text_reader=reader)
    importer.extract("/statements/Provincia/P2/statement.pdf", [])
    assert calls == ["/statements/Provincia/P2/statement.pdf"]


def test_a_reader_failure_is_wrapped_without_leaking_the_path() -> None:
    """A reader error carries the real path; ``extract`` must replace it with the class."""
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> str:
        raise FileNotFoundError(sensitive)

    importer = ProvinciaImporter(text_reader=boom)

    with pytest.raises(StatementReadError) as excinfo:
        importer.extract("/statements/Provincia/P2/statement.pdf", [])

    message = str(excinfo.value)
    for token in (sensitive, "statement.pdf", "Nombre", "Apellido", "/statements/Provincia"):
        assert token not in message, (token, message)
    assert "FileNotFoundError" in message
    assert excinfo.value.__cause__ is None


def test_the_reader_failure_suppresses_its_context_in_a_traceback() -> None:
    """``from None`` is load-bearing: the suppressed context holds the real path.

    Without it the ``FileNotFoundError`` stays in ``__context__`` and the traceback
    prints its message with the real path, so removing ``from None`` must fail here
    even though ``str(exc)`` stays masked. The extract path deliberately avoids the
    sensitive filename so the assertion cannot be satisfied or broken by the test's
    own source line in the traceback.
    """
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> str:
        raise FileNotFoundError(sensitive)

    importer = ProvinciaImporter(text_reader=boom)
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


# ------------------------------------------------------------------ map resolution


def test_expense_and_internal_destinations_use_the_account_part_verbatim(
    ledger: LedgerPaths,
) -> None:
    """A real ``CounterpartyMap`` resolves both destination prefixes.

    The map value's marker says inside/outside the ledger; the account after it is
    used verbatim. Only the marker is removed: a naive split on every ``:`` would
    truncate ``internal:Assets:Provincia:P2:Caja`` to ``Assets``.
    """
    counterparties = CounterpartyMap()
    counterparties.record(SOURCE, "Zxqv Uno", "expense:Expenses:Supermercado")
    counterparties.record(SOURCE, "Zxqv Dos", "internal:Assets:Provincia:P3:Caja")

    entries = build_entries(
        _extracto(
            _movement("-10.00", MovementKind.PURCHASE, description="compra Zxqv Uno"),
            _movement("-20.00", MovementKind.PURCHASE, description="compra Zxqv Dos"),
        ),
        "P2",
        counterparties,
    )

    assert entries[0].postings[1].account == "Expenses:Supermercado"
    assert entries[0].postings[1].units.number == Decimal("10.00")
    assert entries[0].payee == "Zxqv Uno"
    assert entries[1].postings[1].account == "Assets:Provincia:P3:Caja"
    assert entries[1].postings[1].units.number == Decimal("20.00")
    assert entries[1].payee == "Zxqv Dos"


def test_unclassified_counterparties_group_by_unique_raw_name() -> None:
    """Two movements naming one counterparty produce one row, and the count is 1."""
    first = _movement(
        "-100.00",
        MovementKind.PURCHASE,
        description="compra Zxqv Uno",
        date=dt.date(2026, 1, 2),
        line=10,
    )
    second = _movement(
        "-200.00",
        MovementKind.PURCHASE,
        description="compra Zxqv Uno",
        date=dt.date(2026, 1, 3),
        line=20,
    )

    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(_extracto(first, second), "P2", _StubMap({}))

    assert str(excinfo.value) == (
        "1 counterparties are not classified.\n"
        '  2026-01-02  -100.00 ARS  "compra Zxqv Uno"  "Zxqv Uno"\n'
        "    append to counterparties.tsv:\n"
        "      Provincia\tZxqv Uno\texpense:<category>"
    )


def test_unclassified_refusal_matches_the_pinned_shape_for_several_names() -> None:
    """Full message equality, including the two/4/6 indentation and the literal tab."""
    movements = (
        _movement(
            "-25000.00",
            MovementKind.PURCHASE,
            description="compra Zxqv Uno",
            date=dt.date(2026, 4, 2),
            line=5,
        ),
        _movement(
            "-40000.00",
            MovementKind.BILL_PAYMENT,
            description="pago Zxqv Servicio",
            date=dt.date(2026, 4, 5),
            line=9,
        ),
    )

    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(_extracto(*movements), "P2", _StubMap({}))

    assert str(excinfo.value) == (
        "2 counterparties are not classified.\n"
        '  2026-04-02  -25,000.00 ARS  "compra Zxqv Uno"  "Zxqv Uno"\n'
        "    append to counterparties.tsv:\n"
        "      Provincia\tZxqv Uno\texpense:<category>\n"
        '  2026-04-05  -40,000.00 ARS  "pago Zxqv Servicio"  "Zxqv Servicio"\n'
        "    append to counterparties.tsv:\n"
        "      Provincia\tZxqv Servicio\texpense:<category>"
    )


def test_an_empty_raw_name_is_refused_with_a_masked_actionable_error() -> None:
    movement = _movement("-10.00", MovementKind.PURCHASE, description="compra", line=7)

    with pytest.raises(ExtractoParseError) as excinfo:
        build_entries(_extracto(movement), "P2", _StubMap({}))

    message = str(excinfo.value)
    assert "movement 1" in message and "line 7" in message
    assert "compra" not in message


def test_an_unknown_kind_is_refused_defensively() -> None:
    movement = _movement("-10.00", MovementKind.UNKNOWN, description="qwertyzacion Zxqv", line=3)

    with pytest.raises(ExtractoParseError) as excinfo:
        build_entries(_extracto(movement), "P2", _StubMap({}))

    assert "movement 1" in str(excinfo.value) and "line 3" in str(excinfo.value)
    assert "qwertyzacion" not in str(excinfo.value)


def test_a_reconciliation_failure_propagates_untouched() -> None:
    """A repeated natural key is already refused by the parser; extract must not mask it."""
    mutated = WRAPPED.replace(" 354.64", " 354.65", 1)
    importer = ProvinciaImporter(text_reader=lambda _path: mutated)

    with pytest.raises(ReconciliationError) as excinfo:
        importer.extract("/statements/Provincia/P2/statement.pdf", [])

    assert "running-balance-chain" in {check.name for check in excinfo.value.failures}


# ------------------------------------------------------------------------- identify


def test_identify_claims_a_file_carrying_the_marker() -> None:
    importer = ProvinciaImporter(text_reader=lambda _path: WRAPPED)
    assert importer.identify("/tmp/not-a-real-file.pdf") is True


def test_identify_claims_the_marker_in_any_casing() -> None:
    """The statement's own title casing varies between renderings, so the match folds."""
    for text in (
        WRAPPED.replace("Extracto de Cuenta", "Extracto de cuenta", 1),
        WRAPPED.replace("Extracto de Cuenta", "EXTRACTO DE CUENTA", 1),
    ):
        assert ProvinciaImporter(text_reader=lambda _p, _t=text: _t).identify("/tmp/x.pdf") is True


def test_identify_returns_false_without_the_marker() -> None:
    importer = ProvinciaImporter(text_reader=lambda _path: "unrelated prose")
    assert importer.identify("/tmp/not-a-real-file.pdf") is False


def test_identify_returns_false_when_extraction_raises() -> None:
    def boom(_path: str) -> str:
        raise OSError("unreadable for the test")

    assert ProvinciaImporter(text_reader=boom).identify("/tmp/whatever.pdf") is False


def test_both_importers_cannot_claim_each_other_statements() -> None:
    assert not MercadoPagoImporter(text_reader=lambda _path: WRAPPED).identify("/tmp/x.pdf")
    assert not ProvinciaImporter(text_reader=lambda _path: "RESUMEN DE CUENTA EN PESOS\n").identify(
        "/tmp/x.pdf"
    )


def test_a_bbva_account_statement_is_claimed_by_exactly_one_registered_importer() -> None:
    """A BBVA caja de ahorro statement can carry the marker phrase as boilerplate.

    ``MARKER`` is matched as a raw substring over the whole folded document, so
    it used to fire whenever the phrase "Extracto de Cuenta" turned up anywhere
    in a BBVA statement's incidental text, not just in an actual Provincia
    title: two claimants made ``extract`` refuse the file as
    ``ambiguous-importer``. This mirrors the same bug already fixed for the
    card siblings in ``test_bbva_card_importer.py``.
    """
    text = BBVA_FULL_STATEMENT + "\nCondiciones generales: consulte su Extracto de Cuenta.\n"
    claimants = [
        importer.name
        for importer in (type(i)(text_reader=lambda _p: text) for i in get_importers())
        if importer.identify("/statements/Bbva/P2/x.pdf")
    ]
    assert claimants == ["BBVA"]


# ------------------------------------------------------------------- registration


def test_importing_the_importer_package_does_not_import_pypdfium2() -> None:
    script = (
        "import sys\n"
        "import expensuchis.importers\n"
        "import expensuchis.importers.provincia_importer\n"
        "assert 'pypdfium2' not in sys.modules, 'pypdfium2 was imported eagerly'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# ------------------------------------------------------------- pipeline integration

MAIN_TEMPLATE = (
    'option "title" "Test ledger"\n'
    'option "operating_currency" "ARS"\n'
    'include "accounts.beancount"\n'
    'include "transactions/*.beancount"\n'
)

ACCOUNTS = (
    "2020-01-01 open Equity:Opening-Balances\n"
    "2020-01-01 open Assets:TransferenciaEnTransito\n"
    "2020-01-01 open Assets:Provincia:P2:Caja\n"
)

PLACEHOLDER = ";; placeholder so the include glob matches an empty ledger\n"


def _write_ledger(paths: LedgerPaths) -> None:
    paths.main().write_text(MAIN_TEMPLATE, encoding="utf-8")
    paths.accounts().write_text(ACCOUNTS, encoding="utf-8")
    (paths.transactions_dir() / "placeholder.beancount").write_text(PLACEHOLDER, encoding="utf-8")


def test_pipeline_extract_stages_keys_and_a_second_run_reports_them_skipped(
    ledger: LedgerPaths,
) -> None:
    _write_ledger(ledger)
    statement_dir = ledger.statements_dir() / "Provincia" / "P2"
    statement_dir.mkdir(parents=True)
    statement = statement_dir / "statement.pdf"
    statement.write_text("synthetic placeholder; the text reader is injected\n", encoding="utf-8")

    importer = ProvinciaImporter(text_reader=lambda _path: REFERENCE_ROWS)
    first = pipeline.extract(ledger, "Provincia", statement, importers=[importer])

    proposed = (ledger.staging_batch(first.batch_id) / "proposed.beancount").read_text(
        encoding="utf-8"
    )
    assert "key:" in proposed
    assert "Assets:TransferenciaEnTransito" in proposed
    assert first.entries == 3
    assert first.skipped == 0

    pipeline.approve(ledger, first.batch_id)
    pipeline.append(ledger, first.batch_id)
    assert pipeline.ledger_errors(ledger) == []

    second = pipeline.extract(ledger, "Provincia", statement, importers=[importer])
    assert second.entries == 0
    assert second.skipped == 3
    assert (ledger.staging_batch(second.batch_id) / "proposed.beancount").read_bytes() == b""
