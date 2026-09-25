"""Tests for the statement-token leak guard.

No real PDF and no real ledger: every statement here is a synthetic text file under
``tmp_path``, and the token source is injected. CI has neither the dependency's
data nor the user's statements, and the repository must stay usable without them.

The one exception is :func:`test_pdfium_text_reads_a_synthetic_text_layer_pdf`,
which builds a minimal text-layer PDF in memory and is **opt-in**: it is marked
``pdfium`` and skipped unless ``EXPENSUCHIS_TEST_PDFIUM=1`` is set, so the default
run never needs ``pypdfium2`` to be importable.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from expensuchis.leakguard import (
    BASELINE_FILENAME,
    EXIT_CODES,
    INDEX_FILENAME,
    GuardStatus,
    build_index,
    evaluate,
    extract_tokens,
    load_baseline,
    pdfium_text,
    scan,
    statement_files,
)
from expensuchis.ledger import ENV_VAR

_PYPIUM_AVAILABLE = importlib.util.find_spec("pypdfium2") is not None

# A minimal, hand-built text-layer PDF: one Helvetica Tj string, no fixture needed.
_SYNTHETIC_CONTENT = b"BT /F1 12 Tf 10 50 Td (InventadoPersona total 123,456) Tj ET"
_SYNTHETIC_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 400 100] "
    b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
    b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
    b"5 0 obj << /Length "
    + str(len(_SYNTHETIC_CONTENT)).encode()
    + b" >> stream\n"
    + _SYNTHETIC_CONTENT
    + b"\nendstream endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
)


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    root = tmp_path / "ledger"
    (root / "statements").mkdir(parents=True)
    return root


def _statement(ledger: Path, name: str, text: str) -> Path:
    path = ledger / "statements" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _text_source(*, failures: tuple[str, ...] = ()) -> tuple[Callable[[Path], str], list[str]]:
    calls: list[str] = []

    def read(path: Path) -> str:
        calls.append(path.name)
        if path.name in failures:
            raise OSError("unreadable for the test")
        return path.read_text(encoding="utf-8")

    return read, calls


def test_extract_tokens_normalises_grouping_and_length() -> None:
    tokens = extract_tokens("Total 123,456 y 123456 y 1.234.567,89; el saldo")
    assert "123456" in tokens  # both groupings collapse to one token
    assert "123456789" in tokens
    assert "saldo" in tokens  # five letters, kept
    assert "total" in tokens  # five letters, kept
    assert "y" not in tokens  # below the length floor
    assert "el" not in tokens


def test_extract_tokens_is_case_folded_and_handles_accents() -> None:
    tokens = extract_tokens("CRÉDITO Credito")
    assert "crédito" in tokens
    assert "credito" in tokens


def test_statement_files_skips_download_metadata_and_dotfiles(ledger: Path) -> None:
    _statement(ledger, "bbva.pdf", "text")
    _statement(ledger, "bbva.pdf:Zone.Identifier", "[ZoneTransfer]")
    _statement(ledger, ".hidden", "text")
    assert [path.name for path in statement_files(ledger)] == ["bbva.pdf"]


def test_truncated_and_recased_statement_phrase_is_caught(ledger: Path) -> None:
    """The leak is a transformation of the source, so phrases do not match."""
    _statement(ledger, "servicios.pdf", "SERVICIO DE ENERGIA FICTICIA S.A.")
    source, _ = _text_source()

    result = evaluate(
        {"notes.md": "cliente de Energia Ficticia"},
        source=source,
        ledger_root=ledger,
    )

    assert result.status is GuardStatus.LEAK
    assert {finding.token for finding in result.findings} == {"energia", "ficticia"}


def test_a_single_word_name_is_caught(ledger: Path) -> None:
    _statement(ledger, "banco.pdf", "Titular: INVENTADOPERSONA")
    source, _ = _text_source()

    result = evaluate({"tracker.md": "filed under InventadoPersona"}, source=source, ledger_root=ledger)

    assert result.status is GuardStatus.LEAK
    assert [finding.token for finding in result.findings] == ["inventadopersona"]


def test_a_decimal_less_amount_is_caught(ledger: Path) -> None:
    _statement(ledger, "resumen.pdf", "Total a pagar 123,456")
    source, _ = _text_source()

    result = evaluate({"notes.md": "the sum was 123456"}, source=source, ledger_root=ledger)

    assert result.status is GuardStatus.LEAK
    assert {finding.token for finding in result.findings} == {"123456"}


def test_a_baseline_token_is_ignored(ledger: Path) -> None:
    _statement(ledger, "resumen.pdf", "CUENTA SALDO")
    (ledger / BASELINE_FILENAME).write_text("# reviewed\ncuenta\nsaldo\n", encoding="utf-8")
    source, _ = _text_source()

    result = evaluate(
        {"docs/model.md": "el saldo de la cuenta"},
        source=source,
        ledger_root=ledger,
    )

    assert result.status is GuardStatus.CLEAN
    assert result.findings == []


def test_a_new_intersection_fails_and_clears_only_when_baselined(ledger: Path) -> None:
    _statement(ledger, "resumen.pdf", "PROVEEDORINVENTADO")
    source, _ = _text_source()
    texts = {"notes.md": "pago a ProveedorInventado"}

    first = evaluate(texts, source=source, ledger_root=ledger)
    assert first.status is GuardStatus.LEAK

    (ledger / BASELINE_FILENAME).write_text("proveedorinventado\n", encoding="utf-8")
    second = evaluate(texts, source=source, ledger_root=ledger)
    assert second.status is GuardStatus.CLEAN


def test_finding_names_the_token_the_file_and_the_statement(ledger: Path) -> None:
    _statement(ledger, "Bbva/agosto.pdf", "PROVEEDORINVENTADO")
    source, _ = _text_source()

    result = evaluate({"odd/tasks/x.md": "ProveedorInventado"}, source=source, ledger_root=ledger)

    (finding,) = result.findings
    assert finding.token == "proveedorinventado"
    assert finding.file == "odd/tasks/x.md"
    assert finding.statements == ("statements/Bbva/agosto.pdf",)


def test_scan_reports_one_finding_per_file_but_not_per_occurrence(ledger: Path) -> None:
    _statement(ledger, "resumen.pdf", "PROVEEDORINVENTADO")
    source, _ = _text_source()
    index = build_index(ledger, source=source)

    findings = scan(
        {"a.md": "ProveedorInventado y ProveedorInventado", "b.md": "ProveedorInventado"},
        index,
        set(),
    )

    assert [(f.file, f.token) for f in findings] == [
        ("a.md", "proveedorinventado"),
        ("b.md", "proveedorinventado"),
    ]


def test_cache_is_reused_then_invalidated_by_a_statement_change(ledger: Path) -> None:
    statement = _statement(ledger, "resumen.pdf", "CUENTA FICTICIA")
    source, calls = _text_source()

    build_index(ledger, source=source)
    assert calls == ["resumen.pdf"]

    build_index(ledger, source=source)
    assert calls == ["resumen.pdf"]  # cache hit, no re-read
    assert (ledger / INDEX_FILENAME).is_file()

    # A changed modification time invalidates the cache.
    current = statement.stat()
    os.utime(statement, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
    build_index(ledger, source=source)
    assert calls == ["resumen.pdf", "resumen.pdf"]

    # force=True rebuilds regardless.
    build_index(ledger, source=source, force=True)
    assert calls == ["resumen.pdf", "resumen.pdf", "resumen.pdf"]


def test_cache_is_invalidated_when_a_statement_is_added(ledger: Path) -> None:
    _statement(ledger, "a.pdf", "CUENTA")
    source, calls = _text_source()
    build_index(ledger, source=source)
    assert calls == ["a.pdf"]

    _statement(ledger, "b.pdf", "SALDO")
    build_index(ledger, source=source)
    assert calls == ["a.pdf", "a.pdf", "b.pdf"]


def test_index_and_baseline_live_in_the_ledger_directory_only(ledger: Path) -> None:
    _statement(ledger, "resumen.pdf", "CUENTA")
    source, _ = _text_source()
    build_index(ledger, source=source, force=True)
    (ledger / BASELINE_FILENAME).write_text("cuenta\n", encoding="utf-8")

    assert (ledger / INDEX_FILENAME).parent == ledger
    assert (ledger / BASELINE_FILENAME).parent == ledger
    assert not (ledger / "statements" / INDEX_FILENAME).exists()
    assert load_baseline(ledger) == {"cuenta"}


def test_absent_ledger_directory_allows_the_commit_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)

    result = evaluate({"notes.md": "anything at all"})

    assert result.status is GuardStatus.CANNOT_RUN
    assert "cannot run" in result.message.lower()
    assert "absent" in result.message.lower()
    assert EXIT_CODES[GuardStatus.CANNOT_RUN.value] == 0


def test_ledger_path_that_does_not_exist_also_counts_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "not-created"))

    result = evaluate({"notes.md": "anything at all"})

    assert result.status is GuardStatus.CANNOT_RUN
    assert "does not exist" in result.message


def test_configured_ledger_without_a_statements_directory_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    root.mkdir()

    result = evaluate({"notes.md": "anything"}, ledger_root=root)

    assert result.status is GuardStatus.UNREADABLE
    assert "statements" in result.message
    assert EXIT_CODES[GuardStatus.UNREADABLE.value] != 0


def test_an_unreadable_statement_fails_closed(ledger: Path) -> None:
    _statement(ledger, "broken.pdf", "irrelevant")
    source, _ = _text_source(failures=("broken.pdf",))

    result = evaluate({"notes.md": "anything"}, source=source, ledger_root=ledger)

    assert result.status is GuardStatus.UNREADABLE
    assert "broken.pdf" in result.message


def test_empty_statements_directory_fails_closed(ledger: Path) -> None:
    source, _ = _text_source()

    result = evaluate({"notes.md": "anything"}, source=source, ledger_root=ledger)

    assert result.status is GuardStatus.UNREADABLE
    assert "no statement tokens" in result.message


def test_ledger_directory_inside_the_repository_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv(ENV_VAR, str(repo_root))

    result = evaluate({"notes.md": "anything"})

    assert result.status is GuardStatus.UNREADABLE


def test_clean_run_reports_zero_exit_code(ledger: Path) -> None:
    _statement(ledger, "resumen.pdf", "PROVEEDORINVENTADO")
    source, _ = _text_source()

    result = evaluate({"notes.md": "unrelated prose"}, source=source, ledger_root=ledger)

    assert result.status is GuardStatus.CLEAN
    assert EXIT_CODES[GuardStatus.CLEAN.value] == 0


@pytest.mark.pdfium
@pytest.mark.skipif(not _PYPIUM_AVAILABLE, reason="pypdfium2 is not installed")
@pytest.mark.skipif(
    not os.environ.get("EXPENSUCHIS_TEST_PDFIUM"),
    reason="opt-in: set EXPENSUCHIS_TEST_PDFIUM=1 to exercise the real reader",
)
def test_pdfium_text_reads_a_synthetic_text_layer_pdf(tmp_path: Path) -> None:
    """Exercise the production reader without any private statement."""
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(_SYNTHETIC_PDF)

    text = pdfium_text(path)

    assert "InventadoPersona" in text
    assert extract_tokens(text) >= {"inventadopersona", "123456"}
