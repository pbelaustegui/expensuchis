"""Tests for the local acceptance probe (``tools/probe_bbva_cards.py``).

The probe is the only component that reads a real BBVA Visa/Mastercard card
statement, so its output surface is a privacy control: aggregates only, never
a date, description, merchant name, comprobante, amount, balance or filename.
These tests exercise that surface with the synthetic positioned-row fixtures
under ``tests/fixtures/bbva_card/`` (reused from the parser/importer suites)
through the probe's ``--rows`` test seam, asserting on stdout and stderr
together as ``capsys`` captures them.

The statement folder also holds account-side ``Extracto consolidado`` PDFs the
probe must not choke on: the parser's own marker predicate
(:func:`~expensuchis.importers.bbva_card.is_bbva_card_liquidacion`) gates each
file, and a non-card file is reported and skipped rather than crashing the
run, the mirror image of ``probe_bbva.py``'s treatment of card PDFs.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data

from expensuchis.importers.bbva_card import CHECK_NAMES

PROBE_PATH = Path(__file__).resolve().parents[1] / "tools" / "probe_bbva_cards.py"
CARD_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bbva_card"

VISA_ROWS_TEXT = (CARD_FIXTURES / "visa_full.txt").read_text(encoding="utf-8")
MASTERCARD_ROWS_TEXT = (CARD_FIXTURES / "mastercard_full.txt").read_text(encoding="utf-8")

#: Words, comprobantes, amounts and dates the fixtures invent; none of them is
#: a document-template word the probe legitimately prints, so none may ever
#: appear in its output.
INVENTED_TOKENS = (
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
    "03-Mar-26",
    "22-Mar-26",
    "28-Mar-26",
    "27-Feb-26",
    "10-Abr-26",
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
    "visa_full.txt",
    "mastercard_full.txt",
)

#: The shortest run of fixture text that must never reach the probe's output.
_MIN_ROWS_RUN = 8

#: Phrases the probe legitimately prints that overlap the fixtures' own
#: vocabulary (the frozen movement-kind and charge-class labels).
_ALLOWED_PRINTED_PHRASES = (
    "purchases:",
    "payments:",
    "charges:",
    "iva:",
    "percepciones:",
)


def _forbidden_text_runs(text: str) -> set[str]:
    windows = {
        text[index : index + _MIN_ROWS_RUN] for index in range(len(text) - _MIN_ROWS_RUN + 1)
    }
    return {
        window
        for window in windows
        if not any(window in phrase for phrase in _ALLOWED_PRINTED_PHRASES)
    }


def _assert_no_fixture_run(text: str, combined: str) -> None:
    leaked = sorted(run for run in _forbidden_text_runs(text) if run in combined)
    assert leaked == [], leaked


def _load_probe():
    # The probe module is loaded the same way test_probe_bbva.py loads its own:
    # by file location, registered in sys.modules before execution.
    spec = importlib.util.spec_from_file_location("probe_bbva_cards", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


# --------------------------------------------------------------------------- success


def test_the_full_visa_fixture_prints_aggregates_and_no_statement_content(capsys) -> None:
    path = CARD_FIXTURES / "visa_full.txt"

    assert probe.main(["--rows", str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "pages=1" in combined
    assert "brand=visa" in combined
    # Movement shape: 6 purchases, 2 payments (which emit nothing), 3 charges.
    assert "movements purchases: 6" in combined
    assert "movements payments: 2" in combined
    assert "movements charges: 3" in combined
    # Entries: 6 purchases + 3 charges; the installment row collapses into a
    # single plan entry (counted under plans below).
    assert "entries built: 9" in combined
    assert "installment plans: 1" in combined
    assert "charges iva: 1" in combined
    assert "charges percepciones: 2" in combined
    # Holder metadata: the additional cardholder's two purchases carry it.
    assert "entries with holder meta: 2" in combined
    assert "entries without holder meta: 7" in combined
    assert "distinct merchant identities resolved: 6" in combined
    assert "natural-keys: ok" in combined
    for name in CHECK_NAMES:
        assert f"check {name}: ok" in combined
    for token in (*INVENTED_TOKENS, str(path)):
        assert token not in combined, (token, combined)
    _assert_no_fixture_run(VISA_ROWS_TEXT, combined)


def test_the_mastercard_fixture_runs_and_reports_its_brand(capsys) -> None:
    path = CARD_FIXTURES / "mastercard_full.txt"

    assert probe.main(["--rows", str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "brand=mastercard" in combined
    assert "natural-keys: ok" in combined
    for name in CHECK_NAMES:
        assert f"check {name}: ok" in combined
    for token in (*INVENTED_TOKENS, str(path)):
        assert token not in combined, (token, combined)
    _assert_no_fixture_run(MASTERCARD_ROWS_TEXT, combined)


def test_entries_are_reported_per_card_account(capsys) -> None:
    path = CARD_FIXTURES / "visa_full.txt"

    assert probe.main(["--rows", str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "account Liabilities:BBVA:PERSONA:Visa: 8" in combined
    assert "account Liabilities:BBVA:PERSONA:VisaUSD: 1" in combined


# --------------------------------------------------------------------------- non-card files


#: A --rows fixture, in the same PAGE/ROW/token format, that carries none of
#: the card marker's required lines (no CIERRE ACTUAL, no SALDO ANTERIOR, no
#: FECHA+PESOS header) -- structurally not a card liquidación.
_NOT_A_CARD_ROWS = "PAGE 1\nROW\n20.0 86.0 EXTRACTO\n94.0 130.0 CONSOLIDADO\nROW\n20.0 86.0 SALDO\n94.0 130.0 FINAL\n"


def test_a_non_card_file_is_reported_and_does_not_fail_the_run(capsys, tmp_path: Path) -> None:
    path = tmp_path / "not-a-card.txt"
    path.write_text(_NOT_A_CARD_ROWS, encoding="utf-8")

    assert probe.main(["--rows", str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "not a card liquidación" in combined


def test_a_run_mixing_a_card_and_a_non_card_file_processes_both(capsys, tmp_path: Path) -> None:
    card_path = CARD_FIXTURES / "visa_full.txt"
    other_path = tmp_path / "not-a-card.txt"
    other_path.write_text(_NOT_A_CARD_ROWS, encoding="utf-8")

    exit_code = probe.main(["--rows", str(card_path), str(other_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "not a card liquidación" in combined
    assert "brand=visa" in combined


# --------------------------------------------------------------------------- failures


def test_a_reconciliation_failure_exits_one_with_masked_details(capsys) -> None:
    path = CARD_FIXTURES / "fail_balance_ars.txt"

    assert probe.main(["--rows", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "class: ReconciliationError" in combined
    assert "check balance-ars: FAILED" in combined
    assert "refused" in combined
    for token in (*INVENTED_TOKENS, str(path), "fail_balance_ars.txt"):
        assert token not in combined, (token, combined)


def test_a_structural_failure_exits_one_with_a_masked_parse_error(capsys) -> None:
    path = CARD_FIXTURES / "fail_no_brand.txt"

    assert probe.main(["--rows", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "class: CardLiquidacionParseError" in combined
    assert "parse: FAILED" in combined
    assert str(path) not in combined
    assert path.name not in combined


def test_a_key_uniqueness_failure_is_reported_not_a_crash(capsys, monkeypatch) -> None:
    path = CARD_FIXTURES / "visa_full.txt"

    def _duplicate_keys(liquidacion, person, counterparty_map):
        meta = {"key": "bbva-card:PERSONA:visa:duplicate"}
        posting = data.Posting(
            "Expenses:Probe", data.Amount(Decimal(1), "ARS"), None, None, None, None
        )
        return [
            data.Transaction(
                meta, liquidacion.close_date, "*", "", "", frozenset(), frozenset(), [posting]
            ),
            data.Transaction(
                dict(meta), liquidacion.close_date, "*", "", "", frozenset(), frozenset(), [posting]
            ),
        ]

    monkeypatch.setattr(probe, "build_entries", _duplicate_keys)

    assert probe.main(["--rows", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "natural-keys: FAILED" in combined
    assert "duplicates: 1" in combined
    assert str(path) not in combined


# --------------------------------------------------------------------------- usage


def test_no_paths_is_a_usage_error(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe.ENV_VAR, raising=False)

    assert probe.main([]) == 2

    captured = capsys.readouterr()
    assert "no statement paths given" in captured.out + captured.err


def test_paths_come_from_the_environment_when_argv_is_empty(
    capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = CARD_FIXTURES / "visa_full.txt"
    monkeypatch.setenv(probe.ENV_VAR, str(path))

    assert probe.main(["--rows"]) == 0

    captured = capsys.readouterr()
    assert "brand=visa" in captured.out


def test_a_missing_file_exits_one_without_naming_it(capsys, tmp_path: Path) -> None:
    path = tmp_path / "missing-statement.pdf"

    assert probe.main([str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "unreadable" in combined
    assert "FileNotFoundError" in combined
    assert str(path) not in combined
    assert path.name not in combined
