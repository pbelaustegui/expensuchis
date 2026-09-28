"""Tests for the Brubank importer wiring (T-08b).

The parser's own tests (``test_brubank_parser.py``) prove it reads the format and
refuses a bad document. These prove the *ledger* side: the per-person path
declares the cash account, each movement kind posts where the design says, the
natural key is the parser's identity, and an unclassified counterparty stops the
import with the pinned refusal shape.

Unlike Provincia and Mercado Pago, Brubank has no movement verb vocabulary: every
:class:`~expensuchis.importers.brubank.MovementKind.ORDINARY` row — credit or
debit alike — is name-matched through the counterparty map, never verb-parsed.
Only :class:`~expensuchis.importers.brubank.MovementKind.INTEREST` and
:class:`~expensuchis.importers.brubank.MovementKind.INTERNAL_TRANSFER` are
structurally distinguished and post to a fixed account.

Everything is synthetic, and both fixtures used here (``minimal.txt``,
``multipage.txt``) are the same ones the parser's own suite already exercises.
No test reads a real statement, and the text reader is always injected, so the
default run needs neither ``pypdfium2`` nor any private file.
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
from expensuchis.importers.brubank import (
    Movement,
    MovementKind,
    ReconciliationError,
    Resumen,
    ResumenParseError,
)
from expensuchis.importers.brubank_importer import (
    SOURCE,
    BrubankImporter,
    CounterpartyClassificationError,
    SourceAccountError,
    StatementReadError,
    build_entries,
    derive_person,
)
from expensuchis.ledger import ENV_VAR
from expensuchis.paths import LedgerPaths

FIXTURES = Path(__file__).parent / "fixtures" / "brubank"
MINIMAL = (FIXTURES / "minimal.txt").read_text(encoding="utf-8")
MULTIPAGE = (FIXTURES / "multipage.txt").read_text(encoding="utf-8")

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
    kind: MovementKind = MovementKind.ORDINARY,
    *,
    reference: str = "1234567890",
    date: dt.date = dt.date(2026, 1, 1),
    line: int = 1,
    transfer_bank: str = "",
) -> Movement:
    return Movement(
        date=date,
        reference=reference,
        description=description,
        amount=Decimal(amount),
        balance=Decimal("0.00"),
        page=1,
        line=line,
        kind=kind,
        transfer_bank=transfer_bank,
    )


def _resumen(*movements: Movement) -> Resumen:
    return Resumen(
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2026, 12, 31),
        opening=Decimal("0.00"),
        closing=Decimal("0.00"),
        declared_credits=Decimal("0.00"),
        declared_debits=Decimal("0.00"),
        financial_transactions_tax=Decimal("0.00"),
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
    importer = BrubankImporter()
    assert derive_person("/statements/Brubank/P1/statement.pdf") == "P1"
    assert importer.account("/statements/Brubank/P1/statement.pdf") == (
        "Assets:Brubank:P1:Caja"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/statements/Brubank/statement.pdf",  # missing person folder
        "/statements/Brubank/P1/nested/statement.pdf",  # extra nesting after person
        "/statements/Brubank/a:b/statement.pdf",
        "/statements/Brubank/a b/statement.pdf",
        "/statements/Brubank/./statement.pdf",
        "/statements/Brubank/../statement.pdf",
        "/statements/Brubank//statement.pdf",  # empty token
        "/statements/OtherSource/P1/statement.pdf",  # no Brubank segment
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
    assert "Brubank/<person>/<file>.pdf" in message
    assert "Assets:Brubank:<person>:Caja" in message


def test_the_importer_account_is_the_derived_cash_account() -> None:
    assert BrubankImporter().account("/s/Brubank/P3/x.pdf") == "Assets:Brubank:P3:Caja"


# ------------------------------------------------------------------- posting table


def test_every_kind_posts_to_its_decided_account() -> None:
    """The two fixtures, checked entry by entry: accounts, signs, keys, payees.

    ``Intereses pagados`` posts to ``Income:<person>:Intereses`` and
    ``De una cuenta tuya - BBVA`` to the clearing account, regardless of the named
    bank; every other row is name-matched through the counterparty map, credit or
    debit alike.
    """
    mapping = {
        "Persona Ejemplo": "internal:Assets:Provincia:P2:Caja",
        "Aguas Ejemplo": "expense:Expenses:Servicios",
        "Electrica Ejemplo": "expense:Expenses:Servicios",
    }
    expected_minimal = [
        (
            "2026-09-01",
            "Persona Ejemplo",
            "Persona Ejemplo",
            "2026-09-01:1234567890:3500.00",
            (
                ("Assets:Brubank:P1:Caja", "3500.00", "ARS"),
                ("Assets:Provincia:P2:Caja", "-3500.00", "ARS"),
            ),
        ),
        (
            "2026-09-02",
            "Aguas Ejemplo",
            "Aguas Ejemplo",
            "2026-09-02:2345678901:-1200.50",
            (
                ("Assets:Brubank:P1:Caja", "-1200.50", "ARS"),
                ("Expenses:Servicios", "1200.50", "ARS"),
            ),
        ),
    ]
    expected_multipage = [
        (
            "2026-08-31",
            "Persona Ejemplo",
            "Persona Ejemplo",
            "2026-08-31:3141592653:9876.54",
            (
                ("Assets:Brubank:P1:Caja", "9876.54", "ARS"),
                ("Assets:Provincia:P2:Caja", "-9876.54", "ARS"),
            ),
        ),
        (
            "2026-09-01",
            "Aguas Ejemplo",
            "Aguas Ejemplo",
            "2026-09-01:2718281828:-1234.56",
            (
                ("Assets:Brubank:P1:Caja", "-1234.56", "ARS"),
                ("Expenses:Servicios", "1234.56", "ARS"),
            ),
        ),
        (
            "2026-09-05",
            "Brubank",
            "De una cuenta tuya - BBVA",
            "2026-09-05:1618033988:2461.83",
            (
                ("Assets:Brubank:P1:Caja", "2461.83", "ARS"),
                ("Assets:TransferenciaEnTransito", "-2461.83", "ARS"),
            ),
        ),
        (
            "2026-09-10",
            "Brubank",
            "Intereses pagados",
            "2026-09-10:1414213562:321.45",
            (
                ("Assets:Brubank:P1:Caja", "321.45", "ARS"),
                ("Income:P1:Intereses", "-321.45", "ARS"),
            ),
        ),
        (
            "2026-09-20",
            "Electrica Ejemplo",
            "Electrica Ejemplo",
            "2026-09-20:2236067977:-678.90",
            (
                ("Assets:Brubank:P1:Caja", "-678.90", "ARS"),
                ("Expenses:Servicios", "678.90", "ARS"),
            ),
        ),
    ]
    for text, expected in ((MINIMAL, expected_minimal), (MULTIPAGE, expected_multipage)):
        importer = BrubankImporter(
            counterparty_map=_StubMap(mapping),
            text_reader=lambda _path, _text=text: _text,
        )
        entries = importer.extract("/statements/Brubank/P1/statement.pdf", [])
        assert _normalize(entries) == expected


_MINIMAL_MAPPING = {
    "Persona Ejemplo": "expense:Expenses:Otros",
    "Aguas Ejemplo": "expense:Expenses:Servicios",
}


def test_entry_meta_carries_only_the_key() -> None:
    entries = BrubankImporter(
        counterparty_map=_StubMap(_MINIMAL_MAPPING),
        text_reader=lambda _path: MINIMAL,
    ).extract("/statements/Brubank/P1/statement.pdf", [])
    assert all(set(entry.meta) == {"key"} for entry in entries)
    assert entries[0].meta["key"] == "2026-09-01:1234567890:3500.00"


def test_the_reader_seam_supplies_the_statement_text() -> None:
    calls: list[str] = []

    def reader(path: str) -> str:
        calls.append(path)
        return MINIMAL

    importer = BrubankImporter(
        counterparty_map=_StubMap(_MINIMAL_MAPPING),
        text_reader=reader,
    )
    importer.extract("/statements/Brubank/P1/statement.pdf", [])
    assert calls == ["/statements/Brubank/P1/statement.pdf"]


def test_a_reader_failure_is_wrapped_without_leaking_the_path() -> None:
    """A reader error carries the real path; ``extract`` must replace it with the class."""
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> str:
        raise FileNotFoundError(sensitive)

    importer = BrubankImporter(text_reader=boom)

    with pytest.raises(StatementReadError) as excinfo:
        importer.extract("/statements/Brubank/P1/statement.pdf", [])

    message = str(excinfo.value)
    for token in (sensitive, "statement.pdf", "Nombre", "Apellido", "/statements/Brubank"):
        assert token not in message, (token, message)
    assert "FileNotFoundError" in message
    assert excinfo.value.__cause__ is None


def test_the_reader_failure_suppresses_its_context_in_a_traceback() -> None:
    """``from None`` is load-bearing: the suppressed context holds the real path."""
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> str:
        raise FileNotFoundError(sensitive)

    importer = BrubankImporter(text_reader=boom)
    caught: StatementReadError | None = None
    try:
        importer.extract("/statements/Brubank/P1/synthetic.pdf", [])
    except StatementReadError as exc:
        caught = exc
        rendered = "".join(traceback.format_exception(exc))

    assert caught is not None
    assert caught.__suppress_context__ is True
    assert caught.__cause__ is None
    for token in (sensitive, "Nombre", "Apellido"):
        assert token not in rendered, (token, rendered)


def test_sort_orders_by_date_then_by_key() -> None:
    """Exercises the importer's own ``sort()`` in isolation from parsing.

    The base ``Importer.sort`` would raise ``KeyError`` on ``meta["lineno"]``,
    which these entries never carry (see :meth:`BrubankImporter.sort`).
    """
    mapping = {"Persona Ejemplo": "expense:Expenses:Otros"}
    later = build_entries(
        _resumen(
            _movement(
                "10.00", "Persona Ejemplo", reference="0000000001", date=dt.date(2026, 1, 2)
            )
        ),
        "P1",
        _StubMap(mapping),
    )[0]
    earlier = build_entries(
        _resumen(
            _movement(
                "10.00", "Persona Ejemplo", reference="0000000002", date=dt.date(2026, 1, 1)
            )
        ),
        "P1",
        _StubMap(mapping),
    )[0]
    entries = [later, earlier]
    BrubankImporter().sort(entries)
    assert [entry.date for entry in entries] == [dt.date(2026, 1, 1), dt.date(2026, 1, 2)]


# ------------------------------------------------------------------ map resolution


def test_expense_and_internal_destinations_use_the_account_part_verbatim(
    ledger: LedgerPaths,
) -> None:
    """A real ``CounterpartyMap`` resolves both destination prefixes, credit or debit.

    The map value's marker says inside/outside the ledger; the account after it is
    used verbatim regardless of the movement's own sign, unlike Provincia and
    Mercado Pago, where the map only ever sees debits.
    """
    counterparties = CounterpartyMap()
    counterparties.record(SOURCE, "Aguas Ejemplo", "expense:Expenses:Supermercado")
    counterparties.record(SOURCE, "Persona Ejemplo", "internal:Assets:Provincia:P2:Caja")

    entries = build_entries(
        _resumen(
            _movement("-10.00", "Aguas Ejemplo", reference="1"),
            _movement("20.00", "Persona Ejemplo", reference="2"),
        ),
        "P1",
        counterparties,
    )

    assert entries[0].postings[1].account == "Expenses:Supermercado"
    assert entries[0].postings[1].units.number == Decimal("10.00")
    assert entries[0].payee == "Aguas Ejemplo"
    assert entries[1].postings[1].account == "Assets:Provincia:P2:Caja"
    assert entries[1].postings[1].units.number == Decimal("-20.00")
    assert entries[1].payee == "Persona Ejemplo"


def test_unclassified_counterparties_group_by_unique_description() -> None:
    """Two movements naming one counterparty produce one row, and the count is 1."""
    first = _movement(
        "-100.00", "Aguas Ejemplo", reference="0000000001", date=dt.date(2026, 1, 2), line=10
    )
    second = _movement(
        "-200.00", "Aguas Ejemplo", reference="0000000002", date=dt.date(2026, 1, 3), line=20
    )

    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(_resumen(first, second), "P1", _StubMap({}))

    assert str(excinfo.value) == (
        "1 counterparties are not classified.\n"
        '  2026-01-02  -100.00 ARS  "Aguas Ejemplo"  "Aguas Ejemplo"\n'
        "    append to counterparties.tsv:\n"
        "      Brubank\tAguas Ejemplo\texpense:<category>"
    )


def test_unclassified_refusal_matches_the_pinned_shape_for_several_names() -> None:
    """Full message equality, including the two/4/6 indentation and the literal tab."""
    movements = (
        _movement(
            "-25000.00", "Aguas Ejemplo", reference="0000000001", date=dt.date(2026, 4, 2), line=5
        ),
        _movement(
            "40000.00", "Persona Ejemplo", reference="0000000002", date=dt.date(2026, 4, 5), line=9
        ),
    )

    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(_resumen(*movements), "P1", _StubMap({}))

    assert str(excinfo.value) == (
        "2 counterparties are not classified.\n"
        '  2026-04-02  -25,000.00 ARS  "Aguas Ejemplo"  "Aguas Ejemplo"\n'
        "    append to counterparties.tsv:\n"
        "      Brubank\tAguas Ejemplo\texpense:<category>\n"
        '  2026-04-05  40,000.00 ARS  "Persona Ejemplo"  "Persona Ejemplo"\n'
        "    append to counterparties.tsv:\n"
        "      Brubank\tPersona Ejemplo\texpense:<category>"
    )


def test_a_reconciliation_failure_propagates_untouched() -> None:
    """A parser reconciliation failure is already refused; extract must not mask it.

    Mutates the first movement's running balance only — a value that appears
    nowhere in the header block — so the header itself stays internally
    consistent and the failure is genuinely the running-balance-chain check,
    not a header parse failure.
    """
    mutated = MINIMAL.replace("13.973,19", "13.973,20", 1)
    importer = BrubankImporter(text_reader=lambda _path: mutated)

    with pytest.raises(ReconciliationError) as excinfo:
        importer.extract("/statements/Brubank/P1/statement.pdf", [])

    assert excinfo.value.failures


def test_a_structural_parse_failure_propagates_untouched() -> None:
    importer = BrubankImporter(text_reader=lambda _path: "not a Brubank statement at all")

    with pytest.raises(ResumenParseError):
        importer.extract("/statements/Brubank/P1/statement.pdf", [])


# ------------------------------------------------------------------------- identify


def test_identify_claims_a_file_carrying_the_marker() -> None:
    importer = BrubankImporter(text_reader=lambda _path: MINIMAL)
    assert importer.identify("/tmp/not-a-real-file.pdf") is True


def test_identify_returns_false_without_the_marker() -> None:
    importer = BrubankImporter(text_reader=lambda _path: "unrelated prose")
    assert importer.identify("/tmp/not-a-real-file.pdf") is False


def test_identify_returns_false_when_extraction_raises() -> None:
    def boom(_path: str) -> str:
        raise OSError("unreadable for the test")

    assert BrubankImporter(text_reader=boom).identify("/tmp/whatever.pdf") is False


def test_pipeline_refusal_does_not_carry_the_sensitive_path(ledger: LedgerPaths) -> None:
    """The CLI prints ``importer-raised``; the reader's path and filename must not ride in it."""
    _write_ledger(ledger)
    statement_dir = ledger.statements_dir() / "Brubank" / "P1"
    statement_dir.mkdir(parents=True)
    statement = statement_dir / "synthetic.pdf"
    statement.write_text("synthetic placeholder\n", encoding="utf-8")

    sensitive = "/sensitive/Nombre Apellido/statement.pdf"
    calls = {"count": 0}

    def reader(_path: str) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            return MINIMAL
        raise FileNotFoundError(sensitive)

    importer = BrubankImporter(text_reader=reader)

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.extract(ledger, "Brubank", statement, importers=[importer])

    assert excinfo.value.reason == pipeline.IMPORTER_RAISED
    message = str(excinfo.value)
    for token in (sensitive, "statement.pdf", "Nombre", "Apellido"):
        assert token not in message, (token, message)
    assert "FileNotFoundError" in message


# ------------------------------------------------------------------- registration


def test_the_brubank_importer_is_registered_without_a_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    importers = get_importers()
    names = [importer.name for importer in importers]
    assert names == ["MercadoPago", "Provincia", "ProvinciaVisa", "Brubank", "BBVA", "BBVACard"]
    assert isinstance(importers[3], BrubankImporter)


def test_importing_the_importer_package_does_not_import_pypdfium2() -> None:
    script = (
        "import sys\n"
        "import expensuchis.importers\n"
        "import expensuchis.importers.brubank_importer\n"
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
    "2020-01-01 open Assets:Brubank:P1:Caja\n"
    "2020-01-01 open Income:P1:Intereses\n"
)

PLACEHOLDER = ";; placeholder so the include glob matches an empty ledger\n"


def _write_ledger(paths: LedgerPaths) -> None:
    paths.main().write_text(MAIN_TEMPLATE, encoding="utf-8")
    paths.accounts().write_text(ACCOUNTS, encoding="utf-8")
    (paths.transactions_dir() / "placeholder.beancount").write_text(PLACEHOLDER, encoding="utf-8")


def test_pipeline_extract_stages_keys_and_a_second_run_reports_them_skipped(
    ledger: LedgerPaths,
) -> None:
    """Uses only the interest row (fixed destination), so no counterparty map is needed."""
    text = (
        "Resumen de Movimientos\n"
        "Saldo Inicial $ 0,00\n"
        "Saldo Final $ 321,45\n"
        "Créditos $ 321,45\n"
        "Débitos $ 0,00\n"
        "Imp. Trans. Financieras $ 0,00\n"
        "Fecha #Ref Descripción Débito Crédito Saldo\n"
        "10-09-26 1414213562 Intereses pagados - $ 321,45 $ 321,45\n"
        "Período 10 Sep 2026 al 10 Sep 2026\n"
    )
    _write_ledger(ledger)
    statement_dir = ledger.statements_dir() / "Brubank" / "P1"
    statement_dir.mkdir(parents=True)
    statement = statement_dir / "statement.pdf"
    statement.write_text("synthetic placeholder; the text reader is injected\n", encoding="utf-8")

    importer = BrubankImporter(text_reader=lambda _path: text)
    first = pipeline.extract(ledger, "Brubank", statement, importers=[importer])

    proposed = (ledger.staging_batch(first.batch_id) / "proposed.beancount").read_text(
        encoding="utf-8"
    )
    assert "key:" in proposed
    assert "2026-09-10:1414213562:321.45" in proposed
    assert first.entries == 1
    assert first.skipped == 0

    pipeline.approve(ledger, first.batch_id)
    pipeline.append(ledger, first.batch_id)
    assert pipeline.ledger_errors(ledger) == []

    second = pipeline.extract(ledger, "Brubank", statement, importers=[importer])
    assert second.entries == 0
    assert second.skipped == 1
    assert (ledger.staging_batch(second.batch_id) / "proposed.beancount").read_bytes() == b""
