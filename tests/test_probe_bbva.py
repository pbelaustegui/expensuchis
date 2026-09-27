"""Tests for the local acceptance probe (``tools/probe_bbva.py``).

The probe is the only component that reads a real BBVA statement, so its output
surface is a privacy control: it must print aggregates only and never a date,
merchant, CUIT, account number, amount, balance or filename. These tests exercise
that surface with the synthetic fixtures under ``tests/fixtures/bbva/`` (reused
from the parser suite, per the T-08c/T-05a convention) and with mutated/inline
text, asserting on stdout and stderr together as ``capsys`` captures them.

The BBVA statement folder also holds card (Visa/Mastercard) statements the probe
must not choke on: :func:`~expensuchis.importers.bbva.is_bbva_extracto` gates
each file first, and a non-``Extracto consolidado`` file is reported and skipped
rather than crashing the run (T-07c brief). The statement never prints its own
year, so the probe needs an anchor date the way the importer does; ``--text``
(the test seam, since the synthetic fixtures are not real PDFs) always pairs
with ``--anchor``, since a text fixture carries no PDF ``CreationDate``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from expensuchis.importers.bbva import CHECK_NAMES, MovementKind

PROBE_PATH = Path(__file__).resolve().parents[1] / "tools" / "probe_bbva.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bbva"
BRUBANK_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "brubank"

FULL = (FIXTURES / "full_statement.txt").read_text(encoding="utf-8")
MULTI_BLOCK = (FIXTURES / "multi_block_quiescent.txt").read_text(encoding="utf-8")

ANCHOR = "2026-09-25"

# Statement words/values the fixtures invent; none of them is a document-template
# word, so none of them may ever appear in the probe's output.
INVENTED_TOKENS = (
    "FULANO",
    "EJEMPLO",
    "COMERCIO",
    "KIOSCO",
    "JUAN",
    "000-111111/1",
    "000-222222/2",
    "000-333333/3",
    "000-444444/4",
    "58273941605837",
    "70491638527049",
    "20000000001",
    "20000000002",
    "0001111111",
    "0002222222",
    "0003333333",
    "0004444444",
    "10.437,19",
    "9.954,02",
    "483,17",
    "237,94",
    "114,37",
    "1.042,63",
    "689,14",
    "583920",
    "847261",
    "519384",
    "full_statement.txt",
    "multi_block_quiescent.txt",
)

#: The shortest run of statement text that must never reach the probe's output.
_MIN_TEXT_RUN = 10

#: Phrases the probe legitimately prints that also happen to overlap, in some
#: form, the fixtures' template vocabulary (the frozen movement-kind labels).
_ALLOWED_PRINTED_PHRASES = tuple(f"movement {kind.value}:" for kind in MovementKind)


def _forbidden_text_runs(text: str) -> set[str]:
    windows = {
        text[index : index + _MIN_TEXT_RUN] for index in range(len(text) - _MIN_TEXT_RUN + 1)
    }
    return {
        window
        for window in windows
        if not any(window in phrase for phrase in _ALLOWED_PRINTED_PHRASES)
    }


def _assert_no_statement_run(text: str, combined: str) -> None:
    leaked = sorted(run for run in _forbidden_text_runs(text) if run in combined)
    assert leaked == [], leaked


def _load_probe():
    # The probe module defines its own frozen dataclasses, whose machinery
    # looks itself up in ``sys.modules`` by ``__module__`` name; register it
    # before executing, the same registration a normal ``import`` performs.
    spec = importlib.util.spec_from_file_location("probe_bbva", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


# --------------------------------------------------------------------------- success


def test_the_full_statement_prints_aggregates_and_no_statement_content(capsys) -> None:
    path = FIXTURES / "full_statement.txt"

    assert probe.main(["--text", "--anchor", ANCHOR, str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "pages=1" in combined
    assert "blocks=1" in combined
    assert "block 1: kind=cc currency=$ quiescent=False" in combined
    assert "routing: ok" in combined
    assert "movement debit_card_purchase: 2" in combined
    assert "movement visa_settlement: 1" in combined
    assert "movement mastercard_settlement: 1" in combined
    assert "movement cash_withdrawal: 1" in combined
    assert "movement salary: 1" in combined
    assert "movement interest: 1" in combined
    assert "movement transfer_out: 2" in combined
    assert "movement transfer_in: 1" in combined
    assert "debit-join matched: 2" in combined
    assert "debit-join zero-candidates: 0" in combined
    assert "debit-join multiple-candidates: 0" in combined
    assert "debit-join detail-rows-outside-window: 1" in combined
    assert "transfer-join matched: 2" in combined
    assert "transfer-join zero-candidates: 0" in combined
    assert "transfer-join multiple-candidates: 0" in combined
    assert "transfer-join unmatched-transfer-rows: 0" in combined
    assert "close-date month=9 day=20" in combined
    assert "anchor-gap-days: 5" in combined
    for name in CHECK_NAMES:
        assert f"check {name}: ok" in combined
    for token in (*INVENTED_TOKENS, str(path)):
        assert token not in combined, (token, combined)
    _assert_no_statement_run(FULL, combined)


def test_the_multi_block_quiescent_statement_reports_three_quiescent_blocks(capsys) -> None:
    path = FIXTURES / "multi_block_quiescent.txt"

    assert probe.main(["--text", "--anchor", ANCHOR, str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "pages=1" in combined
    assert "blocks=3" in combined
    assert "block 1: kind=cc currency=$ quiescent=True" in combined
    assert "block 2: kind=ca currency=eur quiescent=True" in combined
    assert "block 3: kind=ca currency=u$s quiescent=True" in combined
    assert "routing: ok" in combined
    for kind in MovementKind:
        assert f"movement {kind.value}: 0" in combined
    assert "debit-join matched: 0" in combined
    assert "debit-join zero-candidates: 0" in combined
    assert "debit-join multiple-candidates: 0" in combined
    assert "debit-join detail-rows-outside-window: 0" in combined
    assert "transfer-join matched: 0" in combined
    assert "transfer-join zero-candidates: 0" in combined
    assert "transfer-join multiple-candidates: 0" in combined
    assert "transfer-join unmatched-transfer-rows: 0" in combined
    assert "close-date month=9 day=20" in combined
    assert "anchor-gap-days: 5" in combined
    for name in CHECK_NAMES:
        assert f"check {name}: ok" in combined
    for token in (*INVENTED_TOKENS, str(path)):
        assert token not in combined, (token, combined)
    _assert_no_statement_run(MULTI_BLOCK, combined)


def test_a_non_extracto_statement_is_reported_and_does_not_crash_the_run(capsys) -> None:
    # The real folder also holds card statements. A Brubank fixture stands in
    # for "some other document" here: structurally not a BBVA extracto either
    # way, and it exercises the same gate.
    path = BRUBANK_FIXTURES / "minimal.txt"

    assert probe.main(["--text", "--anchor", ANCHOR, str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "not an extracto consolidado" in combined


def test_a_run_mixing_an_extracto_and_a_non_extracto_file_processes_both(capsys) -> None:
    extracto_path = FIXTURES / "full_statement.txt"
    other_path = BRUBANK_FIXTURES / "minimal.txt"

    exit_code = probe.main(["--text", "--anchor", ANCHOR, str(extracto_path), str(other_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "not an extracto consolidado" in combined
    assert "blocks=1" in combined


# --------------------------------------------------------------------------- failures


def test_a_reconciliation_failure_exits_one_with_masked_details(capsys, tmp_path: Path) -> None:
    # A single-digit change to the opening balance breaks the running-balance
    # chain and the closing-balance check while keeping every line's shape
    # (and therefore every structural check) valid.
    mutated = FULL.replace("SALDO ANTERIOR 10.437,19", "SALDO ANTERIOR 10.437,29")
    assert mutated != FULL
    path = tmp_path / "statement.txt"
    path.write_text(mutated, encoding="utf-8")

    assert probe.main(["--text", "--anchor", ANCHOR, str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "class: ReconciliationError" in combined
    assert "check running-balance-chain: FAILED" in combined
    assert "refused" in combined
    for token in (*INVENTED_TOKENS, "10.437,29", str(path), "statement.txt"):
        assert token not in combined, (token, combined)


def test_a_structural_failure_exits_one_with_a_masked_parse_error(capsys, tmp_path: Path) -> None:
    broken = "EXTRACTO CONSOLIDADO\nFECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO\n"
    path = tmp_path / "broken.txt"
    path.write_text(broken, encoding="utf-8")

    assert probe.main(["--text", "--anchor", ANCHOR, str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "class: ExtractoConsolidadoParseError" in combined
    assert "parse: FAILED" in combined
    assert "cause: no sub-account block found" in combined
    assert str(path) not in combined
    assert path.name not in combined


def test_anchor_is_required_together_with_text(capsys) -> None:
    path = FIXTURES / "full_statement.txt"

    assert probe.main(["--text", str(path)]) == 2

    captured = capsys.readouterr()
    assert "--anchor" in (captured.out + captured.err)
    assert str(path) not in (captured.out + captured.err)


def test_a_missing_file_exits_one_without_naming_it(capsys, tmp_path: Path) -> None:
    path = tmp_path / "missing-statement.txt"

    assert probe.main(["--text", "--anchor", ANCHOR, str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "unreadable" in combined
    assert "FileNotFoundError" in combined
    assert str(path) not in combined
    assert path.name not in combined


def test_non_utf8_text_names_only_the_exception_class(capsys, tmp_path: Path) -> None:
    path = tmp_path / "not-utf8.txt"
    path.write_bytes(b"\xff\xfe\x00\x01synthetic")

    assert probe.main(["--text", "--anchor", ANCHOR, str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "text extraction failed: UnicodeDecodeError" in combined
    assert str(path) not in combined
    assert path.name not in combined


def test_paths_come_from_the_environment_when_argv_is_empty(
    capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = FIXTURES / "full_statement.txt"
    monkeypatch.setenv(probe.ENV_VAR, str(path))

    assert probe.main(["--text", "--anchor", ANCHOR]) == 0

    captured = capsys.readouterr()
    assert "blocks=1" in captured.out


def test_no_paths_is_a_usage_error(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe.ENV_VAR, raising=False)

    assert probe.main([]) == 2

    captured = capsys.readouterr()
    assert "no statement paths given" in captured.out + captured.err
