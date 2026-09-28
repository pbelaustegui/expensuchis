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

from expensuchis import cli, pipeline, summary
from expensuchis.fx import FxCache, Quote, Series
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
    assert commands == {
        "bootstrap",
        "identify",
        "extract",
        "report",
        "approve",
        "append",
        "summary",
        "installments",
    }


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


class _FakeFxSource:
    source_id = "fake"

    def __init__(self, quotes):
        self._quotes = quotes

    def url_for(self, series: Series) -> str:
        return f"fake://{series.value}"

    def fetch(self, series: Series):
        return self._quotes[series]


def test_summary_command_prints_totals(ledger: LedgerPaths, capsys) -> None:
    cli.main(["bootstrap"])
    ledger.accounts().write_text(
        "2020-01-01 open Assets:Test:Caja\n2020-01-01 open Expenses:Otros\n",
        encoding="utf-8",
    )
    FxCache(
        ledger,
        source=_FakeFxSource(
            {Series.MEP: [Quote(dt.date(2026, 1, 1), Decimal(1000), Decimal(1000))]}
        ),
    ).refresh(Series.MEP)
    month_file = ledger.transaction_file(dt.date(2026, 1, 5))
    month_file.write_text(
        '2026-01-05 * "Store" "Stuff"\n'
        '  key: "k1"\n'
        "  Expenses:Otros       1000.00 ARS\n"
        "  Assets:Test:Caja    -1000.00 ARS\n",
        encoding="utf-8",
    )

    assert cli.main(["summary", "--month", "2026-01", "--series", "mep"]) == 0
    out = capsys.readouterr().out
    assert "Expenses:Otros\t1.00 USD" in out
    assert "total\t1.00 USD" in out


def test_summary_refuses_when_the_fx_cache_is_missing(ledger: LedgerPaths, capsys) -> None:
    cli.main(["bootstrap"])
    ledger.accounts().write_text(
        "2020-01-01 open Assets:Test:Caja\n2020-01-01 open Expenses:Otros\n",
        encoding="utf-8",
    )
    month_file = ledger.transaction_file(dt.date(2026, 1, 5))
    month_file.write_text(
        '2026-01-05 * "Store" "Stuff"\n'
        '  key: "k1"\n'
        "  Expenses:Otros       1000.00 ARS\n"
        "  Assets:Test:Caja    -1000.00 ARS\n",
        encoding="utf-8",
    )

    assert cli.main(["summary", "--month", "2026-01", "--series", "mep"]) == 1
    assert summary.FX_CACHE_MISSING in capsys.readouterr().err


def test_summary_range_prints_one_total_line_per_month(ledger: LedgerPaths, capsys) -> None:
    cli.main(["bootstrap"])
    ledger.accounts().write_text(
        "2020-01-01 open Assets:Test:Caja\n2020-01-01 open Expenses:Otros\n",
        encoding="utf-8",
    )
    FxCache(
        ledger,
        source=_FakeFxSource(
            {
                Series.MEP: [
                    Quote(dt.date(2026, 1, 1), Decimal(1000), Decimal(1000)),
                    Quote(dt.date(2026, 2, 1), Decimal(1000), Decimal(1000)),
                ]
            }
        ),
    ).refresh(Series.MEP)
    for day, month in ((5, 1), (5, 2)):
        month_file = ledger.transaction_file(dt.date(2026, month, day))
        month_file.write_text(
            f'2026-{month:02d}-{day:02d} * "Store" "Stuff"\n'
            '  key: "k1"\n'
            "  Expenses:Otros       1000.00 ARS\n"
            "  Assets:Test:Caja    -1000.00 ARS\n",
            encoding="utf-8",
        )

    exit_code = cli.main(["summary", "--from", "2026-01", "--to", "2026-02", "--series", "mep"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "2026-01\t1.00 USD" in out
    assert "2026-02\t1.00 USD" in out
    assert "Expenses:Otros" not in out


def test_summary_rejects_month_combined_with_range() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "summary",
                "--month",
                "2026-01",
                "--from",
                "2026-01",
                "--to",
                "2026-02",
                "--series",
                "mep",
            ]
        )
    assert excinfo.value.code == 2


def test_summary_rejects_from_without_to() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["summary", "--from", "2026-01", "--series", "mep"])
    assert excinfo.value.code == 2


def test_summary_rejects_no_month_and_no_range() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["summary", "--series", "mep"])
    assert excinfo.value.code == 2


def test_installments_command_prints_rows_and_total(ledger: LedgerPaths, capsys) -> None:
    cli.main(["bootstrap"])
    ledger.accounts().write_text(
        "2020-01-01 open Assets:Test:Caja\n"
        "2020-01-01 open Expenses:Otros\n"
        "2020-01-01 open Liabilities:Test:Visa\n",
        encoding="utf-8",
    )
    FxCache(
        ledger,
        source=_FakeFxSource(
            {Series.MEP: [Quote(dt.date(2026, 2, 1), Decimal(1000), Decimal(1000))]}
        ),
    ).refresh(Series.MEP)
    month_file = ledger.transaction_file(dt.date(2026, 1, 5))
    month_file.write_text(
        '2026-01-05 * "Store" "Washing machine"\n'
        '  key: "k1"\n'
        "  installments: 6\n"
        '  first_due: "2026-02"\n'
        "  installment_amount: 500.00 ARS\n"
        "  Expenses:Otros         3000.00 ARS\n"
        "  Liabilities:Test:Visa -3000.00 ARS\n",
        encoding="utf-8",
    )

    assert cli.main(["installments", "--month", "2026-02", "--series", "mep"]) == 0
    out = capsys.readouterr().out
    assert "Store\t1/6\t0.50 USD" in out
    assert "total\t0.50 USD" in out


def test_installments_command_prints_only_total_when_no_plan_is_due(
    ledger: LedgerPaths, capsys
) -> None:
    cli.main(["bootstrap"])
    ledger.accounts().write_text(
        "2020-01-01 open Assets:Test:Caja\n2020-01-01 open Expenses:Otros\n",
        encoding="utf-8",
    )
    capsys.readouterr()

    assert cli.main(["installments", "--month", "2026-02", "--series", "mep"]) == 0
    out = capsys.readouterr().out
    assert out == "total\t0.00 USD\n"


def test_installments_refuses_when_the_fx_cache_is_missing(ledger: LedgerPaths, capsys) -> None:
    cli.main(["bootstrap"])
    ledger.accounts().write_text(
        "2020-01-01 open Assets:Test:Caja\n"
        "2020-01-01 open Expenses:Otros\n"
        "2020-01-01 open Liabilities:Test:Visa\n",
        encoding="utf-8",
    )
    month_file = ledger.transaction_file(dt.date(2026, 1, 5))
    month_file.write_text(
        '2026-01-05 * "Store" "Washing machine"\n'
        '  key: "k1"\n'
        "  installments: 6\n"
        '  first_due: "2026-01"\n'
        "  installment_amount: 500.00 ARS\n"
        "  Expenses:Otros         3000.00 ARS\n"
        "  Liabilities:Test:Visa -3000.00 ARS\n",
        encoding="utf-8",
    )

    assert cli.main(["installments", "--month", "2026-01", "--series", "mep"]) == 1
    assert summary.FX_CACHE_MISSING in capsys.readouterr().err
