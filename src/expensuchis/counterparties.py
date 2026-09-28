"""The learned counterparty map (personal data, so it lives in the ledger directory).

The same person is identified differently in every entity: P1 is
``APELLIDO NOMBRE J`` at Provincia, ``Nombre Apellido`` at BBVA and
``Nombre`` in Mercado Pago (these are placeholders, not real names). A canonical
name list would fail, so the map is keyed by the pair ``(source, raw_name)`` that
the importer supplies, and it is case-sensitive on purpose. Mercado Pago supplies
the raw statement name; the Provincia account extracto supplies a *normalized*
counterparty identity (the merchant without the per-row id and date/time), so one
merchant resolves across rows that differ only in that noise.

The names are personal data, so the map is a flat, append-only TSV inside
``EXPENSUCHIS_LEDGER_DIR``, next to the ledger and never in this public
repository. The file is hand-editable, so :meth:`CounterpartyMap.record` appends
one row and never rewrites: a rewrite could lose a user's manual edit.

Layout::

    # comment header
    source<TAB>raw_name<TAB>destination

``destination`` is either ``internal:<account>`` or ``expense:<category>``.
Anything else is a malformed row and raises.

An ``internal:`` destination means the counterparty is an account **in this
ledger**, which is what keeps a family transfer from becoming spending: money that
moves between accounts here is not an expense, while ``expense:`` is money that
leaves the household for good.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .paths import LedgerPaths

__all__ = ["CounterpartyError", "CounterpartyMap"]

_INTERNAL = "internal"
_EXPENSE = "expense"
S_DESTINATIONS = frozenset({_INTERNAL, _EXPENSE})

HEADER = (
    "# Expensuchis counterparty map. Append-only; one classification per row.\n"
    "# Columns (tab-separated): source\traw_name\tdestination\n"
    "# destination is either internal:<account> or expense:<category>.\n"
)


class CounterpartyError(ValueError):
    """Raised for a malformed row or an invalid destination."""


def _validate_destination(destination: str, line_number: int | None) -> str:
    location = f" at line {line_number}" if line_number is not None else ""
    prefix, separator, value = destination.partition(":")
    if separator != ":" or prefix not in S_DESTINATIONS or not value:
        raise CounterpartyError(
            f"Malformed destination {destination!r}{location}: expected "
            f"'internal:<account>' or 'expense:<category>'."
        )
    if any(character.isspace() for character in value):
        raise CounterpartyError(
            f"Malformed destination {destination!r}{location}: the account or "
            f"category must not contain whitespace."
        )
    return destination


def _validate_field(value: str, name: str) -> str:
    if not value or "\t" in value or "\n" in value or "\r" in value:
        raise CounterpartyError(f"Malformed {name} {value!r}: it must be a non-empty single field.")
    return value


class CounterpartyMap:
    """Read and append classifications in the ledger's ``counterparties.tsv``.

    The path is obtained through :class:`~expensuchis.paths.LedgerPaths`, so the
    per-path repository guard refuses a map whose path resolves inside a
    discoverable repository. That guard prevents *repository leaks*; it does not
    prevent *redirection*. A ``counterparties.tsv`` that is itself a symlink to a
    file elsewhere redirects the append, because the guard only checks where the
    resolved path lives relative to known repositories, not that the file is a
    regular file owned by the ledger.
    """

    def __init__(self, paths: LedgerPaths | None = None) -> None:
        self._paths = paths if paths is not None else LedgerPaths()

    @property
    def path(self) -> Path:
        """The validated map file path (it may not exist yet)."""
        return self._paths.counterparties()

    def _rows(self) -> Iterator[tuple[str, str, str]]:
        path = self.path
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                row = line.rstrip("\r\n")
                if not row.strip() or row.lstrip().startswith("#"):
                    continue
                columns = row.split("\t")
                if len(columns) != 3:
                    raise CounterpartyError(
                        f"Malformed row at line {line_number}: expected 3 "
                        f"tab-separated columns, found {len(columns)}."
                    )
                source, raw_name, destination = columns
                yield source, raw_name, _validate_destination(destination, line_number)

    def resolve(self, source: str, raw_name: str) -> str | None:
        """Return the destination for ``(source, raw_name)``, or ``None`` if unmapped.

        The key is exact and case-sensitive. When the same key appears more than
        once the last row wins, which is how a correction appended by hand takes
        effect without rewriting the file.
        """
        destination: str | None = None
        for row_source, row_name, row_destination in self._rows():
            if row_source == source and row_name == raw_name:
                destination = row_destination
        return destination

    def record(self, source: str, raw_name: str, destination: str) -> None:
        """Append one classification; never rewrite existing rows.

        The destination is validated before anything is written, so a bad
        argument raises without touching the file. A ``source`` with a leading
        ``#`` is rejected on purpose: the reader treats a ``#`` at the start of a
        line as a comment, so such a row would be written and then silently never
        read back.
        """
        _validate_field(source, "source")
        if source.lstrip().startswith("#"):
            raise CounterpartyError(
                f"Malformed source {source!r}: a leading '#' makes the row a "
                f"comment, so the classification would be written but never read."
            )
        _validate_field(raw_name, "raw name")
        _validate_destination(destination, None)

        path = self.path
        starts_empty = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            if starts_empty:
                handle.write(HEADER)
            handle.write(f"{source}\t{raw_name}\t{destination}\n")
