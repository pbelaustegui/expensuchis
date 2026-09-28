"""Local acceptance probe for the BBVA Visa/Mastercard card importer, run by a human.

This is the only part of the workflow that touches a **real** statement, and it
is run locally so the household's data never enters an agent's context. The
models that drive this repository are remote APIs, so anything printed here may
end up in a transcript: that is why the probe prints **aggregates only** and
why it exists as a separate program instead of "just parse it in the REPL".

It never prints a movement's date, description, merchant name, comprobante,
amount, balance or the statement's filename. It prints, per statement:

* the sha256 of the file's bytes truncated to 12 hex characters and the page
  count;
* whether the file is even a card ``Liquidación`` at all
  (:func:`~expensuchis.importers.bbva_card.is_bbva_card_liquidacion`) -- the
  real statement folder also holds the account ``Extracto consolidado``,
  which this probe does not parse; a non-match is reported as ``not a card
  liquidación`` and the run moves on to the next file rather than raising,
  the mirror image of ``probe_bbva.py``'s own treatment of card PDFs;
* the brand (visa/mastercard);
* the count per :class:`~expensuchis.importers.bbva_card.MovementKind`
  (payments, purchases, charges) across the whole statement;
* the number of entries :func:`~expensuchis.importers.bbva_card_importer.build_entries`
  builds, using a **stub** :class:`~expensuchis.counterparties.CounterpartyMap`
  that resolves every identity to a fixed ``expense:<category>`` destination --
  this probe measures the importer's *shape*, never its classification, so a
  :class:`~expensuchis.importers.bbva_card_importer.CounterpartyClassificationError`
  escaping anyway is a bug in the probe or the importer, not an expected
  outcome, and is allowed to propagate loudly;
* entries per card liability account (Visa ARS, Visa USD, Mastercard ARS,
  Mastercard USD) -- counts only, never balances;
* the count of installment plans (one entry per plan, T-06b's model);
* charges by kind (IVA / Percepciones counts);
* holder-metadata counts (entries carrying ``holder`` meta vs not);
* the count of distinct merchant identities resolved (a count, never a name);
* a natural-key uniqueness check over every built entry, reported
  ``natural-keys: ok`` or ``natural-keys: FAILED (duplicates: N)``;
* one line per reconciliation check with ``ok`` or ``FAILED (<masked
  detail>)``; on a :class:`~expensuchis.importers.bbva_card.ReconciliationError`
  it prints the checks and fails the file; on a
  :class:`~expensuchis.importers.bbva_card.CardLiquidacionParseError` it
  prints the masked message and fails the file.

It exits non-zero when any statement fails to parse, reconcile, or comes out
with a duplicate natural key. A file that is not a card ``Liquidación`` at all
is reported, not a failure.

Usage::

    python tools/probe_bbva_cards.py statement.pdf [statement.pdf ...]
    EXPENSUCHIS_BBVA_CARD_STATEMENTS=a.pdf:b.pdf python tools/probe_bbva_cards.py

``--rows`` reads an already-extracted positioned-rows text file (the same
custom format ``tests/test_bbva_card_parser.py`` builds its fixtures with:
``PAGE n`` starts a page, ``ROW`` starts a row, and every other line is
``x0 x1 token text``). It exists so the probe itself can be exercised against
the synthetic fixtures in ``tests/fixtures/bbva_card`` without a PDF engine;
production runs read PDFs through
:func:`expensuchis.importers.pdf.read_pdf_rows`. With ``--rows``, whether a
file is a card ``Liquidación`` is decided the same way ``identify`` would: by
flattening the fixture's own rows into text (page-separator-joined, space-
joined tokens per row) and applying the same marker predicate the PDF path
uses on its own flat text -- the cleanest honest equivalent to running two
separate readers over a fixture that carries no PDF bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter
from pathlib import Path

from expensuchis.importers.bbva_card import (
    CardBrand,
    CardLiquidacionParseError,
    MovementKind,
    ReconciliationError,
    is_bbva_card_liquidacion,
    parse_card_liquidacion,
)
from expensuchis.importers.bbva_card_importer import (
    IVA_ACCOUNT,
    MASTERCARD_USD_ACCOUNT,
    PERCEPCION_ACCOUNT,
    VISA_USD_ACCOUNT,
    build_entries,
)
from expensuchis.importers.bbva_importer import MASTERCARD_ACCOUNT, VISA_ACCOUNT
from expensuchis.importers.pdf import PAGE_SEPARATOR, PositionedRow, PositionedToken, read_pdf_rows

#: Path-separator-separated (``os.pathsep``) list of statement paths, used when argv is empty.
ENV_VAR = "EXPENSUCHIS_BBVA_CARD_STATEMENTS"

#: The person used for the stub run. The real person is never read here (only
#: the shape matters), so any non-empty value works; a placeholder that plainly
#: reads as one is safer than a name-shaped string an operator could later
#: mistake for output.
_PLACEHOLDER_PERSON = "PERSONA"

#: The fixed destination the stub counterparty map resolves every identity to.
#: Any category works -- this probe measures shape, never classification.
_STUB_DESTINATION = "expense:Expenses:Probe"


class _StubCounterpartyMap:
    """Resolve every identity to a fixed destination; never refuse.

    A real, unclassified merchant is exactly what this probe must never
    surface (it would need a real merchant name to explain), and this stub
    guarantees :class:`~expensuchis.importers.bbva_card_importer.CounterpartyClassificationError`
    is never raised by design, not merely by luck of the statement's content.
    """

    def __init__(self) -> None:
        self._identities: set[str] = set()

    def resolve(self, source: str, raw_name: str) -> str | None:
        self._identities.add(raw_name)
        return _STUB_DESTINATION

    @property
    def distinct_identities(self) -> int:
        return len(self._identities)


def _hash12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _load_rows(text: str) -> tuple[PositionedRow, ...]:
    """Parse the ``--rows`` fixture format into :class:`PositionedRow` values.

    Mirrors the private loader ``tests/test_bbva_card_parser.py`` and
    ``tests/test_bbva_card_importer.py`` each define for the same format.
    """
    rows: list[PositionedRow] = []
    page = 0
    row_number = 0
    tokens: list[PositionedToken] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("PAGE "):
            if tokens is not None:
                rows.append(PositionedRow(page=page, row=row_number, tokens=tuple(tokens)))
                tokens = None
            page = int(line.split()[1])
            row_number = 0
            continue
        if line == "ROW":
            if tokens is not None:
                rows.append(PositionedRow(page=page, row=row_number, tokens=tuple(tokens)))
            tokens = []
            row_number += 1
            continue
        assert tokens is not None, "a token line appears before any ROW"
        x0_raw, x1_raw, token_text = line.split(" ", 2)
        tokens.append(PositionedToken(x0=float(x0_raw), x1=float(x1_raw), text=token_text))
    if tokens is not None:
        rows.append(PositionedRow(page=page, row=row_number, tokens=tuple(tokens)))
    return tuple(rows)


def _flatten_rows(rows: tuple[PositionedRow, ...]) -> str:
    """Rebuild flat, page-separated text from positioned rows, for the marker predicate."""
    pages: dict[int, list[str]] = {}
    for row in rows:
        pages.setdefault(row.page, []).append(" ".join(token.text for token in row.tokens))
    return PAGE_SEPARATOR.join("\n".join(pages[page]) for page in sorted(pages))


def _page_count(rows: tuple[PositionedRow, ...]) -> int:
    return len({row.page for row in rows})


def _print_checks(checks: tuple) -> None:
    for check in checks:
        if check.ok:
            print(f"check {check.name}: ok")
        else:
            print(f"check {check.name}: FAILED ({check.detail})")


def _card_account_template(brand: CardBrand, is_usd: bool) -> str:
    if brand is CardBrand.VISA:
        return VISA_USD_ACCOUNT if is_usd else VISA_ACCOUNT
    return MASTERCARD_USD_ACCOUNT if is_usd else MASTERCARD_ACCOUNT


def _print_entry_aggregates(
    liquidacion, entries: list, counterparty_map: _StubCounterpartyMap
) -> bool:
    """Print the entry aggregates. Returns ``False`` on a natural-key duplicate."""
    movement_counts = Counter(movement.kind for movement in liquidacion.movements)
    print(f"movements purchases: {movement_counts.get(MovementKind.PURCHASE, 0)}")
    print(f"movements payments: {movement_counts.get(MovementKind.PAYMENT, 0)}")
    print(f"movements charges: {movement_counts.get(MovementKind.CHARGE, 0)}")
    print(f"entries built: {len(entries)}")

    brand = liquidacion.brand
    for is_usd in (False, True):
        account = _card_account_template(brand, is_usd).format(person=_PLACEHOLDER_PERSON)
        count = sum(
            1 for entry in entries for posting in entry.postings if posting.account == account
        )
        print(f"account {account}: {count}")

    installment_plans = sum(1 for entry in entries if "installments" in entry.meta)
    print(f"installment plans: {installment_plans}")

    # Charges carry no shape-level marker on the built entry itself; classify
    # by the destination account instead, which is fixed per charge class.
    iva_count = sum(1 for entry in entries if any(p.account == IVA_ACCOUNT for p in entry.postings))
    percepcion_count = sum(
        1 for entry in entries if any(p.account == PERCEPCION_ACCOUNT for p in entry.postings)
    )
    print(f"charges iva: {iva_count}")
    print(f"charges percepciones: {percepcion_count}")

    with_holder = sum(1 for entry in entries if "holder" in entry.meta)
    without_holder = len(entries) - with_holder
    print(f"entries with holder meta: {with_holder}")
    print(f"entries without holder meta: {without_holder}")

    print(f"distinct merchant identities resolved: {counterparty_map.distinct_identities}")

    keys = [entry.meta["key"] for entry in entries]
    duplicates = len(keys) - len(set(keys))
    if duplicates == 0:
        print("natural-keys: ok")
        return True
    print(f"natural-keys: FAILED (duplicates: {duplicates})")
    return False


def _process_statement(source_hash: str, pages: int, rows: tuple[PositionedRow, ...]) -> bool:
    """Probe one already-extracted statement's rows. Returns ``False`` on refusal.

    A file that is not a card ``Liquidación`` at all is reported and treated
    as **not a failure** (see the module docstring).
    """
    if not is_bbva_card_liquidacion(_flatten_rows(rows)):
        print(f"statement {source_hash} pages={pages} not a card liquidación")
        return True

    try:
        liquidacion = parse_card_liquidacion(rows)
    except ReconciliationError as exc:
        print(f"statement {source_hash} pages={pages} refused (class: ReconciliationError)")
        _print_checks(exc.checks)
        return False
    except CardLiquidacionParseError as exc:
        print(f"statement {source_hash} pages={pages} refused (class: CardLiquidacionParseError)")
        print(f"parse: FAILED ({exc})")
        return False

    print(f"statement {source_hash} pages={pages} brand={liquidacion.brand.value}")

    counterparty_map = _StubCounterpartyMap()
    entries = build_entries(liquidacion, _PLACEHOLDER_PERSON, counterparty_map)
    keys_ok = _print_entry_aggregates(liquidacion, entries, counterparty_map)
    _print_checks(liquidacion.checks)
    return keys_ok


def _paths_from_env() -> list[str]:
    raw = os.environ.get(ENV_VAR, "")
    return [path for path in raw.split(os.pathsep) if path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/probe_bbva_cards.py",
        description=(
            "Reconcile and build entries for BBVA Visa/Mastercard card statements, "
            "printing masked aggregates only. Never prints a date, description, "
            "merchant name, comprobante, amount, balance or filename."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="STATEMENT",
        help=f"PDF statements to probe (falls back to ${ENV_VAR})",
    )
    parser.add_argument(
        "--rows",
        action="store_true",
        help="read already-extracted positioned-rows text files instead of PDFs (test seam)",
    )
    args = parser.parse_args(argv)

    paths = list(args.paths) or _paths_from_env()
    if not paths:
        print(f"no statement paths given (argv or ${ENV_VAR})", file=sys.stderr)
        return 2

    failed = False
    for raw_path in paths:
        path = Path(raw_path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            print(
                f"statement <unreadable> refused (read error: {type(exc).__name__})",
                file=sys.stderr,
            )
            failed = True
            continue
        source_hash = _hash12(data)

        try:
            if args.rows:
                rows = _load_rows(data.decode("utf-8"))
            else:
                rows = read_pdf_rows(path)
        except Exception as exc:  # noqa: BLE001 - report the class, never the bytes
            print(
                f"statement {source_hash} refused (rows extraction failed: {type(exc).__name__})",
                file=sys.stderr,
            )
            failed = True
            continue

        pages = _page_count(rows)
        if not _process_statement(source_hash, pages, rows):
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
