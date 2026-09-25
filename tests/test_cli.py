"""Tests for the expensuchis command-line front end (exit codes, refusal codes, output)."""

from __future__ import annotations

import datetime as dt
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import amount, data
from beangulp import Importer

from expensuchis import cli, pipeline
from expensuchis.ledger import ENV_VAR
from expensuchis.paths import LedgerPaths

ACCOUNTS = (
    "2020-01-01 open Equity:Opening-Balances\n"
    "2020-01-01 open Assets:TransferenciaEnTransito\n"
    "2020-01-01 open Expenses:Otros\n"
    "2020-01-01 open Assets:Test:Caja\n"
)


class FakeImporter(Importer):
    def __init__(self, name="test.importer.Fake", entries=None):
        self._name = name
        self._entries = list(entries or [])

    @property
    def name(self):
        return self._name

    def identify(self, filepath):
        return str(filepath).endswith(".csv")

    def account(self, filepath):
        return "Assets:Test:Caja"

    def extract(self, filepath, existing):
        return list(self._entries)


def make_entry(date: dt.date, key: str) -> data.Transaction:
    postings = [
        data.Posting(
            "Expenses:Otros", amount.Amount(Decimal("100.00"), "ARS"), None, None, None, None
        ),
        data.Posting(
            "Assets:Test:Caja", amount.Amount(Decimal("-100.00"), "ARS"), None, None, None, None
        ),
    ]
    meta = {"filename": "<test>", "lineno": 1, "key": key}
    return data.Transaction(
        meta, date, "*", "Payee", "Narration", frozenset(), frozenset(), postings
    )


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LedgerPaths:
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))
    return LedgerPaths()


@pytest.fixture
def statement(tmp_path: Path) -> Path:
    path = tmp_path / "statement.csv"
    path.write_text("synthetic,content\n", encoding="utf-8")
    return path


class ClosedPipe:
    """A stdout whose buffered writes surface as BrokenPipeError only at flush."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        raise BrokenPipeError("consumer closed the pipe")

    def fileno(self) -> int:
        raise OSError("no file descriptor")


def test_main_returns_141_when_the_output_pipe_closes(
    ledger: LedgerPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    assert cli.main(["bootstrap"]) == 141


def test_parser_exposes_every_command() -> None:
    parser = cli.build_parser()
    actions = [a for a in parser._actions if isinstance(a, cli.argparse._SubParsersAction)]
    commands = set(actions[0].choices)
    assert commands == {"bootstrap", "identify", "extract", "report", "approve", "append"}


def test_unknown_command_is_a_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["definitely-not-a-command"])
    assert excinfo.value.code == 2


def test_missing_required_extract_argument_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["extract", "--source", "Test"])
    assert excinfo.value.code == 2


def test_bootstrap_command_reports_success(ledger: LedgerPaths, capsys) -> None:
    assert cli.main(["bootstrap"]) == 0
    assert ledger.main().is_file()
    assert "\x1b[" not in capsys.readouterr().out


def test_bootstrap_refuses_a_second_time(ledger: LedgerPaths, capsys) -> None:
    cli.main(["bootstrap"])
    assert cli.main(["bootstrap"]) == 1
    err = capsys.readouterr().err
    assert pipeline.BATCH_NOT_FOUND not in err
    assert "bootstrap-exists" in err


def test_identify_with_no_importer_refuses(ledger: LedgerPaths, capsys, statement: Path) -> None:
    cli.main(["bootstrap"])
    assert cli.main(["identify", str(statement)]) == 1
    assert pipeline.NO_IMPORTER in capsys.readouterr().err


def test_identify_lists_the_matching_importer(
    ledger: LedgerPaths, monkeypatch: pytest.MonkeyPatch, capsys, statement: Path
) -> None:
    monkeypatch.setattr(pipeline, "get_importers", lambda: [FakeImporter()])
    cli.main(["bootstrap"])
    assert cli.main(["identify", str(statement)]) == 0
    out = capsys.readouterr().out
    assert "test.importer.Fake" in out


def test_extract_without_an_importer_refuses(ledger: LedgerPaths, capsys, statement: Path) -> None:
    cli.main(["bootstrap"])
    assert cli.main(["extract", "--source", "Test", str(statement)]) == 1
    assert pipeline.NO_IMPORTER in capsys.readouterr().err


def test_full_flow_through_the_cli(
    ledger: LedgerPaths, monkeypatch: pytest.MonkeyPatch, capsys, statement: Path
) -> None:
    monkeypatch.setattr(
        pipeline,
        "get_importers",
        lambda: [FakeImporter(entries=[make_entry(dt.date(2026, 1, 5), "k1")])],
    )
    assert cli.main(["bootstrap"]) == 0
    ledger.accounts().write_text(ACCOUNTS, encoding="utf-8")

    assert cli.main(["extract", "--source", "Test", str(statement)]) == 0
    out = capsys.readouterr().out
    batch_id = re.search(r"batch_id:\s*(\S+)", out).group(1)
    report_path = re.search(r"report_path:\s*(\S+)", out).group(1)
    assert Path(report_path).is_file()

    assert cli.main(["report", batch_id]) == 0
    assert "Narration" in capsys.readouterr().out

    assert cli.main(["approve", batch_id]) == 0
    assert cli.main(["append", batch_id]) == 0

    assert pipeline.ledger_errors(ledger) == []
    assert "k1" in (ledger.transactions_dir() / "2026-01.beancount").read_text(encoding="utf-8")


def test_append_without_approval_refuses_with_a_reason(
    ledger: LedgerPaths, monkeypatch: pytest.MonkeyPatch, capsys, statement: Path
) -> None:
    monkeypatch.setattr(
        pipeline,
        "get_importers",
        lambda: [FakeImporter(entries=[make_entry(dt.date(2026, 1, 5), "k1")])],
    )
    cli.main(["bootstrap"])
    ledger.accounts().write_text(ACCOUNTS, encoding="utf-8")
    cli.main(["extract", "--source", "Test", str(statement)])
    batch_id = re.search(r"batch_id:\s*(\S+)", capsys.readouterr().out).group(1)

    assert cli.main(["append", batch_id]) == 1
    assert pipeline.APPROVAL_MISSING in capsys.readouterr().err


def test_output_contains_no_ansi_escapes(
    ledger: LedgerPaths, monkeypatch: pytest.MonkeyPatch, capsys, statement: Path
) -> None:
    monkeypatch.setattr(pipeline, "get_importers", lambda: [FakeImporter()])
    cli.main(["bootstrap"])
    cli.main(["extract", "--source", "Test", str(statement)])
    captured = capsys.readouterr()
    assert "\x1b[" not in captured.out + captured.err
