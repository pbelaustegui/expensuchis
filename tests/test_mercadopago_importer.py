"""Tests for the Mercado Pago importer wiring (T-05b).

The parser's own tests (``test_mercadopago_parser.py``) prove it reads the format
and refuses a bad document. These prove the *ledger* side: the per-person path
declares the cash account, each movement kind posts where the design says, the
natural key is the parser's identity, and an unclassified counterparty stops the
import with the pinned refusal shape.

Everything is synthetic. No test reads a real statement, and the one that builds a
PDF in memory is opt-in behind ``EXPENSUCHIS_TEST_PDFIUM=1`` (marked ``pdfium``),
so the default run needs neither ``pypdfium2`` nor any private file.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import traceback
from decimal import Decimal
from pathlib import Path

import pytest

from expensuchis import pipeline
from expensuchis.counterparties import CounterpartyMap
from expensuchis.importers import get_importers
from expensuchis.importers.mercadopago import (
    Movement,
    MovementKind,
    ReconciliationError,
    Resumen,
    ResumenParseError,
)
from expensuchis.importers.mercadopago_importer import (
    SOURCE,
    CounterpartyClassificationError,
    MercadoPagoImporter,
    SourceAccountError,
    StatementReadError,
    build_entries,
    derive_person,
)
from expensuchis.ledger import ENV_VAR
from expensuchis.paths import LedgerPaths

FIXTURES = Path(__file__).parent / "fixtures" / "mercadopago"
COMPACT = (FIXTURES / "compact_rows.txt").read_text(encoding="utf-8")
WRAPPED = (FIXTURES / "wrapped_rows.txt").read_text(encoding="utf-8")
WRAPPED_DESCRIPTION = (FIXTURES / "wrapped_description.txt").read_text(encoding="utf-8")

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
    description: str,
    kind: MovementKind,
    *,
    operation_id: str = "1",
    date: dt.date = dt.date(2026, 1, 1),
    line: int = 1,
) -> Movement:
    return Movement(
        date=date,
        operation_id=operation_id,
        description=description,
        amount=Decimal(amount),
        balance=Decimal("0.00"),
        page=1,
        line=line,
        kind=kind,
    )


def _resumen(*movements: Movement) -> Resumen:
    return Resumen(
        opening=Decimal("0.00"),
        declared_entries=Decimal("0.00"),
        declared_withdrawals=Decimal("0.00"),
        closing=Decimal("0.00"),
        period_text="synthetic",
        movements=tuple(movements),
        checks=(),
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
    importer = MercadoPagoImporter()
    assert derive_person("/statements/MercadoPago/P1/statement.pdf") == "P1"
    assert importer.account("/statements/MercadoPago/P1/statement.pdf") == (
        "Assets:MercadoPago:P1:Caja"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/statements/MercadoPago/statement.pdf",  # missing person folder
        "/statements/MercadoPago/P1/nested/statement.pdf",  # extra nesting after person
        "/statements/MercadoPago/a:b/statement.pdf",
        "/statements/MercadoPago/a b/statement.pdf",
        "/statements/MercadoPago/./statement.pdf",
        "/statements/MercadoPago/../statement.pdf",
        "/statements/MercadoPago//statement.pdf",  # empty token
        "/statements/OtherSource/P1/statement.pdf",  # no Mercado Pago segment
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
    assert "MercadoPago/<person>/<file>.pdf" in message
    assert "Assets:MercadoPago:<person>:Caja" in message


def test_the_importer_account_is_the_derived_cash_account() -> None:
    assert MercadoPagoImporter().account("/s/MercadoPago/P3/x.pdf") == (
        "Assets:MercadoPago:P3:Caja"
    )


# ------------------------------------------------------------------- posting table


def test_every_kind_posts_to_its_decided_account() -> None:
    """The three fixtures, checked entry by entry: accounts, signs, keys, payees.

    ``Dinero retirado`` posts to ``Expenses:Efectivo`` and ``Dinero reservado`` to
    the clearing account, the two decisions recorded on 2026-09-25.
    """
    mapping = {
        "Zxqv Uno": "expense:Expenses:Otros",
        "Zxqv Dos": "internal:Assets:MercadoPago:P2:Caja",
        "Zxqv Servicio": "expense:Expenses:Servicios",
        "Zxqv Qwerty Plugh": "expense:Expenses:Otros",
    }
    expected_compact = [
        (
            "2026-01-01",
            "MercadoPago",
            "Rendimientos Zxqv",
            "2026-01-01:100000000001:111.11",
            (
                ("Assets:MercadoPago:P1:Caja", "111.11", "ARS"),
                ("Income:P1:Rendimientos", "-111.11", "ARS"),
            ),
        ),
        (
            "2026-01-02",
            "MercadoPago",
            "Transferencia recibida Zxqv Dos",
            "2026-01-02:100000000002:222.22",
            (
                ("Assets:MercadoPago:P1:Caja", "222.22", "ARS"),
                ("Assets:TransferenciaEnTransito", "-222.22", "ARS"),
            ),
        ),
        (
            "2026-01-02",
            "MercadoPago",
            "Rendimientos Qwerty",
            "2026-01-02:100000000003:222.22",
            (
                ("Assets:MercadoPago:P1:Caja", "222.22", "ARS"),
                ("Income:P1:Rendimientos", "-222.22", "ARS"),
            ),
        ),
        (
            "2026-01-03",
            "MercadoPago",
            "Transferencia enviada Plugh",
            "2026-01-03:100000000004:-444.44",
            (
                ("Assets:MercadoPago:P1:Caja", "-444.44", "ARS"),
                ("Assets:TransferenciaEnTransito", "444.44", "ARS"),
            ),
        ),
        (
            "2026-01-04",
            "MercadoPago",
            "Rendimientos Zxqv Tres",
            "2026-01-04:100000000005:444.44",
            (
                ("Assets:MercadoPago:P1:Caja", "444.44", "ARS"),
                ("Income:P1:Rendimientos", "-444.44", "ARS"),
            ),
        ),
    ]
    expected_wrapped = [
        (
            "2026-02-01",
            "MercadoPago",
            "Transferencia recibida Zxqv Dos",
            "2026-02-01:200000000001:456.78",
            (
                ("Assets:MercadoPago:P1:Caja", "456.78", "ARS"),
                ("Assets:TransferenciaEnTransito", "-456.78", "ARS"),
            ),
        ),
        (
            "2026-02-02",
            "Zxqv Uno",
            "Pago con Zxqv Uno",
            "2026-02-02:200000000002:-312.34",
            (
                ("Assets:MercadoPago:P1:Caja", "-312.34", "ARS"),
                ("Expenses:Otros", "312.34", "ARS"),
            ),
        ),
        (
            "2026-02-03",
            "Zxqv Dos",
            "Pago con Zxqv Dos",
            "2026-02-03:200000000003:-123.45",
            (
                ("Assets:MercadoPago:P1:Caja", "-123.45", "ARS"),
                ("Assets:MercadoPago:P2:Caja", "123.45", "ARS"),
            ),
        ),
        (
            "2026-02-04",
            "Zxqv Servicio",
            "Pago Zxqv Servicio",
            "2026-02-04:200000000004:-512.34",
            (
                ("Assets:MercadoPago:P1:Caja", "-512.34", "ARS"),
                ("Expenses:Servicios", "512.34", "ARS"),
            ),
        ),
        (
            "2026-02-05",
            "MercadoPago",
            "Dinero retirado",
            "2026-02-05:200000000005:-212.34",
            (
                ("Assets:MercadoPago:P1:Caja", "-212.34", "ARS"),
                ("Expenses:Efectivo", "212.34", "ARS"),
            ),
        ),
        (
            "2026-02-06",
            "MercadoPago",
            "Dinero reservado",
            "2026-02-06:200000000006:-112.34",
            (
                ("Assets:MercadoPago:P1:Caja", "-112.34", "ARS"),
                ("Assets:TransferenciaEnTransito", "112.34", "ARS"),
            ),
        ),
        (
            "2026-02-07",
            "MercadoPago",
            "Transferencia enviada Plugh",
            "2026-02-07:200000000007:-111.11",
            (
                ("Assets:MercadoPago:P1:Caja", "-111.11", "ARS"),
                ("Assets:TransferenciaEnTransito", "111.11", "ARS"),
            ),
        ),
        (
            "2026-02-08",
            "MercadoPago",
            "Rendimientos Zxqv",
            "2026-02-08:200000000008:666.66",
            (
                ("Assets:MercadoPago:P1:Caja", "666.66", "ARS"),
                ("Income:P1:Rendimientos", "-666.66", "ARS"),
            ),
        ),
    ]
    expected_wrapped_description = [
        (
            "2026-03-10",
            "Zxqv Qwerty Plugh",
            "Pago con Zxqv Qwerty Plugh",
            "2026-03-10:300000000001:-321.09",
            (
                ("Assets:MercadoPago:P1:Caja", "-321.09", "ARS"),
                ("Expenses:Otros", "321.09", "ARS"),
            ),
        ),
    ]
    for text, expected in (
        (COMPACT, expected_compact),
        (WRAPPED, expected_wrapped),
        (WRAPPED_DESCRIPTION, expected_wrapped_description),
    ):
        importer = MercadoPagoImporter(
            counterparty_map=_StubMap(mapping),
            text_reader=lambda _path, _text=text: _text,
        )
        entries = importer.extract("/statements/MercadoPago/P1/statement.pdf", [])
        assert _normalize(entries) == expected


def test_entry_meta_carries_only_the_key() -> None:
    entries = MercadoPagoImporter(
        counterparty_map=_StubMap({}),
        text_reader=lambda _path: COMPACT,
    ).extract("/statements/MercadoPago/P1/statement.pdf", [])
    assert all(set(entry.meta) == {"key"} for entry in entries)
    assert entries[0].meta["key"] == "2026-01-01:100000000001:111.11"


def test_the_key_zero_pads_a_whole_peso_amount_to_two_decimals() -> None:
    """``amount:.2f`` is the contract; ``str(amount)`` would write ``100`` and break dedup.

    Every fixture amount carries centavos, so this synthetic whole-peso movement is
    what pins the zero-padded two-decimal shape through the public API.
    """
    movement = _movement(
        "100",
        "Transferencia enviada Plugh",
        MovementKind.TRANSFER_SENT,
        operation_id="1",
        date=dt.date(2026, 1, 1),
    )

    entries = build_entries(_resumen(movement), "P1", _StubMap({}))

    assert entries[0].meta["key"] == "2026-01-01:1:100.00"


def test_the_reader_seam_supplies_the_statement_text() -> None:
    calls: list[str] = []

    def reader(path: str) -> str:
        calls.append(path)
        return COMPACT

    importer = MercadoPagoImporter(counterparty_map=_StubMap({}), text_reader=reader)
    importer.extract("/statements/MercadoPago/P1/statement.pdf", [])
    assert calls == ["/statements/MercadoPago/P1/statement.pdf"]


def test_a_reader_failure_is_wrapped_without_leaking_the_path() -> None:
    """A reader error carries the real path; ``extract`` must replace it with the class."""
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> str:
        raise FileNotFoundError(sensitive)

    importer = MercadoPagoImporter(text_reader=boom)

    with pytest.raises(StatementReadError) as excinfo:
        importer.extract("/statements/MercadoPago/P1/statement.pdf", [])

    message = str(excinfo.value)
    for token in (sensitive, "statement.pdf", "Nombre", "Apellido", "/statements/MercadoPago"):
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

    importer = MercadoPagoImporter(text_reader=boom)
    caught: StatementReadError | None = None
    try:
        importer.extract("/statements/MercadoPago/P1/synthetic.pdf", [])
    except StatementReadError as exc:
        caught = exc
        rendered = "".join(traceback.format_exception(exc))

    assert caught is not None
    assert caught.__suppress_context__ is True
    assert caught.__cause__ is None
    for token in (sensitive, "Nombre", "Apellido"):
        assert token not in rendered, (token, rendered)


# ------------------------------------------------------------------ map resolution


def test_expense_and_internal_destinations_use_the_account_part_verbatim(ledger) -> None:
    """A real ``CounterpartyMap`` resolves both destination prefixes.

    The map value's marker says inside/outside the ledger; the account after it is
    used verbatim. Only the marker is removed: a naive split on every ``:`` would
    truncate ``internal:Assets:MercadoPago:P2:Caja`` to ``Assets``.
    """
    counterparties = CounterpartyMap()
    counterparties.record(SOURCE, "Zxqv Uno", "expense:Expenses:Supermercado")
    counterparties.record(SOURCE, "Zxqv Dos", "internal:Assets:MercadoPago:P2:Caja")

    entries = build_entries(
        _resumen(
            _movement("-10.00", "Pago con Zxqv Uno", MovementKind.PURCHASE, operation_id="1"),
            _movement("-20.00", "Pago con Zxqv Dos", MovementKind.PURCHASE, operation_id="2"),
        ),
        "P1",
        counterparties,
    )

    assert entries[0].postings[1].account == "Expenses:Supermercado"
    assert entries[0].postings[1].units.number == Decimal("10.00")
    assert entries[0].payee == "Zxqv Uno"
    assert entries[1].postings[1].account == "Assets:MercadoPago:P2:Caja"
    assert entries[1].postings[1].units.number == Decimal("20.00")
    assert entries[1].payee == "Zxqv Dos"


def test_unclassified_counterparties_group_by_unique_raw_name() -> None:
    """Two movements naming one counterparty produce one row, and the count is 1."""
    first = _movement(
        "-100.00",
        "Pago con Zxqv Uno",
        MovementKind.PURCHASE,
        operation_id="111",
        date=dt.date(2026, 1, 2),
        line=10,
    )
    second = _movement(
        "-200.00",
        "Pago con Zxqv Uno",
        MovementKind.PURCHASE,
        operation_id="222",
        date=dt.date(2026, 1, 3),
        line=20,
    )

    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(_resumen(first, second), "P1", _StubMap({}))

    assert str(excinfo.value) == (
        "1 counterparties are not classified.\n"
        '  2026-01-02  -100.00 ARS  "Pago con Zxqv Uno"  "Zxqv Uno"\n'
        "    append to counterparties.tsv:\n"
        "      MercadoPago\tZxqv Uno\texpense:<category>"
    )


def test_unclassified_refusal_matches_the_pinned_shape_for_several_names() -> None:
    """Full message equality, including the two/4/6 indentation and the literal tab."""
    movements = (
        _movement(
            "-25000.00",
            "Pago con Zxqv Uno",
            MovementKind.PURCHASE,
            operation_id="aaa",
            date=dt.date(2026, 4, 2),
            line=5,
        ),
        _movement(
            "-40000.00",
            "Pago Zxqv Servicio",
            MovementKind.BILL_PAYMENT,
            operation_id="bbb",
            date=dt.date(2026, 4, 5),
            line=9,
        ),
    )

    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(_resumen(*movements), "P1", _StubMap({}))

    assert str(excinfo.value) == (
        "2 counterparties are not classified.\n"
        '  2026-04-02  -25,000.00 ARS  "Pago con Zxqv Uno"  "Zxqv Uno"\n'
        "    append to counterparties.tsv:\n"
        "      MercadoPago\tZxqv Uno\texpense:<category>\n"
        '  2026-04-05  -40,000.00 ARS  "Pago Zxqv Servicio"  "Zxqv Servicio"\n'
        "    append to counterparties.tsv:\n"
        "      MercadoPago\tZxqv Servicio\texpense:<category>"
    )


def test_an_empty_raw_name_is_refused_with_a_masked_actionable_error() -> None:
    movement = _movement("-10.00", "Pago con", MovementKind.PURCHASE, line=7)

    with pytest.raises(ResumenParseError) as excinfo:
        build_entries(_resumen(movement), "P1", _StubMap({}))

    message = str(excinfo.value)
    assert "movement 1" in message and "line 7" in message
    assert "Pago con" not in message


def test_an_unknown_kind_is_refused_defensively() -> None:
    movement = _movement("-10.00", "Qwertyzacion Zxqv", MovementKind.UNKNOWN, line=3)

    with pytest.raises(ResumenParseError) as excinfo:
        build_entries(_resumen(movement), "P1", _StubMap({}))

    assert "movement 1" in str(excinfo.value) and "line 3" in str(excinfo.value)


def test_a_reconciliation_failure_propagates_untouched() -> None:
    """A repeated natural key is already refused by the parser; extract must not mask it."""
    mutated = COMPACT.replace("100000000003", "100000000002", 1)
    importer = MercadoPagoImporter(text_reader=lambda _path: mutated)

    with pytest.raises(ReconciliationError) as excinfo:
        importer.extract("/statements/MercadoPago/P1/statement.pdf", [])

    assert "keys-unique" in {check.name for check in excinfo.value.failures}


# ------------------------------------------------------------------------- identify


def test_identify_claims_a_file_carrying_the_marker() -> None:
    importer = MercadoPagoImporter(text_reader=lambda _path: COMPACT)
    assert importer.identify("/tmp/not-a-real-file.pdf") is True


def test_identify_returns_false_without_the_marker() -> None:
    importer = MercadoPagoImporter(text_reader=lambda _path: "unrelated prose")
    assert importer.identify("/tmp/not-a-real-file.pdf") is False


def test_identify_returns_false_when_extraction_raises() -> None:
    def boom(_path: str) -> str:
        raise OSError("unreadable for the test")

    assert MercadoPagoImporter(text_reader=boom).identify("/tmp/whatever.pdf") is False


def test_pipeline_refusal_does_not_carry_the_sensitive_path(ledger: LedgerPaths) -> None:
    """The CLI prints ``importer-raised``; the reader's path and filename must not ride in it.

    ``identify`` succeeds on the first read (its failure path returns False); the
    ``extract`` read then fails, which is the path whose message the CLI prints.
    """
    _write_ledger(ledger)
    statement_dir = ledger.statements_dir() / "MercadoPago" / "P1"
    statement_dir.mkdir(parents=True)
    statement = statement_dir / "synthetic.pdf"
    statement.write_text("synthetic placeholder\n", encoding="utf-8")

    sensitive = "/sensitive/Nombre Apellido/statement.pdf"
    calls = {"count": 0}

    def reader(_path: str) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            return COMPACT
        raise FileNotFoundError(sensitive)

    importer = MercadoPagoImporter(text_reader=reader)

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.extract(ledger, "MercadoPago", statement, importers=[importer])

    assert excinfo.value.reason == pipeline.IMPORTER_RAISED
    message = str(excinfo.value)
    for token in (sensitive, "statement.pdf", "Nombre", "Apellido"):
        assert token not in message, (token, message)
    assert "FileNotFoundError" in message


# ------------------------------------------------------------------- registration


def test_get_importers_registers_mercado_pago_without_a_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    importers = get_importers()
    assert [importer.name for importer in importers] == ["MercadoPago", "Provincia", "ProvinciaVisa"]


def test_importing_the_importer_package_does_not_import_pypdfium2() -> None:
    script = (
        "import sys\n"
        "import expensuchis.importers\n"
        "import expensuchis.importers.mercadopago_importer\n"
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
    "2020-01-01 open Assets:MercadoPago:P1:Caja\n"
    "2020-01-01 open Income:P1:Rendimientos\n"
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
    statement_dir = ledger.statements_dir() / "MercadoPago" / "P1"
    statement_dir.mkdir(parents=True)
    statement = statement_dir / "statement.pdf"
    statement.write_text("synthetic placeholder; the text reader is injected\n", encoding="utf-8")

    importer = MercadoPagoImporter(text_reader=lambda _path: COMPACT)
    first = pipeline.extract(ledger, "MercadoPago", statement, importers=[importer])

    proposed = (ledger.staging_batch(first.batch_id) / "proposed.beancount").read_text(
        encoding="utf-8"
    )
    assert "key:" in proposed
    assert "2026-01-01:100000000001:111.11" in proposed
    assert first.entries == 5
    assert first.skipped == 0

    pipeline.approve(ledger, first.batch_id)
    pipeline.append(ledger, first.batch_id)
    assert pipeline.ledger_errors(ledger) == []

    second = pipeline.extract(ledger, "MercadoPago", statement, importers=[importer])
    assert second.entries == 0
    assert second.skipped == 5
    assert (ledger.staging_batch(second.batch_id) / "proposed.beancount").read_bytes() == b""


# ------------------------------------------------------------------------ pdfium


def _build_pdf(pages: list[list[str]]) -> bytes:
    """Build a minimal text-layer PDF with one text object per line."""
    font_id = 3
    page_ids: list[int] = []
    content_ids: list[int] = []
    next_id = 4
    for _ in pages:
        content_ids.append(next_id)
        next_id += 1
        page_ids.append(next_id)
        next_id += 1
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for page_lines, page_id, content_id in zip(pages, page_ids, content_ids):
        content = b"BT /F1 12 Tf 20 750 Td 14 TL\n"
        for index, line in enumerate(page_lines):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content += (f"({escaped}) Tj\n" if index == 0 else f"T*\n({escaped}) Tj\n").encode()
        content += b"ET"
        objects[content_id] = (
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode()
    out = b"%PDF-1.4\n"
    for object_id in sorted(objects):
        out += f"{object_id} 0 obj ".encode() + objects[object_id] + b"\nendobj\n"
    return out + b"trailer << /Root 1 0 R >>\n%%EOF\n"


@pytest.mark.pdfium
@pytest.mark.skipif(
    not os.environ.get("EXPENSUCHIS_TEST_PDFIUM"),
    reason="opt-in: set EXPENSUCHIS_TEST_PDFIUM=1 to exercise the real reader",
)
def test_read_pdf_reads_a_synthetic_statement_and_identify_claims_it(tmp_path: Path) -> None:
    from expensuchis.importers.pdf import read_pdf

    path = tmp_path / "synthetic.pdf"
    path.write_bytes(
        _build_pdf(
            [
                ["RESUMEN DE CUENTA EN PESOS", "synthetic page one"],
                ["synthetic page two", "synthetic body line"],
            ]
        )
    )

    text, pages = read_pdf(path)

    assert pages == 2
    assert "\f" in text
    assert "RESUMEN DE CUENTA EN PESOS" in text
    assert "synthetic page one" in text and "synthetic page two" in text
    assert MercadoPagoImporter().identify(str(path)) is True
