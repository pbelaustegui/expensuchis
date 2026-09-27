"""Tests for the local acceptance probe (``tools/probe_provincia.py``).

The probe is the only component that reads a real statement, so its output surface
is a privacy control: it must print aggregates only and never a date, description,
reference, amount, balance or filename. These tests exercise that surface with
the synthetic fixtures under ``tests/fixtures/provincia/`` and with broken input,
asserting on stdout and stderr together as ``capsys`` captures them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from expensuchis.importers.provincia import MovementKind

PROBE_PATH = Path(__file__).resolve().parents[1] / "tools" / "probe_provincia.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "provincia"

DESCRIBED = (FIXTURES / "described_rows.txt").read_text(encoding="utf-8")

# Statement words the fixtures invent; none of them is a document-template word, so
# none of them may ever appear in the probe's output.
INVENTED_WORDS = ("Zxqv", "Qwerty", "Plugh")

#: The shortest run of statement text that must never reach the probe's output. Any
#: longer run contains a run of this length, so checking every fixed-length window
#: catches a leaked excerpt whether it is printed whole or truncated by a mutant.
_MIN_TEXT_RUN = 12


def _forbidden_text_runs(text: str) -> set[str]:
    """Every ``_MIN_TEXT_RUN``-character window of ``text`` that is not template.

    The probe legitimately prints the frozen movement vocabulary (its ``kind``
    lines), which also appears in the statement, so windows inside those printed
    phrases are allowed; every other window is statement content and must never be
    printed.
    """
    windows = {
        text[index : index + _MIN_TEXT_RUN] for index in range(len(text) - _MIN_TEXT_RUN + 1)
    }
    vocabulary = tuple(f"  kind {kind.value}" for kind in MovementKind)
    return {window for window in windows if not any(window in phrase for phrase in vocabulary)}


def _assert_no_statement_run(text: str, combined: str) -> None:
    leaked = sorted(run for run in _forbidden_text_runs(text) if run in combined)
    assert leaked == [], leaked


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_provincia", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def test_good_fixture_prints_aggregates_and_no_statement_content(capsys) -> None:
    path = FIXTURES / "described_rows.txt"

    assert probe.main(["--text", str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "movements=9" in combined
    assert "pages=1" in combined
    assert "check running-balance-chain: ok" in combined
    assert "check vocabulary-known: ok" in combined
    for token in (
        *INVENTED_WORDS,
        "Nº.12345",
        "2500.13",
        "1000.37",
        "01/01/2026",
        "02/01/2026",
        "described_rows.txt",
        str(path),
    ):
        assert token not in combined, (token, combined)
    _assert_no_statement_run(DESCRIBED, combined)


def test_the_reference_fixture_counts_the_reference_kind(capsys) -> None:
    path = FIXTURES / "reference_rows.txt"

    assert probe.main(["--text", str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "movements=3" in combined
    assert "kind reference: 3" in combined
    _assert_no_statement_run(
        (FIXTURES / "reference_rows.txt").read_text(encoding="utf-8"), combined
    )


def test_a_reconciliation_failure_exits_one_with_masked_details(capsys, tmp_path: Path) -> None:
    text = DESCRIBED
    mutated = text.replace("SALDO ANTERIOR 1000.37", "SALDO ANTERIOR 1000.38", 1)
    assert mutated != text

    path = tmp_path / "statement.txt"
    path.write_text(mutated, encoding="utf-8")

    assert probe.main(["--text", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "FAILED" in combined
    for token in (*INVENTED_WORDS, "1000.38", "1000.37", str(path), "statement.txt"):
        assert token not in combined, (token, combined)
    _assert_no_statement_run(mutated, combined)


def test_a_structural_failure_exits_one_with_a_masked_parse_error(capsys, tmp_path: Path) -> None:
    broken = "Extracto de Cuenta\nTitular Secreto 12345.67\n"
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


def test_no_paths_is_a_usage_error(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe.ENV_VAR, raising=False)

    assert probe.main([]) == 2

    captured = capsys.readouterr()
    assert "no statement paths given" in captured.out + captured.err
