"""Tests for the BBVA account importer wiring (T-07b).

The parser's own tests (``test_bbva_parser.py``) prove it reads the format and
refuses a bad document. These prove the *ledger* side: the per-person path
declares the account family, each sub-account block maps to its ledger account
by kind + currency, each movement kind posts where the design says, the
natural key is the parser's identity, and an unclassified counterparty stops
the import with the pinned refusal shape.

Unlike the other importers here, one statement can carry more than one
sub-account, so there is no single fixed cash account: the ledger account is
resolved per movement from the block it came from (T-07b owner decision
2026-09-27, "kind + currency, with no private configuration"). The statement
also never prints its year, so the importer's own reader supplies the PDF's
``CreationDate`` as the parser's anchor, unless an explicit year override is
given.

Everything is synthetic, reusing the parser's own fixtures
(``full_statement.txt``, ``multi_block_quiescent.txt``) where they exercise
what this module needs; two more small synthetic statements are inlined here
for the account-mapping refusals the parser fixtures never had a reason to
carry. No test reads a real statement, and both the text and date readers are
always injected, so the default run needs neither ``pypdfium2`` nor any
private file.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

from expensuchis import pipeline
from expensuchis.importers import get_importers
from expensuchis.importers.bbva import (
    ExtractoConsolidadoParseError,
    ReconciliationError,
    movement_keys,
    parse_extracto_consolidado,
)
from expensuchis.importers.bbva_importer import (
    SOURCE,
    AccountMappingError,
    BBVAImporter,
    CounterpartyClassificationError,
    SourceAccountError,
    StatementReadError,
    build_entries,
    derive_person,
)
from expensuchis.ledger import ENV_VAR
from expensuchis.paths import LedgerPaths

FIXTURES = Path(__file__).parent / "fixtures" / "bbva"
FULL = (FIXTURES / "full_statement.txt").read_text(encoding="utf-8")
MULTI_BLOCK = (FIXTURES / "multi_block_quiescent.txt").read_text(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The close in both fixtures is "SALDO AL 20 DE SEPTIEMBRE"; this anchor
#: matches the one ``test_bbva_parser.py`` uses, so the resolved close date
#: (2026-09-20) and every movement date line up with the parser's own tests.
ANCHOR = dt.date(2026, 9, 25)

_DUPLICATE_KIND_CURRENCY = """\
BANCO BBVA ARGENTINA S.A.
EXTRACTO CONSOLIDADO
TITULAR FULANO EJEMPLO
CC $ 000-555555/5 CAJA DE AHORRO FINAL
FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO
SALDO ANTERIOR 100,00
20/09 SIN MOVIMIENTOS 100,00
SALDO AL 20 DE SEPTIEMBRE 100,00
TOTAL MOVIMIENTOS -0,00 0,00
LOS MOVIMIENTOS QUE GENERARON PERCEPCION DE IVA SE INFORMAN CON EL DETALLE DEL CREDITO
1 DE 2 - PAGINA 1 DE 1
CC $ 000-666666/6 CAJA DE AHORRO FINAL
FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO
SALDO ANTERIOR 200,00
20/09 SIN MOVIMIENTOS 200,00
SALDO AL 20 DE SEPTIEMBRE 200,00
TOTAL MOVIMIENTOS -0,00 0,00
LOS MOVIMIENTOS QUE GENERARON PERCEPCION DE IVA SE INFORMAN CON EL DETALLE DEL CREDITO
"""

_UNKNOWN_KIND_CURRENCY = """\
BANCO BBVA ARGENTINA S.A.
EXTRACTO CONSOLIDADO
TITULAR FULANO EJEMPLO
CC EUR 000-777777/7 CAJA DE AHORRO EUROS FINAL
FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO
SALDO ANTERIOR 50,00
20/09 SIN MOVIMIENTOS 50,00
SALDO AL 20 DE SEPTIEMBRE 50,00
TOTAL MOVIMIENTOS -0,00 0,00
LOS MOVIMIENTOS QUE GENERARON PERCEPCION DE IVA SE INFORMAN CON EL DETALLE DEL CREDITO
"""


class _StubMap:
    """A minimal stand-in for ``CounterpartyMap`` that records the lookups."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)
        self.calls: list[tuple[str, str]] = []

    def resolve(self, source: str, raw_name: str) -> str | None:
        self.calls.append((source, raw_name))
        return self._mapping.get(raw_name)


def _extracto(text: str = FULL, **kwargs):
    kwargs.setdefault("anchor", ANCHOR)
    return parse_extracto_consolidado(text, **kwargs)


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


def test_a_valid_path_derives_the_person() -> None:
    assert derive_person("/statements/Bbva/P1/statement.pdf") == "P1"


@pytest.mark.parametrize(
    "path",
    [
        "/statements/Bbva/statement.pdf",  # missing person folder
        "/statements/Bbva/P1/nested/statement.pdf",  # extra nesting after person
        "/statements/Bbva/a:b/statement.pdf",
        "/statements/Bbva/a b/statement.pdf",
        "/statements/Bbva/./statement.pdf",
        "/statements/Bbva/../statement.pdf",
        "/statements/Bbva//statement.pdf",  # empty token
        "/statements/OtherSource/P1/statement.pdf",  # no Bbva segment
        "/statements/BBVA/P1/statement.pdf",  # wrong casing: the real folder is 'Bbva'
    ],
)
def test_an_invalid_path_is_refused_without_echoing_it(path: str) -> None:
    with pytest.raises(SourceAccountError) as excinfo:
        derive_person(path)
    message = str(excinfo.value)
    assert path not in message
    assert Path(path).name not in message
    # The refusal names the convention the user must follow instead.
    assert "Bbva/<person>/<file>.pdf" in message
    assert "BBVA" in message


def test_the_importer_account_is_the_checking_account_for_the_person() -> None:
    assert BBVAImporter().account("/s/Bbva/P3/x.pdf") == "Assets:BBVA:P3:CuentaCorriente"


# ------------------------------------------------------------------- posting table


def test_every_kind_in_the_full_statement_posts_to_its_decided_account() -> None:
    """The full fixture's 10 movements, checked entry by entry.

    Debit-card purchases resolve by merchant, sent transfers by their joined
    recipient CUIT, and the received transfer by the name left after
    stripping 'transferencia inmediata' from its concept -- both the CUIT and
    the name are ordinary counterparty-map keys, one resolving to an outside
    expense, the other two to the shared clearing account (T-07 owner
    decision 4, 'own accounts').
    """
    mapping = {
        "COMERCIO EJEMPLO": "expense:Expenses:Supermercado",
        "KIOSCO EJEMPLO": "expense:Expenses:Kiosco",
        "20000000001": "expense:Expenses:Otros",
        "20000000002": "internal:Assets:TransferenciaEnTransito",
        "fulano": "internal:Assets:TransferenciaEnTransito",
    }
    extracto = _extracto(FULL)
    keys = movement_keys(extracto.movements)
    entries = build_entries(extracto, "P1", _StubMap(mapping))

    account = "Assets:BBVA:P1:CuentaCorriente"
    expected = [
        (
            "2026-09-05",
            "COMERCIO EJEMPLO",
            "pago con visa debito",
            keys[0],
            ((account, "-483.17", "ARS"), ("Expenses:Supermercado", "483.17", "ARS")),
        ),
        (
            "2026-09-06",
            "BBVA",
            "cuenta visa 58273941605837",
            keys[1],
            ((account, "-1183.46", "ARS"), ("Liabilities:BBVA:P1:Visa", "1183.46", "ARS")),
        ),
        (
            "2026-09-07",
            "BBVA",
            "cuenta mastercard 70491638527049",
            keys[2],
            (
                (account, "-764.33", "ARS"),
                ("Liabilities:BBVA:P1:Mastercard", "764.33", "ARS"),
            ),
        ),
        (
            "2026-09-08",
            "BBVA",
            "extraccion elec+cash",
            keys[3],
            ((account, "-317.89", "ARS"), ("Expenses:Efectivo", "317.89", "ARS")),
        ),
        (
            "2026-09-09",
            "BBVA",
            "pago haberes",
            keys[4],
            ((account, "5124.71", "ARS"), ("Income:P1:Sueldo", "-5124.71", "ARS")),
        ),
        (
            "2026-09-10",
            "KIOSCO EJEMPLO",
            "pago con visa debito",
            keys[5],
            ((account, "-237.94", "ARS"), ("Expenses:Kiosco", "237.94", "ARS")),
        ),
        (
            "2026-09-11",
            "BBVA",
            "intereses ganados",
            keys[6],
            ((account, "63.28", "ARS"), ("Income:P1:Intereses", "-63.28", "ARS")),
        ),
        (
            "2026-09-12",
            "20000000001",
            "transferencia",
            keys[7],
            ((account, "-1042.63", "ARS"), ("Expenses:Otros", "1042.63", "ARS")),
        ),
        (
            "2026-09-12",
            "20000000002",
            "transferencia juan 654321 5",
            keys[8],
            (
                (account, "-689.14", "ARS"),
                ("Assets:TransferenciaEnTransito", "689.14", "ARS"),
            ),
        ),
        (
            "2026-09-13",
            "fulano",
            "transferencia inmediata fulano",
            keys[9],
            (
                (account, "917.53", "ARS"),
                ("Assets:TransferenciaEnTransito", "-917.53", "ARS"),
            ),
        ),
    ]
    assert _normalize(entries) == expected


def test_the_multi_block_quiescent_statement_yields_no_entries() -> None:
    """Three sub-accounts, all quiescent: nothing to post, and the account
    mapping for all three kind/currency pairs still resolves without error."""
    extracto = _extracto(MULTI_BLOCK)
    entries = build_entries(extracto, "P1", _StubMap({}))
    assert entries == []


def test_entry_meta_carries_only_the_key() -> None:
    mapping = {
        "COMERCIO EJEMPLO": "expense:Expenses:Otros",
        "KIOSCO EJEMPLO": "expense:Expenses:Otros",
        "20000000001": "expense:Expenses:Otros",
        "20000000002": "expense:Expenses:Otros",
        "fulano": "expense:Expenses:Otros",
    }
    entries = build_entries(_extracto(FULL), "P1", _StubMap(mapping))
    assert all(set(entry.meta) == {"key"} for entry in entries)


# ------------------------------------------------------------- account mapping


def test_an_unknown_kind_currency_pair_refuses() -> None:
    extracto = _extracto(_UNKNOWN_KIND_CURRENCY)
    with pytest.raises(AccountMappingError) as excinfo:
        build_entries(extracto, "P1", _StubMap({}))
    message = str(excinfo.value)
    assert "cc/eur" in message
    assert "000-777777/7" not in message


def test_two_blocks_sharing_a_kind_and_currency_refuse() -> None:
    extracto = _extracto(_DUPLICATE_KIND_CURRENCY)
    with pytest.raises(AccountMappingError) as excinfo:
        build_entries(extracto, "P1", _StubMap({}))
    message = str(excinfo.value)
    assert "cc/$" in message
    assert "000-555555/5" not in message
    assert "000-666666/6" not in message


# ---------------------------------------------------------- transfer identity


def test_a_transfer_out_uses_its_recipient_cuit_as_the_map_key() -> None:
    extracto = _extracto(FULL)
    stub = _StubMap(
        {
            "COMERCIO EJEMPLO": "expense:Expenses:Otros",
            "KIOSCO EJEMPLO": "expense:Expenses:Otros",
            "20000000001": "expense:Expenses:Otros",
            "20000000002": "expense:Expenses:Otros",
            "fulano": "expense:Expenses:Otros",
        }
    )
    build_entries(extracto, "P1", stub)
    assert (SOURCE, "20000000001") in stub.calls
    assert (SOURCE, "20000000002") in stub.calls


def test_an_unclassified_transfer_out_redacts_the_cuit_in_the_refusal() -> None:
    extracto = _extracto(FULL)
    mapping = {
        "COMERCIO EJEMPLO": "expense:Expenses:Otros",
        "KIOSCO EJEMPLO": "expense:Expenses:Otros",
        "fulano": "expense:Expenses:Otros",
        # "20000000001" and "20000000002" deliberately absent.
    }
    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(extracto, "P1", _StubMap(mapping))
    message = str(excinfo.value)
    assert "20000000001" not in message
    assert "20000000002" not in message
    assert "<cuit>" in message
    assert "copy the exact value from the private ledger" in message


def test_an_unclassified_transfer_in_shows_its_stripped_name() -> None:
    extracto = _extracto(FULL)
    mapping = {
        "COMERCIO EJEMPLO": "expense:Expenses:Otros",
        "KIOSCO EJEMPLO": "expense:Expenses:Otros",
        "20000000001": "expense:Expenses:Otros",
        "20000000002": "expense:Expenses:Otros",
        # "fulano" deliberately absent.
    }
    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(extracto, "P1", _StubMap(mapping))
    message = str(excinfo.value)
    assert '"fulano"' in message
    assert "append to counterparties.tsv:" in message
    assert "BBVA\tfulano\texpense:<category>" in message


def test_a_received_transfer_with_no_name_after_the_verb_refuses_structurally() -> None:
    """'TRANSFERENCIA INMEDIATA' alone (no name) has no counterparty identity at all."""
    mutated = FULL.replace("TRANSFERENCIA INMEDIATA FULANO", "TRANSFERENCIA INMEDIATA", 1)
    extracto = _extracto(mutated)
    with pytest.raises(ExtractoConsolidadoParseError):
        build_entries(extracto, "P1", _StubMap({}))


# ------------------------------------------------------------ unclassified shape


def test_unclassified_debit_card_purchase_matches_the_pinned_shape() -> None:
    extracto = _extracto(FULL)
    mapping = {
        "KIOSCO EJEMPLO": "expense:Expenses:Otros",
        "20000000001": "expense:Expenses:Otros",
        "20000000002": "expense:Expenses:Otros",
        "fulano": "expense:Expenses:Otros",
        # "COMERCIO EJEMPLO" deliberately absent.
    }
    with pytest.raises(CounterpartyClassificationError) as excinfo:
        build_entries(extracto, "P1", _StubMap(mapping))
    assert str(excinfo.value) == (
        "1 counterparties are not classified.\n"
        '  2026-09-05  -483.17 ARS  "pago con visa debito"  "COMERCIO EJEMPLO"\n'
        "    append to counterparties.tsv:\n"
        "      BBVA\tCOMERCIO EJEMPLO\texpense:<category>"
    )


# --------------------------------------------------------------------- anchor date


def test_the_creation_date_reader_supplies_the_parser_anchor() -> None:
    calls: list[str] = []

    def date_reader(path: str) -> dt.date:
        calls.append(path)
        return ANCHOR

    importer = BBVAImporter(
        counterparty_map=_StubMap(
            {
                "COMERCIO EJEMPLO": "expense:Expenses:Otros",
                "KIOSCO EJEMPLO": "expense:Expenses:Otros",
                "20000000001": "expense:Expenses:Otros",
                "20000000002": "expense:Expenses:Otros",
                "fulano": "expense:Expenses:Otros",
            }
        ),
        text_reader=lambda _path: FULL,
        date_reader=date_reader,
    )
    entries = importer.extract("/statements/Bbva/P1/statement.pdf", [])
    assert calls == ["/statements/Bbva/P1/statement.pdf"]
    assert len(entries) == 10


def test_a_missing_creation_date_refuses_without_a_year_override() -> None:
    importer = BBVAImporter(text_reader=lambda _path: FULL, date_reader=lambda _path: None)
    with pytest.raises(StatementReadError) as excinfo:
        importer.extract("/statements/Bbva/P1/statement.pdf", [])
    assert "CreationDate" in str(excinfo.value)


def test_a_year_override_never_reads_the_creation_date() -> None:
    def boom(_path: str) -> dt.date:
        raise AssertionError("the date reader must not be called when year overrides it")

    importer = BBVAImporter(
        counterparty_map=_StubMap(
            {
                "COMERCIO EJEMPLO": "expense:Expenses:Otros",
                "KIOSCO EJEMPLO": "expense:Expenses:Otros",
                "20000000001": "expense:Expenses:Otros",
                "20000000002": "expense:Expenses:Otros",
                "fulano": "expense:Expenses:Otros",
            }
        ),
        text_reader=lambda _path: FULL,
        date_reader=boom,
        year=2026,
    )
    entries = importer.extract("/statements/Bbva/P1/statement.pdf", [])
    assert len(entries) == 10


def test_a_date_reader_failure_is_wrapped_without_leaking_the_path() -> None:
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> dt.date:
        raise FileNotFoundError(sensitive)

    importer = BBVAImporter(text_reader=lambda _path: FULL, date_reader=boom)
    with pytest.raises(StatementReadError) as excinfo:
        importer.extract("/statements/Bbva/P1/statement.pdf", [])
    message = str(excinfo.value)
    for token in (sensitive, "statement.pdf", "Nombre", "Apellido"):
        assert token not in message, (token, message)
    assert "FileNotFoundError" in message
    assert excinfo.value.__cause__ is None


# A minimal, hand-built PDF whose /Info dictionary carries a CreationDate. No
# statement text is needed for this reader, only the metadata.
_SYNTHETIC_PDF_WITH_CREATION_DATE = (
    b"%PDF-1.4\n"
    b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] >> endobj\n"
    b"4 0 obj << /CreationDate (D:20260115120000-03'00') >> endobj\n"
    b"trailer << /Root 1 0 R /Info 4 0 R >>\n%%EOF\n"
)

_PYPDFIUM2_AVAILABLE = importlib.util.find_spec("pypdfium2") is not None


@pytest.mark.pdfium
@pytest.mark.skipif(not _PYPDFIUM2_AVAILABLE, reason="pypdfium2 is not installed")
@pytest.mark.skipif(
    not os.environ.get("EXPENSUCHIS_TEST_PDFIUM"),
    reason="opt-in: set EXPENSUCHIS_TEST_PDFIUM=1 to exercise the real reader",
)
def test_read_creation_date_reads_a_synthetic_pdfs_metadata(tmp_path: Path) -> None:
    from expensuchis.importers.pdf import read_creation_date

    path = tmp_path / "synthetic.pdf"
    path.write_bytes(_SYNTHETIC_PDF_WITH_CREATION_DATE)

    assert read_creation_date(path) == dt.date(2026, 1, 15)


@pytest.mark.pdfium
@pytest.mark.skipif(not _PYPDFIUM2_AVAILABLE, reason="pypdfium2 is not installed")
@pytest.mark.skipif(
    not os.environ.get("EXPENSUCHIS_TEST_PDFIUM"),
    reason="opt-in: set EXPENSUCHIS_TEST_PDFIUM=1 to exercise the real reader",
)
def test_read_creation_date_returns_none_without_the_metadata_field(tmp_path: Path) -> None:
    from expensuchis.importers.pdf import read_creation_date

    path = tmp_path / "synthetic.pdf"
    path.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] >> endobj\n"
        b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )

    assert read_creation_date(path) is None


# ------------------------------------------------------------------ text reader


def test_the_text_reader_seam_supplies_the_statement_text() -> None:
    calls: list[str] = []

    def reader(path: str) -> str:
        calls.append(path)
        return FULL

    importer = BBVAImporter(
        counterparty_map=_StubMap(
            {
                "COMERCIO EJEMPLO": "expense:Expenses:Otros",
                "KIOSCO EJEMPLO": "expense:Expenses:Otros",
                "20000000001": "expense:Expenses:Otros",
                "20000000002": "expense:Expenses:Otros",
                "fulano": "expense:Expenses:Otros",
            }
        ),
        text_reader=reader,
        date_reader=lambda _path: ANCHOR,
    )
    importer.extract("/statements/Bbva/P1/statement.pdf", [])
    assert calls == ["/statements/Bbva/P1/statement.pdf"]


def test_a_text_reader_failure_is_wrapped_without_leaking_the_path() -> None:
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> str:
        raise FileNotFoundError(sensitive)

    importer = BBVAImporter(text_reader=boom)
    with pytest.raises(StatementReadError) as excinfo:
        importer.extract("/statements/Bbva/P1/statement.pdf", [])
    message = str(excinfo.value)
    for token in (sensitive, "statement.pdf", "Nombre", "Apellido"):
        assert token not in message, (token, message)
    assert "FileNotFoundError" in message
    assert excinfo.value.__cause__ is None


def test_the_text_reader_failure_suppresses_its_context_in_a_traceback() -> None:
    sensitive = "/sensitive/Nombre Apellido/statement.pdf"

    def boom(_path: str) -> str:
        raise FileNotFoundError(sensitive)

    importer = BBVAImporter(text_reader=boom)
    caught: StatementReadError | None = None
    try:
        importer.extract("/statements/Bbva/P1/synthetic.pdf", [])
    except StatementReadError as exc:
        caught = exc
        rendered = "".join(traceback.format_exception(exc))

    assert caught is not None
    assert caught.__suppress_context__ is True
    assert caught.__cause__ is None
    for token in (sensitive, "Nombre", "Apellido"):
        assert token not in rendered, (token, rendered)


# ------------------------------------------------------------------- reconciliation


def test_a_reconciliation_failure_propagates_untouched() -> None:
    mutated = FULL.replace("9.954,02", "9.954,03", 1)
    importer = BBVAImporter(text_reader=lambda _path: mutated, date_reader=lambda _path: ANCHOR)
    with pytest.raises(ReconciliationError) as excinfo:
        importer.extract("/statements/Bbva/P1/statement.pdf", [])
    assert excinfo.value.failures


def test_a_structural_parse_failure_propagates_untouched() -> None:
    importer = BBVAImporter(
        text_reader=lambda _path: "not a BBVA statement at all",
        date_reader=lambda _path: ANCHOR,
    )
    with pytest.raises(ExtractoConsolidadoParseError):
        importer.extract("/statements/Bbva/P1/statement.pdf", [])


# ------------------------------------------------------------------------- identify


def test_identify_claims_a_file_carrying_the_marker() -> None:
    importer = BBVAImporter(text_reader=lambda _path: FULL)
    assert importer.identify("/tmp/not-a-real-file.pdf") is True


def test_identify_returns_false_without_the_marker() -> None:
    importer = BBVAImporter(text_reader=lambda _path: "unrelated prose")
    assert importer.identify("/tmp/not-a-real-file.pdf") is False


def test_identify_returns_false_when_extraction_raises() -> None:
    def boom(_path: str) -> str:
        raise OSError("unreadable for the test")

    assert BBVAImporter(text_reader=boom).identify("/tmp/whatever.pdf") is False


def test_pipeline_refusal_does_not_carry_the_sensitive_path(ledger: LedgerPaths) -> None:
    _write_ledger(ledger)
    statement_dir = ledger.statements_dir() / "Bbva" / "P1"
    statement_dir.mkdir(parents=True)
    statement = statement_dir / "synthetic.pdf"
    statement.write_text("synthetic placeholder\n", encoding="utf-8")

    sensitive = "/sensitive/Nombre Apellido/statement.pdf"
    calls = {"count": 0}

    def reader(_path: str) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            return FULL
        raise FileNotFoundError(sensitive)

    importer = BBVAImporter(text_reader=reader, date_reader=lambda _path: ANCHOR)

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.extract(ledger, "BBVA", statement, importers=[importer])

    assert excinfo.value.reason == pipeline.IMPORTER_RAISED
    message = str(excinfo.value)
    for token in (sensitive, "statement.pdf", "Nombre", "Apellido"):
        assert token not in message, (token, message)
    assert "FileNotFoundError" in message


# ------------------------------------------------------------------- registration


def test_the_bbva_importer_is_registered_without_a_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    importers = get_importers()
    names = [importer.name for importer in importers]
    assert names == ["MercadoPago", "Provincia", "ProvinciaVisa", "Brubank", "BBVA"]
    assert isinstance(importers[-1], BBVAImporter)


def test_importing_the_importer_package_does_not_import_pypdfium2() -> None:
    script = (
        "import sys\n"
        "import expensuchis.importers\n"
        "import expensuchis.importers.bbva_importer\n"
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
    "2020-01-01 open Assets:BBVA:P1:CuentaCorriente\n"
    "2020-01-01 open Liabilities:BBVA:P1:Visa\n"
    "2020-01-01 open Liabilities:BBVA:P1:Mastercard\n"
    "2020-01-01 open Expenses:Efectivo\n"
    "2020-01-01 open Income:P1:Sueldo\n"
    "2020-01-01 open Income:P1:Intereses\n"
    "2020-01-01 open Expenses:Supermercado\n"
    "2020-01-01 open Expenses:Kiosco\n"
    "2020-01-01 open Expenses:Otros\n"
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
    statement_dir = ledger.statements_dir() / "Bbva" / "P1"
    statement_dir.mkdir(parents=True)
    statement = statement_dir / "statement.pdf"
    statement.write_text("synthetic placeholder; the text reader is injected\n", encoding="utf-8")

    mapping = {
        "COMERCIO EJEMPLO": "expense:Expenses:Supermercado",
        "KIOSCO EJEMPLO": "expense:Expenses:Kiosco",
        "20000000001": "expense:Expenses:Otros",
        "20000000002": "internal:Assets:TransferenciaEnTransito",
        "fulano": "internal:Assets:TransferenciaEnTransito",
    }
    importer = BBVAImporter(
        counterparty_map=_StubMap(mapping),
        text_reader=lambda _path: FULL,
        date_reader=lambda _path: ANCHOR,
    )
    first = pipeline.extract(ledger, "BBVA", statement, importers=[importer])

    proposed = (ledger.staging_batch(first.batch_id) / "proposed.beancount").read_text(
        encoding="utf-8"
    )
    assert "key:" in proposed
    assert first.entries == 10
    assert first.skipped == 0

    pipeline.approve(ledger, first.batch_id)
    pipeline.append(ledger, first.batch_id)
    assert pipeline.ledger_errors(ledger) == []

    second = pipeline.extract(ledger, "BBVA", statement, importers=[importer])
    assert second.entries == 0
    assert second.skipped == 10
    assert (ledger.staging_batch(second.batch_id) / "proposed.beancount").read_bytes() == b""
