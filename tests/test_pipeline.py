"""Tests for the import workflow contract (T-04b).

The pipeline is: identify -> extract -> human review -> approve -> append -> bean-check.
Every refusal path is pinned as a pure condition, so the gate cannot regress silently.
Every ledger lives under ``tmp_path``; no real ledger directory is ever touched.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from beancount import loader
from beancount.core import amount, data
from beancount.ops import validation
from beancount.parser import printer
from beangulp import Importer

from expensuchis import pipeline
from expensuchis.importers.mercadopago_importer import (
    CounterpartyClassificationError as MercadoPagoCounterpartyClassificationError,
)
from expensuchis.importers.provincia_importer import (
    CounterpartyClassificationError as ProvinciaCounterpartyClassificationError,
)
from expensuchis.importers.provincia_visa_importer import (
    CounterpartyClassificationError as ProvinciaVisaCounterpartyClassificationError,
)
from expensuchis.ledger import ENV_VAR
from expensuchis.paths import LedgerPaths
from expensuchis.redact import content_hash, load_or_create_key

REPO_ROOT = Path(__file__).resolve().parents[1]

MAIN_TEMPLATE = (
    'option "title" "Test ledger"\n'
    'option "operating_currency" "ARS"\n'
    'include "accounts.beancount"\n'
    'include "transactions/*.beancount"\n'
)

ACCOUNTS = (
    "2020-01-01 open Equity:Opening-Balances\n"
    "2020-01-01 open Assets:TransferenciaEnTransito\n"
    "2020-01-01 open Expenses:Otros\n"
    "2020-01-01 open Assets:Test:Caja\n"
)

PLACEHOLDER = ";; placeholder so the include glob matches an empty ledger\n"


class FakeImporter(Importer):
    """A minimal importer for exercising the pipeline seams."""

    def __init__(self, name, entries=None, suffix=".csv", error=None, match=True):
        self._name = name
        self._entries = list(entries or [])
        self._suffix = suffix
        self._error = error
        self._match = match

    @property
    def name(self):
        return self._name

    def identify(self, filepath):
        return self._match and str(filepath).endswith(self._suffix)

    def account(self, filepath):
        return "Assets:Test:Caja"

    def extract(self, filepath, existing):
        if self._error is not None:
            raise self._error
        return list(self._entries)


class _DeletingImporter(Importer):
    """An importer that deletes its own statement, then fails (regression: R4-001).

    Exercises the except handler's own hashing: by the time ``extract`` tries to
    name the statement by content hash, the file is already gone.
    """

    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    def identify(self, filepath):
        return str(filepath).endswith(".csv")

    def account(self, filepath):
        return "Assets:Test:Caja"

    def extract(self, filepath, existing):
        Path(filepath).unlink()
        raise ValueError("boom after deleting the statement")


def make_entry(
    date: dt.date,
    key: str | None,
    account: str = "Expenses:Otros",
    counterpart: str = "Assets:Test:Caja",
    value: str = "100.00",
    currency: str = "ARS",
    extra_meta: dict | None = None,
) -> data.Transaction:
    meta = {"filename": "<test>", "lineno": 1}
    if key is not None:
        meta["key"] = key
    if extra_meta:
        meta.update(extra_meta)
    postings = [
        data.Posting(account, amount.Amount(Decimal(value), currency), None, None, None, None),
        data.Posting(counterpart, amount.Amount(-Decimal(value), currency), None, None, None, None),
    ]
    return data.Transaction(
        meta, date, "*", "Payee", "Narration", frozenset(), frozenset(), postings
    )


FIXED_DT = SimpleNamespace(
    datetime=SimpleNamespace(now=lambda tz=None: dt.datetime(2026, 1, 1, tzinfo=dt.UTC)),
    UTC=dt.UTC,
)


def txn_text(date: dt.date, key: str, account: str = "Expenses:Otros") -> str:
    return (
        f'{date.isoformat()} * "Payee" "Narration"\n'
        f'  key: "{key}"\n'
        f"  {account}  100.00 ARS\n"
        f"  Assets:Test:Caja  -100.00 ARS\n"
    )


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LedgerPaths:
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))
    paths = LedgerPaths()
    paths.ensure()
    return paths


@pytest.fixture
def statement(tmp_path: Path) -> Path:
    path = tmp_path / "statement.csv"
    path.write_text("synthetic,content\n", encoding="utf-8")
    return path


def write_ledger(paths: LedgerPaths, months: dict[str, str] | None = None) -> None:
    paths.main().write_text(MAIN_TEMPLATE, encoding="utf-8")
    paths.accounts().write_text(ACCOUNTS, encoding="utf-8")
    month_files = months or {}
    if not month_files:
        (paths.transactions_dir() / "placeholder.beancount").write_text(
            PLACEHOLDER, encoding="utf-8"
        )
    for month, body in month_files.items():
        (paths.transactions_dir() / f"{month}.beancount").write_text(body, encoding="utf-8")


def stage(paths: LedgerPaths, entries, source: str = "Test") -> str:
    statement_path = paths.root / "statement.csv"
    statement_path.write_text("synthetic,content\n", encoding="utf-8")
    importer = FakeImporter("test.importer.Fake", entries=entries)
    result = pipeline.extract(paths, source, statement_path, importers=[importer])
    return result.batch_id


def staged(paths: LedgerPaths, entries, source: str = "Test") -> str:
    write_ledger(paths)
    return stage(paths, entries, source=source)


# --------------------------------------------------------------------------- staging


def test_extract_writes_the_three_staging_files(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    batch = ledger.staging_batch(batch_id)

    assert (batch / "proposed.beancount").is_file()
    assert (batch / "batch.json").is_file()
    assert (batch / "report.txt").is_file()

    meta = json.loads((batch / "batch.json").read_text(encoding="utf-8"))
    assert meta["source"] == "Test"
    assert meta["statement"] == "statement.csv"
    assert meta["entries"] == 1
    assert meta["skipped"] == 0
    assert len(meta["statement_sha256"]) == 64
    assert "created" in meta and meta["created"].endswith("+00:00")


def test_proposed_bytes_are_the_printers_own_bytes(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    entry = make_entry(dt.date(2026, 1, 5), "k1")
    batch_id = stage(ledger, [entry])
    expected = printer.format_entry(entry).encode("utf-8")
    assert (ledger.staging_batch(batch_id) / "proposed.beancount").read_bytes() == expected


def test_batch_id_is_source_timestamp_and_content_hash(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    proposed = (ledger.staging_batch(batch_id) / "proposed.beancount").read_bytes()
    digest = hashlib.sha256(proposed).hexdigest()[:8]

    assert batch_id.startswith("Test-")
    assert batch_id.endswith(f"-{digest}")
    timestamp = batch_id.split("-")[1]
    parsed = dt.datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    assert parsed.tzinfo is not None


def test_batch_id_is_a_single_staging_component(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    assert ledger.staging_batch(batch_id).parent == ledger.staging_dir()


def test_extract_refuses_an_entry_without_a_key(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    with pytest.raises(pipeline.PipelineError) as excinfo:
        stage(ledger, [make_entry(dt.date(2026, 1, 5), None)])
    assert excinfo.value.reason == pipeline.KEY_MISSING


def test_extract_refuses_an_entry_with_an_empty_key(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    with pytest.raises(pipeline.PipelineError) as excinfo:
        stage(ledger, [make_entry(dt.date(2026, 1, 5), "")])
    assert excinfo.value.reason == pipeline.KEY_MISSING


def test_extract_refuses_duplicate_keys_within_a_batch(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    entries = [
        make_entry(dt.date(2026, 1, 5), "dup"),
        make_entry(dt.date(2026, 1, 6), "dup"),
    ]
    with pytest.raises(pipeline.PipelineError) as excinfo:
        stage(ledger, entries)
    assert excinfo.value.reason == pipeline.KEY_DUPLICATE


def test_extract_drops_keys_already_in_the_ledger_and_counts_them(ledger: LedgerPaths) -> None:
    write_ledger(ledger, months={"2026-01": txn_text(dt.date(2026, 1, 1), "already")})
    batch_id = stage(
        ledger,
        [
            make_entry(dt.date(2026, 1, 1), "already"),
            make_entry(dt.date(2026, 1, 5), "fresh", value="250.00"),
        ],
    )
    batch = ledger.staging_batch(batch_id)
    meta = json.loads((batch / "batch.json").read_text(encoding="utf-8"))
    assert meta["skipped"] == 1
    assert meta["entries"] == 1
    assert "fresh" in (batch / "proposed.beancount").read_text(encoding="utf-8")
    assert "already" not in (batch / "proposed.beancount").read_text(encoding="utf-8")


def test_extract_refuses_to_overwrite_an_existing_batch(
    ledger: LedgerPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_ledger(ledger)
    monkeypatch.setattr(pipeline, "dt", FIXED_DT)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    proposed = ledger.staging_batch(batch_id) / "proposed.beancount"
    proposed.write_text(";; hand edit\n", encoding="utf-8")

    with pytest.raises(pipeline.PipelineError) as excinfo:
        stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    assert excinfo.value.reason == pipeline.BATCH_EXISTS
    assert proposed.read_text(encoding="utf-8") == ";; hand edit\n"


def test_dedup_index_reads_the_loader_not_the_raw_text(ledger: LedgerPaths) -> None:
    """A key that only appears in a comment must not count as present."""
    write_ledger(
        ledger,
        months={"2026-01": '; key: "ghost"  (this is a comment, not an entry)\n'},
    )
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "ghost")])
    meta = json.loads((ledger.staging_batch(batch_id) / "batch.json").read_text(encoding="utf-8"))
    assert meta["skipped"] == 0
    assert pipeline.build_key_index(ledger) == set()


def test_key_index_reads_main_and_append_refuses_a_key_written_there(
    ledger: LedgerPaths,
) -> None:
    """The index must cover everything bean-check sees, not just transactions/*

    A ``key`` hand-written into ``main.beancount`` itself is invisible to a glob over
    the month files, so a re-import of that movement could append a duplicate key
    without a refusal. Because a duplicate key is the project's primary correctness
    risk, this is the discriminating test for the index source: with the old
    ``transactions/*.beancount`` glob the first assertion below fires.
    """
    write_ledger(ledger)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "rootkey")])
    pipeline.approve(ledger, batch_id)

    # The ledger moves between staging and appending: the key appears in main itself.
    ledger.main().write_text(
        MAIN_TEMPLATE + txn_text(dt.date(2026, 1, 5), "rootkey"), encoding="utf-8"
    )
    assert "rootkey" in pipeline.build_key_index(ledger)

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, batch_id)
    assert excinfo.value.reason == pipeline.KEY_ALREADY_PRESENT


# --------------------------------------------------------------------------- identify


def test_identify_returns_the_single_matching_importer(statement: Path) -> None:
    importer = FakeImporter("one")
    result = pipeline.identify([statement], importers=[importer])
    assert len(result) == 1
    assert result[0].importer is importer


def test_identify_refuses_a_file_claimed_by_no_importer(statement: Path) -> None:
    importer = FakeImporter("one", suffix=".pdf")
    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.identify([statement], importers=[importer])
    assert excinfo.value.reason == pipeline.NO_IMPORTER


def test_identify_refuses_a_file_claimed_by_two_importers(statement: Path) -> None:
    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.identify(
            [statement],
            importers=[FakeImporter("one"), FakeImporter("two")],
        )
    assert excinfo.value.reason == pipeline.AMBIGUOUS_IMPORTER
    assert "one" in str(excinfo.value)
    assert "two" in str(excinfo.value)


def test_extract_wraps_an_importer_failure_in_a_refusal(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    statement = ledger.root / "statement.csv"
    statement.write_text("x\n", encoding="utf-8")
    importer = FakeImporter("boom", error=ValueError("reconciliation failed"))
    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.extract(ledger, "Test", statement, importers=[importer])
    assert excinfo.value.reason == pipeline.IMPORTER_RAISED
    assert "ValueError" in str(excinfo.value)


def test_extract_refusal_redacts_the_statement_basename_with_a_content_hash(
    ledger: LedgerPaths,
) -> None:
    """T-01d: ``importer-raised`` names the statement by content hash, never by basename."""
    write_ledger(ledger)
    statement = ledger.root / "statement.csv"
    statement.write_text("x\n", encoding="utf-8")
    importer = FakeImporter("boom", error=ValueError("reconciliation failed"))

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.extract(ledger, "Test", statement, importers=[importer])

    message = str(excinfo.value)
    key = load_or_create_key(ledger.redaction_key())
    assert excinfo.value.reason == pipeline.IMPORTER_RAISED
    assert "statement.csv" not in message
    assert content_hash(statement.read_bytes(), key) in message


def test_extract_refusal_names_only_the_exception_class_never_its_message(
    ledger: LedgerPaths,
) -> None:
    """R1-002/R3-002: an importer's raw ``str()`` can carry the statement's path."""
    write_ledger(ledger)
    statement = ledger.root / "some" / "nested" / "holder-name.csv"
    statement.parent.mkdir(parents=True)
    statement.write_text("x\n", encoding="utf-8")
    importer = FakeImporter("boom", error=FileNotFoundError(f"cannot open {statement}"))

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.extract(ledger, "Test", statement, importers=[importer])

    message = str(excinfo.value)
    assert excinfo.value.reason == pipeline.IMPORTER_RAISED
    assert "FileNotFoundError" in message
    assert str(statement) not in message
    assert "holder-name.csv" not in message


@pytest.mark.parametrize(
    "error_cls",
    [
        MercadoPagoCounterpartyClassificationError,
        ProvinciaCounterpartyClassificationError,
        ProvinciaVisaCounterpartyClassificationError,
    ],
)
def test_extract_preserves_the_counterparty_classification_message(
    ledger: LedgerPaths, error_cls: type[Exception]
) -> None:
    """T-04c's 'never defaulted' refusal (docs/import-workflow.md) reaches the CLI verbatim."""
    write_ledger(ledger)
    statement = ledger.root / "statement.csv"
    statement.write_text("x\n", encoding="utf-8")
    message_text = (
        "1 counterparties are not classified.\n"
        '  2026-04-02  -25,000.00 ARS  "desc"  "Raw Name"\n'
        "    append to counterparties.tsv:\n"
        "      MercadoPago\tRaw Name\texpense:<category>"
    )
    importer = FakeImporter("boom", error=error_cls(message_text))

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.extract(ledger, "Test", statement, importers=[importer])

    assert excinfo.value.reason == pipeline.IMPORTER_RAISED
    assert message_text in str(excinfo.value)


def test_extract_refusal_survives_a_statement_deleted_during_extract(
    ledger: LedgerPaths,
) -> None:
    """R4-001/R3-001/R2-004: hashing must not turn a typed refusal into an unhandled OSError."""
    write_ledger(ledger)
    statement = ledger.root / "statement.csv"
    statement.write_text("x\n", encoding="utf-8")
    importer = _DeletingImporter("boom")

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.extract(ledger, "Test", statement, importers=[importer])

    assert excinfo.value.reason == pipeline.IMPORTER_RAISED
    assert "unreadable" in str(excinfo.value)
    assert "ValueError" in str(excinfo.value)


# --------------------------------------------------------------------------- approve


def test_approve_refuses_a_missing_batch(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.approve(ledger, "Test-20260101T000000Z-deadbeef")
    assert excinfo.value.reason == pipeline.BATCH_NOT_FOUND


def test_approve_writes_digest_and_timestamp(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    pipeline.approve(ledger, batch_id)

    approval = json.loads(
        (ledger.staging_batch(batch_id) / "approval.json").read_text(encoding="utf-8")
    )
    proposed = (ledger.staging_batch(batch_id) / "proposed.beancount").read_bytes()
    assert approval["sha256"] == hashlib.sha256(proposed).hexdigest()
    assert approval["approved_at"].endswith("+00:00")


# ------------------------------------------------------------------- append refusals


def test_append_refuses_a_missing_batch(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, "Test-20260101T000000Z-deadbeef")
    assert excinfo.value.reason == pipeline.BATCH_NOT_FOUND


def test_append_refuses_without_approval(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, batch_id)
    assert excinfo.value.reason == pipeline.APPROVAL_MISSING


def test_append_refuses_when_the_digest_does_not_match(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    pipeline.approve(ledger, batch_id)
    proposed = ledger.staging_batch(batch_id) / "proposed.beancount"
    proposed.write_bytes(proposed.read_bytes().replace(b"100.00", b"999.00"))

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, batch_id)
    assert excinfo.value.reason == pipeline.DIGEST_MISMATCH


def test_append_refuses_when_main_is_missing(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    pipeline.approve(ledger, batch_id)
    ledger.main().unlink()

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, batch_id)
    assert excinfo.value.reason == pipeline.MAIN_MISSING


def test_append_refuses_when_the_ledger_is_already_dirty(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    pipeline.approve(ledger, batch_id)
    # Introduce an undeclared account; the ledger is now dirty before the append.
    ledger.accounts().write_text(ACCOUNTS, encoding="utf-8")
    dirty = txn_text(dt.date(2026, 1, 1), "prior").replace("Expenses:Otros", "Expenses:Undec")
    (ledger.transactions_dir() / "2026-01.beancount").write_text(dirty, encoding="utf-8")
    assert pipeline.ledger_errors(ledger)

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, batch_id)
    assert excinfo.value.reason == pipeline.LEDGER_DIRTY


def test_append_refuses_when_a_key_is_already_present(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    pipeline.approve(ledger, batch_id)
    # The same key arrives through another statement before this batch is appended.
    (ledger.transactions_dir() / "2026-01.beancount").write_text(
        txn_text(dt.date(2026, 1, 5), "k1"), encoding="utf-8"
    )

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, batch_id)
    assert excinfo.value.reason == pipeline.KEY_ALREADY_PRESENT


def test_append_refuses_a_mis_split_staging_file(ledger: LedgerPaths) -> None:
    entries = [make_entry(dt.date(2026, 1, 5), "k1"), make_entry(dt.date(2026, 1, 6), "k2")]
    batch_id = staged(ledger, entries)
    proposed = ledger.staging_batch(batch_id) / "proposed.beancount"
    glued = txn_text(dt.date(2026, 1, 5), "k1") + txn_text(dt.date(2026, 1, 6), "k2")
    proposed.write_text(glued + "\n; trailing\n", encoding="utf-8")
    pipeline.approve(ledger, batch_id)

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, batch_id)
    assert excinfo.value.reason == pipeline.STAGING_CORRUPT


def test_append_refuses_a_second_append(ledger: LedgerPaths) -> None:
    batch_id = staged(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    pipeline.approve(ledger, batch_id)
    pipeline.append(ledger, batch_id)

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, batch_id)
    assert excinfo.value.reason == pipeline.BATCH_ALREADY_APPENDED


def test_append_refuses_an_empty_batch(ledger: LedgerPaths) -> None:
    """A batch whose entries were all deduped must not write an append record.

    An empty proposed file and ``files: []`` in ``append.json`` would be an audit
    record claiming an append that never happened.
    """
    write_ledger(ledger, months={"2026-01": txn_text(dt.date(2026, 1, 1), "already")})
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 1), "already")])
    batch = ledger.staging_batch(batch_id)
    assert (batch / "proposed.beancount").read_bytes() == b""
    pipeline.approve(ledger, batch_id)

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, batch_id)
    assert excinfo.value.reason == pipeline.BATCH_EMPTY
    assert not (batch / "append.json").exists()


# ------------------------------------------------------------------- append success


def test_append_writes_the_staged_bytes_into_a_month_file(ledger: LedgerPaths) -> None:
    entry = make_entry(dt.date(2026, 1, 5), "k1")
    batch_id = staged(ledger, [entry])
    pipeline.approve(ledger, batch_id)
    pipeline.append(ledger, batch_id)

    month = ledger.transactions_dir() / "2026-01.beancount"
    assert month.read_bytes() == printer.format_entry(entry).encode("utf-8")


def test_append_creates_a_month_file_that_did_not_exist(ledger: LedgerPaths) -> None:
    batch_id = staged(ledger, [make_entry(dt.date(2026, 3, 7), "k1")])
    pipeline.approve(ledger, batch_id)
    pipeline.append(ledger, batch_id)

    month = ledger.transactions_dir() / "2026-03.beancount"
    assert month.is_file()
    assert pipeline.ledger_errors(ledger) == []


def test_append_preserves_previous_month_file_bytes_as_a_prefix(ledger: LedgerPaths) -> None:
    write_ledger(ledger, months={"2026-01": txn_text(dt.date(2026, 1, 1), "prior")})
    previous = (ledger.transactions_dir() / "2026-01.beancount").read_bytes()
    batch_id = stage(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    pipeline.approve(ledger, batch_id)
    pipeline.append(ledger, batch_id)

    month = (ledger.transactions_dir() / "2026-01.beancount").read_bytes()
    assert month.startswith(previous)
    assert month.endswith(b"\n")
    assert b"k1" in month


def test_append_orders_entries_by_date_across_months(ledger: LedgerPaths) -> None:
    entries = [
        make_entry(dt.date(2026, 2, 3), "feb"),
        make_entry(dt.date(2026, 1, 5), "jan"),
    ]
    batch_id = staged(ledger, entries)
    pipeline.approve(ledger, batch_id)
    pipeline.append(ledger, batch_id)

    assert "jan" in (ledger.transactions_dir() / "2026-01.beancount").read_text(encoding="utf-8")
    assert "feb" in (ledger.transactions_dir() / "2026-02.beancount").read_text(encoding="utf-8")


def test_append_writes_append_json(ledger: LedgerPaths) -> None:
    batch_id = staged(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    pipeline.approve(ledger, batch_id)
    pipeline.append(ledger, batch_id)

    record = json.loads(
        (ledger.staging_batch(batch_id) / "append.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "appended"
    assert record["batch_id"] == batch_id
    assert record["files"] == ["transactions/2026-01.beancount"]
    assert record["appended_at"].endswith("+00:00")


def test_append_leaves_the_ledger_clean(ledger: LedgerPaths) -> None:
    batch_id = staged(ledger, [make_entry(dt.date(2026, 1, 5), "k1")])
    pipeline.approve(ledger, batch_id)
    pipeline.append(ledger, batch_id)

    assert pipeline.ledger_errors(ledger) == []


# --------------------------------------------------------------------------- rollback


def test_append_rolls_back_a_post_write_failure_byte_for_byte(ledger: LedgerPaths) -> None:
    """A staged entry with an undeclared account passes every pre-write check.

    It only fails the bean-check gate *after* it is written, which is exactly the
    failure the rollback exists for. One touched month file already existed; the
    other did not, so rollback must restore one and delete the other.
    """
    write_ledger(ledger, months={"2026-01": txn_text(dt.date(2026, 1, 1), "prior")})
    month_jan = ledger.transactions_dir() / "2026-01.beancount"
    month_feb = ledger.transactions_dir() / "2026-02.beancount"
    before = month_jan.read_bytes()
    assert not month_feb.exists()

    bad = [
        make_entry(dt.date(2026, 1, 9), "bad-jan", account="Expenses:Undec"),
        make_entry(dt.date(2026, 2, 9), "bad-feb", account="Expenses:Undec"),
    ]
    batch_id = stage(ledger, bad)
    pipeline.approve(ledger, batch_id)
    approval_before = (ledger.staging_batch(batch_id) / "approval.json").read_bytes()

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.append(ledger, batch_id)
    assert excinfo.value.reason == pipeline.POST_WRITE_DIRTY

    assert month_jan.read_bytes() == before
    assert not month_feb.exists()
    assert not (ledger.staging_batch(batch_id) / "append.json").exists()
    assert (ledger.staging_batch(batch_id) / "approval.json").read_bytes() == approval_before


# ------------------------------------------------------------------------------ report


def test_report_lists_every_metadata_field_except_filename_and_lineno(
    ledger: LedgerPaths,
) -> None:
    write_ledger(ledger)
    entry = make_entry(dt.date(2026, 1, 5), "k1", extra_meta={"note": "reviewed by hand"})
    batch_id = stage(ledger, [entry])

    text = pipeline.report(ledger, batch_id)
    assert "2026-01-05" in text
    assert "Payee" in text and "Narration" in text
    assert "Expenses:Otros" in text and "Assets:Test:Caja" in text
    assert "note" in text and "reviewed by hand" in text
    assert "filename" not in text
    assert "lineno" not in text


def test_report_shows_totals_per_currency(ledger: LedgerPaths) -> None:
    write_ledger(ledger)
    entries = [
        make_entry(dt.date(2026, 1, 5), "k1", value="100.00", currency="ARS"),
        make_entry(dt.date(2026, 1, 6), "k2", value="15.00", currency="USD"),
    ]
    batch_id = stage(ledger, entries)
    text = pipeline.report(ledger, batch_id)
    assert "Debit-side totals per currency" in text
    assert "nets to zero" in text
    assert "ARS: 100.00" in text
    assert "USD: 15.00" in text


def test_report_lists_skipped_keys(ledger: LedgerPaths) -> None:
    write_ledger(ledger, months={"2026-01": txn_text(dt.date(2026, 1, 1), "already")})
    batch_id = stage(
        ledger,
        [
            make_entry(dt.date(2026, 1, 1), "already"),
            make_entry(dt.date(2026, 1, 5), "fresh"),
        ],
    )
    text = pipeline.report(ledger, batch_id)
    assert "already" in text
    assert "fresh" in text


# ------------------------------------------------------------------------------ gate


def test_in_process_gate_reports_the_failure_bean_check_reports(
    ledger: LedgerPaths,
) -> None:
    write_ledger(
        ledger,
        months={
            "2026-01": (
                '2026-01-05 * "Payee" "Narration"\n'
                '  key: "k1"\n'
                "  Expenses:Undec  100.00 ARS\n"
                "  Assets:Test:Caja  -100.00 ARS\n"
            )
        },
    )
    errors = pipeline.ledger_errors(ledger)
    assert errors  # the in-process gate sees the undeclared account

    result = subprocess.run(
        ["uv", "run", "--no-sync", "bean-check", str(ledger.main())],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode != 0
    assert "Undec" in result.stderr


def test_in_process_gate_reports_clean_when_bean_check_reports_clean(
    ledger: LedgerPaths,
) -> None:
    write_ledger(ledger)
    assert pipeline.ledger_errors(ledger) == []

    result = subprocess.run(
        ["uv", "run", "--no-sync", "bean-check", str(ledger.main())],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_hardcore_validations_list_is_pinned() -> None:
    """Tripwire for the ``ledger_errors`` parity claim.

    The parity claim rests on the verified fact that ``HARDCORE_VALIDATIONS`` is
    exactly ``[validate_data_types]`` and that validator inspects the types of
    in-memory entry objects, so it cannot fire for a ledger parsed from text. If
    beancount ever changes this list, the parity claim must be **re-derived**, not
    assumed: this test is the notification, not a permanent truth.
    """
    assert validation.HARDCORE_VALIDATIONS == [validation.validate_data_types]


def test_ledger_errors_forwards_hardcore_validations(
    ledger: LedgerPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the option itself, not just beancount's constant.

    Deleting ``extra_validations=...`` from ``ledger_errors`` is invisible to any
    text-ledger assertion (no hardcore validation can fire on parsed text), so this
    spy is what actually kills that mutation. The option is kept for CLI parity and
    to stay equal if a future hardcore validation can fire on text.
    """
    write_ledger(ledger)
    captured: dict[str, object] = {}
    real_load_file = loader.load_file

    def spy(filename, *args, **kwargs):
        captured["extra_validations"] = kwargs.get("extra_validations")
        return real_load_file(filename, *args, **kwargs)

    monkeypatch.setattr(loader, "load_file", spy)
    pipeline.ledger_errors(ledger)
    assert captured["extra_validations"] == validation.HARDCORE_VALIDATIONS


def test_pipeline_module_has_no_console_dependency_on_colour() -> None:
    source = (REPO_ROOT / "src" / "expensuchis" / "cli.py").read_text(encoding="utf-8")
    assert "\x1b[" not in source
    assert "\\033[" not in source


# ---------------------------------------------------------------------------- diagnose


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_diagnose_reports_posted_accounts_when_all_are_open(
    ledger: LedgerPaths, statement: Path
) -> None:
    write_ledger(ledger)
    importer = FakeImporter("test.importer.Fake", entries=[make_entry(dt.date(2026, 1, 5), "k1")])
    result = pipeline.diagnose(ledger, statement, importers=[importer])
    assert result.importer == "test.importer.Fake"
    assert result.accounts == ("Assets:Test:Caja", "Expenses:Otros")
    assert result.missing == ()


def test_diagnose_lists_unopened_accounts_sorted_with_their_first_date(
    ledger: LedgerPaths, statement: Path
) -> None:
    write_ledger(ledger)
    entries = [
        make_entry(dt.date(2026, 2, 9), "k2", account="Expenses:Zeta"),
        make_entry(dt.date(2026, 1, 5), "k1", account="Expenses:Alfa"),
        make_entry(dt.date(2026, 3, 1), "k3", account="Expenses:Alfa"),
    ]
    importer = FakeImporter("test.importer.Fake", entries=entries)
    result = pipeline.diagnose(ledger, statement, importers=[importer])
    assert result.missing == ("Expenses:Alfa", "Expenses:Zeta")
    assert result.first_dates == {
        "Expenses:Alfa": dt.date(2026, 1, 5),
        "Expenses:Zeta": dt.date(2026, 2, 9),
    }


def test_diagnose_writes_nothing_under_the_ledger(ledger: LedgerPaths, statement: Path) -> None:
    write_ledger(ledger)
    before = _snapshot(ledger.root)
    importer = FakeImporter(
        "test.importer.Fake",
        entries=[make_entry(dt.date(2026, 1, 5), "k1", account="Expenses:New")],
    )
    pipeline.diagnose(ledger, statement, importers=[importer])
    assert _snapshot(ledger.root) == before


def test_diagnose_refuses_a_missing_statement(ledger: LedgerPaths, tmp_path: Path) -> None:
    write_ledger(ledger)
    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.diagnose(ledger, tmp_path / "nope.csv", importers=[FakeImporter("x")])
    assert excinfo.value.reason == pipeline.STATEMENT_MISSING


def test_diagnose_refuses_when_no_importer_claims_the_statement(
    ledger: LedgerPaths, statement: Path
) -> None:
    write_ledger(ledger)
    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.diagnose(ledger, statement, importers=[FakeImporter("x", match=False)])
    assert excinfo.value.reason == pipeline.NO_IMPORTER


def test_diagnose_refuses_when_main_is_missing(ledger: LedgerPaths, statement: Path) -> None:
    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.diagnose(ledger, statement, importers=[FakeImporter("x")])
    assert excinfo.value.reason == pipeline.MAIN_MISSING


def test_diagnose_wraps_an_importer_failure_like_extract(
    ledger: LedgerPaths, statement: Path
) -> None:
    write_ledger(ledger)
    importer = FakeImporter("test.importer.Fake", error=ValueError("secret Juan Perez"))
    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.diagnose(ledger, statement, importers=[importer])
    assert excinfo.value.reason == pipeline.IMPORTER_RAISED
    assert "ValueError" in excinfo.value.message
    assert "Juan Perez" not in excinfo.value.message
    assert statement.name not in excinfo.value.message


def _open_dates(ledger: LedgerPaths, lines: str) -> None:
    ledger.accounts().write_text(lines, encoding="utf-8")


def test_diagnose_flags_an_account_opened_after_its_first_entry(
    ledger: LedgerPaths, statement: Path
) -> None:
    write_ledger(ledger)
    _open_dates(
        ledger,
        "2020-01-01 open Assets:Test:Caja\n2026-03-01 open Expenses:Otros\n"
        "2026-02-01 open Expenses:Otros\n",
    )
    importer = FakeImporter("test.importer.Fake", entries=[make_entry(dt.date(2026, 1, 5), "k1")])
    result = pipeline.diagnose(ledger, statement, importers=[importer])
    assert result.missing == ()
    assert result.opened_late == {"Expenses:Otros": dt.date(2026, 2, 1)}
    assert result.late_first_dates == {"Expenses:Otros": dt.date(2026, 1, 5)}
    assert result.accounts == ("Assets:Test:Caja", "Expenses:Otros")


def test_diagnose_does_not_flag_an_account_opened_on_or_before_its_first_entry(
    ledger: LedgerPaths, statement: Path
) -> None:
    write_ledger(ledger)
    _open_dates(ledger, "2020-01-01 open Assets:Test:Caja\n2026-01-05 open Expenses:Otros\n")
    importer = FakeImporter("test.importer.Fake", entries=[make_entry(dt.date(2026, 1, 5), "k1")])
    result = pipeline.diagnose(ledger, statement, importers=[importer])
    assert result.missing == ()
    assert result.opened_late == {}
    assert result.late_first_dates == {}


def test_diagnose_separates_late_accounts_from_truly_missing_ones(
    ledger: LedgerPaths, statement: Path
) -> None:
    write_ledger(ledger)
    _open_dates(ledger, "2020-01-01 open Assets:Test:Caja\n2026-03-01 open Expenses:Late\n")
    entries = [
        make_entry(dt.date(2026, 1, 5), "k1", account="Expenses:Late"),
        make_entry(dt.date(2026, 1, 6), "k2", account="Expenses:Gone"),
    ]
    importer = FakeImporter("test.importer.Fake", entries=entries)
    result = pipeline.diagnose(ledger, statement, importers=[importer])
    assert result.missing == ("Expenses:Gone",)
    assert result.first_dates == {"Expenses:Gone": dt.date(2026, 1, 6)}
    assert result.opened_late == {"Expenses:Late": dt.date(2026, 3, 1)}
    assert result.late_first_dates == {"Expenses:Late": dt.date(2026, 1, 5)}


def test_diagnose_reports_zero_ledger_errors_for_a_clean_ledger(
    ledger: LedgerPaths, statement: Path
) -> None:
    write_ledger(ledger)
    importer = FakeImporter("test.importer.Fake", entries=[make_entry(dt.date(2026, 1, 5), "k1")])
    assert pipeline.diagnose(ledger, statement, importers=[importer]).ledger_errors == 0


def test_diagnose_counts_loader_errors_and_still_returns(
    ledger: LedgerPaths, statement: Path
) -> None:
    write_ledger(ledger)
    with ledger.main().open("a", encoding="utf-8") as handle:
        handle.write('include "nope.beancount"\n')
    importer = FakeImporter("test.importer.Fake", entries=[make_entry(dt.date(2026, 1, 5), "k1")])
    result = pipeline.diagnose(ledger, statement, importers=[importer])
    assert result.ledger_errors >= 1
    assert result.accounts == ("Assets:Test:Caja", "Expenses:Otros")
