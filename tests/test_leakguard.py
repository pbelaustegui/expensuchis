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
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from expensuchis.leakguard import (
    BASELINE_FILENAME,
    EXIT_CODES,
    INDEX_FILENAME,
    GuardStatus,
    build_index,
    derived_legitimate_numbers,
    evaluate,
    extract_tokens,
    format_finding,
    format_result,
    load_baseline,
    main,
    pdfium_text,
    scan,
    statement_files,
)
from expensuchis.ledger import ENV_VAR
from expensuchis.redact import content_hash

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
    # Invented words on purpose: a real accented statement token here would have to
    # be baselined just to keep the repository clean under its own guard.
    tokens = extract_tokens("ZÚRTANO Zurtano")
    assert "zúrtano" in tokens
    assert "zurtano" in tokens


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
        {"notes.md": "zafrante de Energia Ficticia"},
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
    _statement(ledger, "resumen.pdf", "Total a pagorrar 123,456")
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
    _statement(ledger, "Bbva/fictimbre.pdf", "PROVEEDORINVENTADO")
    source, _ = _text_source()

    result = evaluate({"odd/tasks/x.md": "ProveedorInventado"}, source=source, ledger_root=ledger)

    (finding,) = result.findings
    assert finding.token == "proveedorinventado"
    assert finding.file == "odd/tasks/x.md"
    assert finding.line == 1
    assert finding.statements == ("statements/Bbva/fictimbre.pdf",)


def test_default_finding_output_is_redacted_but_locatable(ledger: Path) -> None:
    """T-01d: a finding names a shape, a hash and a location -- never the token or the path."""
    _statement(ledger, "resumen.pdf", "PROVEEDORINVENTADO")
    source, _ = _text_source()

    result = evaluate(
        {"odd/tasks/x.md": "pago a ProveedorInventado"},
        source=source,
        ledger_root=ledger,
    )
    assert result.status is GuardStatus.LEAK

    output = format_result(result, reveal=False)

    assert "proveedorinventado" not in output.lower()
    assert "resumen.pdf" not in output
    assert "statements/resumen.pdf" not in output
    assert content_hash(b"proveedorinventado") in output
    assert "18-letter word" in output
    assert "odd/tasks/x.md:1" in output


def test_reveal_shows_the_raw_token_and_the_statement_path(ledger: Path) -> None:
    """T-01d: ``--reveal`` restores the full output, for the owner at a terminal only."""
    _statement(ledger, "resumen.pdf", "PROVEEDORINVENTADO")
    source, _ = _text_source()

    result = evaluate(
        {"odd/tasks/x.md": "pago a ProveedorInventado"},
        source=source,
        ledger_root=ledger,
    )

    output = format_result(result, reveal=True)

    assert "proveedorinventado" in output
    assert "statements/resumen.pdf" in output
    assert "odd/tasks/x.md:1" in output


def test_a_clean_result_formats_with_no_finding_lines(ledger: Path) -> None:
    _statement(ledger, "resumen.pdf", "PROVEEDORINVENTADO")
    source, _ = _text_source()

    result = evaluate({"notes.md": "unrelated prose"}, source=source, ledger_root=ledger)

    assert format_result(result, reveal=False) == result.message


def test_format_finding_redacted_shows_digit_shape_for_a_numeric_token(ledger: Path) -> None:
    _statement(ledger, "resumen.pdf", "Total a pagorrar 123,456")
    source, _ = _text_source()
    index = build_index(ledger, source=source)
    (finding,) = scan({"notes.md": "the sum was 123456"}, index, set())

    line = format_finding(finding, reveal=False)

    assert "123456" not in line
    assert content_hash(b"123456") in line
    assert "6-digit number" in line
    assert "notes.md:1" in line


def test_reveal_flag_is_documented_in_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    help_text = capsys.readouterr().out
    assert "--reveal" in help_text


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


def test_ledger_path_set_but_missing_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo must not silently disable the guard.

    An unset variable is CANNOT_RUN so CI and fresh clones keep working, but a
    *set* path that does not exist is a typo and is UNREADABLE (exit 2).
    """
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "not-created"))

    result = evaluate({"notes.md": "anything at all"})

    assert result.status is GuardStatus.UNREADABLE
    assert "does not exist" in result.message
    assert EXIT_CODES[GuardStatus.UNREADABLE.value] == 2


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


_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_baseline_reasons_after_hash_are_ignored(ledger: Path) -> None:
    (ledger / BASELINE_FILENAME).write_text(
        "cuenta  # generic banking vocabulary\n"
        "# a whole-line comment\n"
        "saldo\n",
        encoding="utf-8",
    )

    assert load_baseline(ledger) == {"cuenta", "saldo"}


def test_a_numeric_token_below_six_digits_is_not_flagged(ledger: Path) -> None:
    _statement(ledger, "resumen.pdf", "12345")
    source, _ = _text_source()

    result = evaluate({"notes.md": "12345"}, source=source, ledger_root=ledger)

    assert result.status is GuardStatus.CLEAN


def test_derived_legitimate_numbers_are_read_from_the_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "sample").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "uv.lock").write_text("version = 999999\n", encoding="utf-8")
    (repo / "sample" / "model.beancount").write_text("10,000,000.00 ARS\n", encoding="utf-8")
    (repo / "docs" / "model.md").write_text("a 25,000,000.00 example\n", encoding="utf-8")

    numbers = derived_legitimate_numbers(repo)

    assert {"999999", "1000000000", "2500000000"} <= numbers


def test_numbers_from_uv_lock_sample_and_docs_are_not_flagged(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    (ledger / "statements").mkdir(parents=True)
    _statement(ledger, "resumen.pdf", "irrelevant because the source below is injected")
    repo = tmp_path / "repo"
    (repo / "sample").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "uv.lock").write_text("version = 999999\n", encoding="utf-8")
    (repo / "sample" / "model.beancount").write_text("10,000,000.00 ARS\n", encoding="utf-8")
    (repo / "docs" / "model.md").write_text("a 25,000,000.00 example\n", encoding="utf-8")

    def source(path: Path) -> str:
        return "999999 1000000000 2500000000 888888"

    result = evaluate(
        {"notes.md": "999999 1000000000 2500000000 888888"},
        source=source,
        ledger_root=ledger,
        repo_root=repo,
    )

    assert result.status is GuardStatus.LEAK
    assert {finding.token for finding in result.findings} == {"888888"}


# --- Hook integration -------------------------------------------------------------------
#
# The commit-message scan is exercised through the real `.githooks/commit-msg`
# invocation form (message file as ``$1``), not a direct module call, because that
# is the integration that was broken. A fake ``uv`` forwards to this interpreter so
# the hook's real dispatch is tested without a nested ``uv`` run.


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _forwarding_uv() -> str:
    return f'#!/usr/bin/env bash\nset -u\nexec "{sys.executable}" "${{@:3}}"\n'


def _seeded_ledger_with_unreadable_baseline(ledger: Path) -> None:
    """Seed a valid index, then make the reviewed baseline unreadable so the guard crashes.

    ``load_baseline`` reads ``leakguard-baseline.txt`` with no exception handling,
    so a directory at that path raises ``IsADirectoryError``. That exception
    propagates out of ``main`` and the module's ``__main__`` handler turns it into
    exit 3. The crash is produced by the real module, not by a fake exit code.
    """
    _seeded_ledger(ledger)
    (ledger / BASELINE_FILENAME).mkdir()


def _hook_environment(tmp_path: Path, ledger: Path, uv_body: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    _write_executable(bindir / "uv", uv_body)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env[ENV_VAR] = str(ledger)
    return env


def _scratch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def _hook(name: str) -> Path:
    return _REPO_ROOT / ".githooks" / name


def _seeded_ledger(ledger: Path) -> None:
    _statement(ledger, "resumen.pdf", "placeholder read from the seeded index")
    build_index(ledger, source=lambda path: "PROVEEDORINVENTADO", force=True)


def test_commit_msg_hook_blocks_a_statement_token_in_the_message(
    tmp_path: Path, ledger: Path
) -> None:
    _seeded_ledger(ledger)
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("note: counterparty ProveedorInventado confirmed\n", encoding="utf-8")
    env = _hook_environment(tmp_path, ledger, _forwarding_uv())

    completed = subprocess.run(
        [str(_hook("commit-msg")), str(message)],
        cwd=_scratch_repo(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr
    assert "leaked into the commit message" in completed.stderr


def test_commit_msg_hook_allows_a_benign_message(tmp_path: Path, ledger: Path) -> None:
    _seeded_ledger(ledger)
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("chore: tidy the fixtures\n", encoding="utf-8")
    env = _hook_environment(tmp_path, ledger, _forwarding_uv())

    completed = subprocess.run(
        [str(_hook("commit-msg")), str(message)],
        cwd=_scratch_repo(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_commit_msg_hook_fails_closed_without_a_message_file(
    tmp_path: Path, ledger: Path
) -> None:
    env = _hook_environment(tmp_path, ledger, _forwarding_uv())

    completed = subprocess.run(
        [str(_hook("commit-msg"))],
        cwd=_scratch_repo(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "no readable commit-message file" in completed.stderr


def test_pre_commit_hook_reports_a_leakguard_crash_as_a_crash(
    tmp_path: Path, ledger: Path
) -> None:
    """Exercise the real crash-to-3 path in ``leakguard.__main__``.

    The forwarding ``uv`` runs the real module. The ledger index is seeded so it
    loads from cache, then the baseline path is made a directory, which makes
    ``load_baseline`` raise ``IsADirectoryError``. The hook must report exit 3 as a
    crash and must not call it a leak. A fake ``uv`` that hard-codes 3 is not used,
    because that would leave the module's crash handler with zero coverage.
    """
    _seeded_ledger_with_unreadable_baseline(ledger)
    env = _hook_environment(tmp_path, ledger, _forwarding_uv())

    completed = subprocess.run(
        [str(_hook("pre-commit"))],
        cwd=_scratch_repo(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr
    assert "CRASHED" in completed.stderr
    assert "crashed" in completed.stderr.lower()
    assert "leaked" not in completed.stderr.lower()
