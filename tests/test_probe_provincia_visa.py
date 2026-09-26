"""Tests for the local acceptance probe (``tools/probe_provincia_visa.py``).

The probe is the only component that reads a real statement, so its output surface
is a privacy control: it must print aggregates only and never a date, description,
comprobante, rate, amount, balance value or filename — the payment block's balance
row is reported only as present or absent. These tests exercise that surface with
the synthetic fixtures under ``tests/fixtures/provincia_visa/`` and with broken
input, asserting on stdout and stderr together as ``capsys`` captures them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from expensuchis.importers.provincia_visa import CHECK_NAMES, SurchargeKind

PROBE_PATH = Path(__file__).resolve().parents[1] / "tools" / "probe_provincia_visa.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "provincia_visa"

FULL = (FIXTURES / "full_statement.txt").read_text(encoding="utf-8")
MINIMAL = (FIXTURES / "minimal_statement.txt").read_text(encoding="utf-8")

# Statement words the fixtures invent; none of them is a document-template word, so
# none of them may ever appear in the probe's output.
INVENTED_WORDS = ("Zxqv", "Qwerty", "Plugh")

#: The shortest run of statement text that must never reach the probe's output. Any
#: longer run contains a run of this length, so checking every fixed-length window
#: catches a leaked excerpt whether it is printed whole or truncated by a mutant.
_MIN_TEXT_RUN = 12


def _forbidden_text_runs(text: str) -> set[str]:
    """Every ``_MIN_TEXT_RUN``-character window of ``text`` that is not template.

    The probe legitimately prints the frozen surcharge-kind names (its ``surcharge``
    lines), which also appear in the statement's post-total rows, so windows inside
    those printed phrases are allowed; every other window is statement content and
    must never be printed.
    """
    windows = {
        text[index : index + _MIN_TEXT_RUN] for index in range(len(text) - _MIN_TEXT_RUN + 1)
    }
    allowed = tuple(f"  surcharge {kind.value}" for kind in SurchargeKind)
    return {window for window in windows if not any(window in phrase for phrase in allowed)}


def _assert_no_statement_run(text: str, combined: str) -> None:
    leaked = sorted(run for run in _forbidden_text_runs(text) if run in combined)
    assert leaked == [], leaked


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_provincia_visa", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def test_the_full_fixture_prints_aggregates_and_no_statement_content(capsys) -> None:
    path = FIXTURES / "full_statement.txt"

    assert probe.main(["--text", str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "pages=1" in combined
    assert "charges=6" in combined
    # One payment row (the credit); the block's second row is the balance row, so
    # the indicator says present without ever naming a value.
    assert "payments=1" in combined
    assert "balance=present" in combined
    assert "surcharge sellos: 2" in combined
    assert "surcharge percepcion: 1" in combined
    for name in CHECK_NAMES:
        assert f"check {name}: ok" in combined
    for token in (
        *INVENTED_WORDS,
        "400567",
        "400678",
        "400345",
        "400456",
        "400123",
        "400234",
        "800,13",
        "12.345,67",
        "11.545,54",
        "1.455,987",
        "900,12",
        "512,34",
        "1.751,68",
        "12,34",
        "75,00",
        "137.284,59",
        "9.713,48",
        "full_statement.txt",
        str(path),
    ):
        assert token not in combined, (token, combined)
    _assert_no_statement_run(FULL, combined)


def test_the_minimal_fixture_counts_its_single_charge(capsys) -> None:
    path = FIXTURES / "minimal_statement.txt"

    assert probe.main(["--text", str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "charges=1" in combined
    assert "payments=0" in combined
    # No payment block at all: the indicator must say absent, never invent one.
    assert "balance=absent" in combined
    assert "surcharge sellos: 2" in combined
    assert "surcharge percepcion: 0" in combined
    _assert_no_statement_run(MINIMAL, combined)


def test_a_balance_chain_failure_prints_the_masked_detail(capsys, tmp_path: Path) -> None:
    """The ten-check surface's masked-detail path, pinned for the new check.

    The ``fail_balance_chain`` fixture breaks only the resulting-ARS relation by
    one cent; the probe must exit one, name the check, and print its masked detail
    (relation names and a page/line, never an amount).
    """
    text = (FIXTURES / "fail_balance_chain.txt").read_text(encoding="utf-8")
    path = tmp_path / "statement.txt"
    path.write_text(text, encoding="utf-8")

    assert probe.main(["--text", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "check balance-chain: FAILED" in combined
    assert "relation" in combined
    for token in (
        *INVENTED_WORDS,
        "12.345,67",
        "11.545,54",
        "11.545,55",
        "800,13",
        "1.455,987",
        "900,12",
        str(path),
        "statement.txt",
    ):
        assert token not in combined, (token, combined)
    _assert_no_statement_run(text, combined)


def test_a_reconciliation_failure_exits_one_with_masked_details(capsys, tmp_path: Path) -> None:
    mutated = MINIMAL.replace(
        "Total Consumos de Tarjeta Zxqv 42,13", "Total Consumos de Tarjeta Zxqv 42,14", 1
    )
    assert mutated != MINIMAL

    path = tmp_path / "statement.txt"
    path.write_text(mutated, encoding="utf-8")

    assert probe.main(["--text", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "check charge-sums-ars: FAILED" in combined
    assert "refused" in combined
    for token in (*INVENTED_WORDS, "42,14", "42,13", str(path), "statement.txt"):
        assert token not in combined, (token, combined)
    _assert_no_statement_run(mutated, combined)


def test_a_structural_failure_exits_one_with_a_masked_parse_error(capsys, tmp_path: Path) -> None:
    broken = "Liquidación de Visa\nTitular Secreto 12345.67\n"
    path = tmp_path / "broken.txt"
    path.write_text(broken, encoding="utf-8")

    assert probe.main(["--text", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "parse: FAILED" in combined
    assert "Secreto" not in combined
    assert str(path) not in combined
    assert path.name not in combined
    _assert_no_statement_run(broken, combined)


def test_a_missing_file_exits_one_without_naming_it(capsys, tmp_path: Path) -> None:
    path = tmp_path / "missing-statement.txt"

    assert probe.main(["--text", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "unreadable" in combined
    assert "FileNotFoundError" in combined
    assert str(path) not in combined
    assert path.name not in combined


def test_non_utf8_text_names_only_the_exception_class(capsys, tmp_path: Path) -> None:
    path = tmp_path / "not-utf8.txt"
    path.write_bytes(b"\xff\xfe\x00\x01synthetic")

    assert probe.main(["--text", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "text extraction failed: UnicodeDecodeError" in combined
    assert str(path) not in combined
    assert path.name not in combined


def test_paths_come_from_the_environment_when_argv_is_empty(
    capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = FIXTURES / "minimal_statement.txt"
    monkeypatch.setenv(probe.ENV_VAR, str(path))

    # ``--text`` with no positional paths: the paths come from the environment.
    assert probe.main(["--text"]) == 0

    captured = capsys.readouterr()
    assert "charges=1" in captured.out


def test_no_paths_is_a_usage_error(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe.ENV_VAR, raising=False)

    assert probe.main([]) == 2

    captured = capsys.readouterr()
    assert "no statement paths given" in captured.out + captured.err
