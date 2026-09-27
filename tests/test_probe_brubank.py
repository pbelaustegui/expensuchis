"""Tests for the local acceptance probe (``tools/probe_brubank.py``).

The probe is the only component that reads a real Brubank statement, so its output
surface is a privacy control: it must print aggregates only and never a date,
description, ``#Ref``, amount or balance value, or the statement's filename. These
tests exercise that surface with the synthetic fixtures under
``tests/fixtures/brubank/`` (reused from the parser suite, per the T-08 convention) and
with mutated/inline text, asserting on stdout and stderr together as ``capsys``
captures them.

Half of this file proves the probe reports the ordinary aggregates and never leaks
statement content (mirroring ``test_probe_provincia_visa.py``); the other half is
T-08c's own job: it pins the two shape facts the parser itself never exposes (whether
a date-prefixed line ever falls outside a table region, and how many regions closed by
the footer versus by a header field) and the ``Intereses pagados`` direction count, all
derived without changing the parser's public surface (``tools/probe_brubank.py`` reuses
``brubank``'s own module-level regexes and helpers for the shape scan). The
``multipage.txt`` fixture also carries the real file's merged two-column header lines
and its optional quiescent USD account block (see ``odd/tasks/family-ledger.md``, the
2026-09-27 correction to the T-08 reconnaissance).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from expensuchis.importers.brubank import CHECK_NAMES, MovementKind

PROBE_PATH = Path(__file__).resolve().parents[1] / "tools" / "probe_brubank.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "brubank"

MINIMAL = (FIXTURES / "minimal.txt").read_text(encoding="utf-8")
MULTIPAGE = (FIXTURES / "multipage.txt").read_text(encoding="utf-8")

# Statement words the fixtures invent; none of them is a document-template word, so
# none of them may ever appear in the probe's output.
INVENTED_WORDS = ("Persona Ejemplo", "Aguas Ejemplo", "Electrica Ejemplo", "BBVA")

#: The shortest run of statement text that must never reach the probe's output. Any
#: longer run contains a run of this length, so checking every fixed-length window
#: catches a leaked excerpt whether it is printed whole or truncated by a mutant.
_MIN_TEXT_RUN = 12

#: Phrases the probe legitimately prints that also happen to appear, in some form,
#: inside the fixtures' template vocabulary (the frozen movement-kind labels). A
#: window that is a substring of one of these is allowed; every other window is
#: statement content and must never be printed.
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
    # The probe module defines its own frozen dataclass (``_RegionShape``), whose
    # machinery looks itself up in ``sys.modules`` by ``__module__`` name; register it
    # before executing, the same registration a normal ``import`` performs for us.
    spec = importlib.util.spec_from_file_location("probe_brubank", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


# --------------------------------------------------------------------------- success


def test_the_multipage_fixture_prints_aggregates_and_no_statement_content(capsys) -> None:
    path = FIXTURES / "multipage.txt"

    assert probe.main(["--text", str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "pages=4" in combined
    assert "movements=5" in combined
    assert "movement ordinary: 3" in combined
    assert "movement intereses: 1" in combined
    assert "movement internal_transfer: 1" in combined
    assert "intereses-pagados credit: 1" in combined
    assert "intereses-pagados debit: 0" in combined
    for name in CHECK_NAMES:
        assert f"check {name}: ok" in combined
    for token in (
        *INVENTED_WORDS,
        "3141592653",
        "2718281828",
        "1618033988",
        "1414213562",
        "2236067977",
        "9.876,54",
        "1.234,56",
        "2.500,00",
        "321,45",
        "678,90",
        "multipage.txt",
        str(path),
    ):
        assert token not in combined, (token, combined)
    _assert_no_statement_run(MULTIPAGE, combined)


def test_the_minimal_fixture_counts_its_two_movements(capsys) -> None:
    path = FIXTURES / "minimal.txt"

    assert probe.main(["--text", str(path)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "movements=2" in combined
    assert "movement ordinary: 2" in combined
    assert "movement intereses: 0" in combined
    assert "movement internal_transfer: 0" in combined
    assert "intereses-pagados credit: 0" in combined
    assert "intereses-pagados debit: 0" in combined
    _assert_no_statement_run(MINIMAL, combined)


# ------------------------------------------------------------- T-08c shape facts (1)+(2)


def test_the_multipage_fixture_reports_two_footer_closes_and_no_header_close(capsys) -> None:
    """Fact (2): the table pages (1 and 2) both close on the footer's period line.

    The USD account block on page 3 never opens a table region of its own (it
    has no movement rows), so no region ever closes on a header field.
    """
    path = FIXTURES / "multipage.txt"

    assert probe.main(["--text", str(path)]) == 0

    combined = "".join(capsys.readouterr())
    assert "shape stray-date-lines-outside-region: 0" in combined
    assert "shape regions-closed-by-footer: 2" in combined
    assert "shape regions-closed-by-header: 0" in combined
    assert "shape regions-closed-by-end-of-page: 0" in combined


def test_the_minimal_fixture_closes_its_only_region_by_footer(capsys) -> None:
    """Fact (2): the minimal fixture's single table region closes on the footer's period line."""
    path = FIXTURES / "minimal.txt"

    assert probe.main(["--text", str(path)]) == 0

    combined = "".join(capsys.readouterr())
    assert "shape stray-date-lines-outside-region: 0" in combined
    assert "shape regions-closed-by-footer: 1" in combined
    assert "shape regions-closed-by-header: 0" in combined
    assert "shape regions-closed-by-end-of-page: 0" in combined


def test_a_stray_date_line_outside_a_region_is_counted_and_refuses(capsys, tmp_path: Path) -> None:
    """Fact (1): a date-prefixed line on the legal-prose page (no table ever opens there)."""
    mutated = MULTIPAGE + "15-09-26 nota generada automaticamente\n"
    path = tmp_path / "statement.txt"
    path.write_text(mutated, encoding="utf-8")

    assert probe.main(["--text", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "class: ResumenParseError" in combined
    assert "cause: date-prefixed line outside a table region" in combined
    assert "shape stray-date-lines-outside-region: 1" in combined
    for token in (*INVENTED_WORDS, "nota generada automaticamente", str(path), "statement.txt"):
        assert token not in combined, (token, combined)


# ----------------------------------------------------------- T-08c fact (3): direction


def test_an_intereses_pagados_debit_row_is_counted_by_direction(capsys, tmp_path: Path) -> None:
    """Fact (3): T-08b assumed this never happens; the probe must still be able to see it."""
    mutated = MINIMAL.replace(
        "02-09-26 2345678901 Aguas Ejemplo 1.200,50 - 12.299,50",
        "02-09-26 2345678901 Intereses pagados 1.200,50 - 12.299,50",
        1,
    )
    assert mutated != MINIMAL
    path = tmp_path / "statement.txt"
    path.write_text(mutated, encoding="utf-8")

    assert probe.main(["--text", str(path)]) == 0

    combined = "".join(capsys.readouterr())
    assert "movement intereses: 1" in combined
    assert "intereses-pagados credit: 0" in combined
    assert "intereses-pagados debit: 1" in combined


# --------------------------------------------------------------------------- failures


def test_a_reconciliation_failure_exits_one_with_masked_details(capsys, tmp_path: Path) -> None:
    # Replace both the opening and recap copies identically, so the header stays
    # internally consistent (``ResumenParseError`` would fire on a mismatch between
    # them) while the balance equation and the declared-credits sum both go wrong.
    mutated = MINIMAL.replace("Créditos $ 3.500,00", "Créditos $ 3.500,01")
    assert mutated != MINIMAL

    path = tmp_path / "statement.txt"
    path.write_text(mutated, encoding="utf-8")

    assert probe.main(["--text", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "class: ReconciliationError" in combined
    assert "check header-balance-equation: FAILED" in combined
    assert "refused" in combined
    for token in (*INVENTED_WORDS, "3.500,01", "3.500,00", str(path), "statement.txt"):
        assert token not in combined, (token, combined)
    _assert_no_statement_run(mutated, combined)


def test_a_structural_failure_exits_one_with_a_masked_parse_error(capsys, tmp_path: Path) -> None:
    broken = "Resumen de Movimientos\nSaldo Inicial 100\n"
    path = tmp_path / "broken.txt"
    path.write_text(broken, encoding="utf-8")

    assert probe.main(["--text", str(path)]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "class: ResumenParseError" in combined
    assert "parse: FAILED" in combined
    assert str(path) not in combined
    assert path.name not in combined


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
    path = FIXTURES / "minimal.txt"
    monkeypatch.setenv(probe.ENV_VAR, str(path))

    assert probe.main(["--text"]) == 0

    captured = capsys.readouterr()
    assert "movements=2" in captured.out


def test_no_paths_is_a_usage_error(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe.ENV_VAR, raising=False)

    assert probe.main([]) == 2

    captured = capsys.readouterr()
    assert "no statement paths given" in captured.out + captured.err
