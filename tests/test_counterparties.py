"""Tests for the learned counterparty map (append-only, ledger-directory-local)."""

from __future__ import annotations

from pathlib import Path

import pytest

from expensuchis.counterparties import CounterpartyError, CounterpartyMap
from expensuchis.ledger import ENV_VAR
from expensuchis.paths import LedgerPaths


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))
    return root


@pytest.fixture
def counterparties(ledger: Path) -> CounterpartyMap:
    return CounterpartyMap()


def test_unmapped_pair_returns_none(counterparties: CounterpartyMap) -> None:
    assert counterparties.resolve("Bbva", "Someone Never Seen") is None


def test_record_then_resolve(counterparties: CounterpartyMap) -> None:
    counterparties.record("Provincia", "FICTICIO INVENTADO J", "internal:Assets:Provincia:P1:Caja")
    counterparties.record("Bbva", "Inventado J Ficticio", "expense:Expenses:Otros")
    assert (
        counterparties.resolve("Provincia", "FICTICIO INVENTADO J")
        == "internal:Assets:Provincia:P1:Caja"
    )
    assert counterparties.resolve("Bbva", "Inventado J Ficticio") == "expense:Expenses:Otros"


def test_key_is_case_sensitive_and_source_scoped(counterparties: CounterpartyMap) -> None:
    counterparties.record("Bbva", "Inventado", "expense:Expenses:Otros")
    assert counterparties.resolve("Bbva", "Inventado") == "expense:Expenses:Otros"
    assert counterparties.resolve("Bbva", "inventado") is None
    assert counterparties.resolve("bbva", "Inventado") is None
    assert counterparties.resolve("Brubank", "Inventado") is None


def test_comments_and_blank_lines_are_ignored(counterparties: CounterpartyMap) -> None:
    counterparties.path.write_text(
        "# a comment header\n"
        "\n"
        "# source\traw_name\tdestination\n"
        "\n"
        "MercadoPago\tNombre Inventado\tinternal:Assets:MercadoPago:P3:Caja\n"
        "   \n"
        "# trailing comment\n",
        encoding="utf-8",
    )
    assert (
        counterparties.resolve("MercadoPago", "Nombre Inventado")
        == "internal:Assets:MercadoPago:P3:Caja"
    )


def test_append_never_rewrites_earlier_rows(counterparties: CounterpartyMap) -> None:
    counterparties.record("Bbva", "Uno", "expense:Expenses:Otros")
    counterparties.record("Bbva", "Dos", "expense:Expenses:Supermercado")

    before = counterparties.path.read_bytes()

    counterparties.record("Bbva", "Tres", "expense:Expenses:Mascotas")
    after = counterparties.path.read_bytes()

    assert after[: len(before)] == before  # earlier bytes are byte-identical
    assert counterparties.resolve("Bbva", "Uno") == "expense:Expenses:Otros"
    assert counterparties.resolve("Bbva", "Dos") == "expense:Expenses:Supermercado"
    assert counterparties.resolve("Bbva", "Tres") == "expense:Expenses:Mascotas"


def test_later_row_wins_for_a_duplicate_key(counterparties: CounterpartyMap) -> None:
    counterparties.record("Bbva", "Corregido", "expense:Expenses:Otros")
    counterparties.record("Bbva", "Corregido", "expense:Expenses:Salud")
    assert counterparties.resolve("Bbva", "Corregido") == "expense:Expenses:Salud"


def test_first_record_writes_a_comment_header(counterparties: CounterpartyMap) -> None:
    counterparties.record("Bbva", "Uno", "expense:Expenses:Otros")
    text = counterparties.path.read_text(encoding="utf-8")
    assert text.startswith("#")
    assert "# Columns" in text


@pytest.mark.parametrize("destination", ["Otros", "internal", "expense", "other:Expenses:Otros"])
def test_record_rejects_a_bad_destination(
    counterparties: CounterpartyMap, destination: str
) -> None:
    with pytest.raises(CounterpartyError):
        counterparties.record("Bbva", "Uno", destination)
    assert not counterparties.path.exists()  # nothing written on failure


def test_bad_destination_in_the_file_names_the_line(counterparties: CounterpartyMap) -> None:
    counterparties.path.write_text(
        "# header\n\nBbva\tUno\texpense:Expenses:Otros\nBbva\tDos\tgarbage:Expenses:Otros\n",
        encoding="utf-8",
    )
    with pytest.raises(CounterpartyError) as excinfo:
        counterparties.resolve("Bbva", "Uno")
    assert "line 4" in str(excinfo.value)


@pytest.mark.parametrize("bad_row", ["Bbva\tUno\n", "Bbva\tUno\texpense:Expenses:Otros\textra\n"])
def test_wrong_column_count_names_the_line(counterparties: CounterpartyMap, bad_row: str) -> None:
    counterparties.path.write_text(
        "# header\nBbva\tUno\texpense:Expenses:Otros\n" + bad_row,
        encoding="utf-8",
    )
    with pytest.raises(CounterpartyError) as excinfo:
        counterparties.resolve("Bbva", "Uno")
    assert "line 3" in str(excinfo.value)


def test_record_rejects_tabbed_or_empty_names(counterparties: CounterpartyMap) -> None:
    with pytest.raises(CounterpartyError):
        counterparties.record("Bbva", "con\ttab", "expense:Expenses:Otros")
    with pytest.raises(CounterpartyError):
        counterparties.record("Bbva", "", "expense:Expenses:Otros")
    assert not counterparties.path.exists()


@pytest.mark.parametrize("source", ["#commented", "  #indented", "#", "#src"])
def test_record_rejects_a_comment_like_source(counterparties: CounterpartyMap, source: str) -> None:
    """A leading ``#`` writes a row the reader would then skip as a comment."""
    with pytest.raises(CounterpartyError, match="comment"):
        counterparties.record(source, "Uno", "expense:Expenses:Otros")
    assert not counterparties.path.exists()


def test_map_is_stored_inside_the_ledger_directory(ledger: Path) -> None:
    paths = LedgerPaths()
    counterparties = CounterpartyMap(paths)
    assert counterparties.path == paths.counterparties()
    counterparties.record("Bbva", "Uno", "expense:Expenses:Otros")
    assert counterparties.path.parent == paths.root
    assert sorted(entry.name for entry in ledger.iterdir()) == ["counterparties.tsv"]
