"""Local acceptance probe for the BBVA Argentina ``Extracto consolidado`` parser,
run by a human.

This is the only part of the workflow that touches a **real** statement, and it is
run locally so the household's data never enters an agent's context. The models that
drive this repository are remote APIs, so anything printed here may end up in a
transcript: that is why the probe prints **aggregates only** and why it exists as a
separate program instead of "just parse it in the REPL".

It never prints a movement's date, concept, merchant, CUIT, account number, amount,
balance or the statement's filename. It prints, per statement:

* the sha256 of the file's bytes truncated to 12 hex characters (an identity a human
  can compare, without the name) and the page count;
* whether the file is even an ``Extracto consolidado`` at all
  (:func:`~expensuchis.importers.bbva.is_bbva_extracto`) -- the real statement
  folder also holds Visa/Mastercard card statements, which this probe does not
  parse; a non-match is reported as ``not an extracto consolidado`` and the run
  moves on to the next file rather than raising;
* the number of sub-account blocks, each one's ``kind``/``currency`` pair (a
  closed, non-sensitive vocabulary) and whether it is quiescent (``SIN
  MOVIMIENTOS``) -- never its real account number, which stays a
  :class:`~expensuchis.importers.bbva.Movement`/block-internal field;
* whether every block's ``kind``/``currency`` pair resolves to a ledger account
  through the importer's own routing table
  (:func:`expensuchis.importers.bbva_importer._resolve_account_routes`), reused
  here so this probe can never disagree with the importer about what routes --
  called with a **placeholder** person, since the real path is never read;
* the count per :class:`~expensuchis.importers.bbva.MovementKind`, across every
  sub-account block;
* the debit-card detail join's shape (matched / zero-candidate / multiple-candidate
  purchase rows, and how many detail rows fell outside the table's date window --
  T-07 owner decision 3: those are ignored by design, not an error) and the
  sent-transfer join's shape (matched / zero-candidate / multiple-candidate
  ``TRANSFERENCIA``-out rows, and how many sent-transfer rows were never claimed).
  Neither join result is exposed by :func:`~expensuchis.importers.bbva.parse_extracto_consolidado`
  on a reconciliation refusal (it raises before returning anything), so both are
  **re-derived** here from the parser's own private block/detail/transfer parsing
  functions (:func:`bbva._parse_blocks`, :func:`bbva._parse_debit_card_details`,
  :func:`bbva._parse_transfer_rows`, :func:`bbva._resolve_row_date`) -- a read-only
  shadow of the same joins, reusing the parser's own machinery so it can never
  disagree with it, the same discipline ``probe_brubank.py`` uses for its own
  shape scan;
* the resolved close date's **month and day only** (never the year) and the gap in
  days between the anchor (the source PDF's ``CreationDate``) and that close date
  -- the two numbers the 60-day-ceiling rule (T-07 owner decision 6) is checked
  against, without printing either date whole;
* one line per reconciliation check with ``ok`` or ``FAILED``, and on failure the
  check's **masked** detail -- printed whenever the document reaches the check
  stage at all (a structural refusal before that point has no checks to print).

On a structural refusal (:class:`~expensuchis.importers.bbva.ExtractoConsolidadoParseError`)
it prints the error class, the parser's own (already-masked, per its module
docstring) message, and a short masked ``cause:`` bucket built only from that
message (see :func:`_refusal_cause`) -- never a bare ``{exc}`` from a generic
exception path, because an engine error (unlike this module's own exceptions) can
carry a real file path.

It exits non-zero when any statement fails to parse or reconcile. A file that is
not an ``Extracto consolidado`` at all is reported, not a failure: it is expected
in this folder (the card statements), the same way an importer's own ``identify``
returning ``False`` is not an error.

Usage::

    python tools/probe_bbva.py statement.pdf [statement.pdf ...]
    EXPENSUCHIS_BBVA_STATEMENTS=a.pdf:b.pdf python tools/probe_bbva.py

``--text`` reads already-extracted text files instead of PDFs. It exists so the
probe itself can be exercised against the synthetic fixtures in
``tests/fixtures/bbva`` without a PDF engine; production runs read PDFs through
:func:`expensuchis.importers.pdf.read_pdf` and take the anchor date from
:func:`expensuchis.importers.pdf.read_creation_date`, which imports ``pypdfium2``
lazily. Because a text fixture carries no PDF ``CreationDate``, ``--text`` always
pairs with ``--anchor YYYY-MM-DD``, a single anchor applied to every path in the
run (production never uses either flag: each real file gets its own anchor read
from its own metadata).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from expensuchis.importers import bbva, bbva_importer
from expensuchis.importers.pdf import read_creation_date, read_pdf

#: Path-separator-separated (``os.pathsep``) list of statement paths, used when argv is empty.
ENV_VAR = "EXPENSUCHIS_BBVA_STATEMENTS"

#: The person used for the routing check. The real path is never read here, so
#: any non-empty value works; a placeholder that plainly reads as one is safer
#: than a name-shaped string an operator could later mistake for output.
_PLACEHOLDER_PERSON = "PERSONA"


def _hash12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _extract_text(data: bytes) -> tuple[str, int]:
    text = data.decode("utf-8")
    return text, text.count("\f") + 1


@dataclass(frozen=True)
class _DebitJoinShape:
    """The debit-card detail join's shape, re-derived (see the module docstring)."""

    matched: int
    zero_candidates: int
    multiple_candidates: int
    outside_window: int


@dataclass(frozen=True)
class _TransferJoinShape:
    """The sent-transfer join's shape, re-derived (see the module docstring)."""

    matched: int
    zero_candidates: int
    multiple_candidates: int
    unmatched: int


#: Ordered ``(needle, bucket)`` pairs bucketing
#: :class:`~expensuchis.importers.bbva.ExtractoConsolidadoParseError` messages
#: into a short, generic cause. The messages are already masked by the parser's
#: own contract (never a date, merchant, CUIT or account number); this only
#: replaces the (still-safe) full message with an even shorter, fixed label for
#: quick scanning, the same discipline ``probe_brubank.py``'s ``_refusal_cause``
#: uses.
_CAUSE_BUCKETS: tuple[tuple[str, str], ...] = (
    ("no sub-account movement block was found", "no sub-account block found"),
    ("unknown currency marker", "unknown currency marker"),
    ("has no table header line right after it", "missing table header after block"),
    ("does not open with SALDO ANTERIOR", "table region missing opening SALDO ANTERIOR"),
    (
        "an unrecognized line appears inside the movement table",
        "unrecognized line inside table region",
    ),
    ("month name outside the known table", "unknown month name in SALDO AL"),
    ("never reaches a SALDO AL closing line", "block never reaches its SALDO AL line"),
    ("has no TOTAL MOVIMIENTOS line", "missing TOTAL MOVIMIENTOS line"),
    ("only a quiescent block is accepted", "non-ARS block carries movement"),
    ("never followed by its", "debit-card detail header missing its CUENTA DEBITO line"),
    (
        "merchant split on the card's last 4 digits is ambiguous",
        "debit-card detail merchant split ambiguous",
    ),
    ("has no words after the card's last 4 digits", "debit-card detail row has no merchant text"),
    (
        "debit-card detail row's date is not a real date",
        "debit-card detail row has an invalid date",
    ),
    ("is not a well-formed Argentine amount", "malformed amount"),
    ("outside the known vocabulary", "unknown concept vocabulary"),
    ("never carries an origin code", "unexpected origin code for its concept class"),
    ("has the wrong sign for its class", "concept sign mismatch"),
    ("sent-transfer row's date is not a real date", "sent-transfer row has an invalid date"),
    ("year override is not a real date", "invalid year-override close date"),
    ("SALDO AL close date is not a real date", "invalid SALDO AL close date"),
    ("more than 60 days after the resolved close date", "anchor more than 60 days after close"),
)


def _refusal_cause(exc: bbva.ExtractoConsolidadoParseError) -> str:
    message = str(exc)
    for needle, bucket in _CAUSE_BUCKETS:
        if needle in message:
            return bucket
    return "other structural mismatch"


def _rows_with_dates(
    blocks: tuple, close_year: int, close_month: int
) -> list[tuple[object, object, dt.date]]:
    """Return ``(block, row, resolved_date)`` for every row in every block.

    Uses :func:`bbva._resolve_row_date`, the exact function
    :func:`~expensuchis.importers.bbva.parse_extracto_consolidado` itself calls,
    so the dates here can never disagree with the parser's own.
    """
    resolved: list[tuple[object, object, dt.date]] = []
    for block in blocks:
        for row in block.rows:
            date = bbva._resolve_row_date(row.dd, row.mm, close_year, close_month)
            resolved.append((block, row, date))
    return resolved


def _debit_join_shape(
    details: tuple,
    resolved_rows: list[tuple[object, object, dt.date]],
    primary_close_date: dt.date,
) -> _DebitJoinShape:
    ars_dates = [date for block, _row, date in resolved_rows if block.currency == "$"]
    window_start = min(ars_dates) if ars_dates else primary_close_date

    candidates_by_key: dict[tuple[dt.date, object], list] = {}
    for detail in details:
        candidates_by_key.setdefault((detail.date, detail.amount), []).append(detail)

    matched = zero = multiple = 0
    for _block, row, date in resolved_rows:
        if row.kind is not bbva.MovementKind.DEBIT_CARD_PURCHASE:
            continue
        candidates = candidates_by_key.get((date, -row.amount), [])
        if len(candidates) == 1:
            matched += 1
        elif len(candidates) == 0:
            zero += 1
        else:
            multiple += 1

    outside_window = sum(
        1 for detail in details if not (window_start <= detail.date <= primary_close_date)
    )
    return _DebitJoinShape(matched, zero, multiple, outside_window)


def _transfer_join_shape(
    transfers: tuple, resolved_rows: list[tuple[object, object, dt.date]]
) -> _TransferJoinShape:
    candidates_by_key: dict[tuple[dt.date, object], list] = {}
    for transfer in transfers:
        candidates_by_key.setdefault((transfer.date, transfer.amount), []).append(transfer)

    matched = zero = multiple = 0
    used_keys: set[tuple[dt.date, object, int]] = set()
    for _block, row, date in resolved_rows:
        if row.kind is not bbva.MovementKind.TRANSFER_OUT:
            continue
        candidates = candidates_by_key.get((date, -row.amount), [])
        if len(candidates) == 1:
            matched += 1
            transfer = candidates[0]
            used_keys.add((transfer.date, transfer.amount, transfer.line))
        elif len(candidates) == 0:
            zero += 1
        else:
            multiple += 1

    unmatched = sum(
        1
        for transfer in transfers
        if (transfer.date, transfer.amount, transfer.line) not in used_keys
    )
    return _TransferJoinShape(matched, zero, multiple, unmatched)


def _print_checks(checks: tuple) -> None:
    for check in checks:
        if check.ok:
            print(f"check {check.name}: ok")
        else:
            print(f"check {check.name}: FAILED ({check.detail})")


def _print_blocks(blocks: tuple) -> None:
    for index, block in enumerate(blocks, start=1):
        print(
            f"  block {index}: kind={block.kind} currency={block.currency} "
            f"quiescent={block.quiescent}"
        )


def _print_routing(blocks: tuple, person: str) -> None:
    account_blocks = tuple(
        bbva.AccountBlock(kind=block.kind, currency=block.currency, account=block.account)
        for block in blocks
    )
    try:
        bbva_importer._resolve_account_routes(account_blocks, person)
    except bbva_importer.AccountMappingError as exc:
        print(f"  routing: FAILED ({exc})")
    else:
        print("  routing: ok")


def _print_movement_counts(blocks: tuple) -> None:
    counts = Counter(row.kind for block in blocks for row in block.rows)
    for kind in bbva.MovementKind:
        print(f"  movement {kind.value}: {counts.get(kind, 0)}")


def _print_debit_join(shape: _DebitJoinShape) -> None:
    print(f"  debit-join matched: {shape.matched}")
    print(f"  debit-join zero-candidates: {shape.zero_candidates}")
    print(f"  debit-join multiple-candidates: {shape.multiple_candidates}")
    print(f"  debit-join detail-rows-outside-window: {shape.outside_window}")


def _print_transfer_join(shape: _TransferJoinShape) -> None:
    print(f"  transfer-join matched: {shape.matched}")
    print(f"  transfer-join zero-candidates: {shape.zero_candidates}")
    print(f"  transfer-join multiple-candidates: {shape.multiple_candidates}")
    print(f"  transfer-join unmatched-transfer-rows: {shape.unmatched}")


def _print_close_date(primary_close_date: dt.date, anchor: dt.date) -> None:
    print(f"  close-date month={primary_close_date.month} day={primary_close_date.day}")
    print(f"  anchor-gap-days: {(anchor - primary_close_date).days}")


def _process_statement(source_hash: str, pages: int, text: str, anchor: dt.date) -> bool:
    """Probe one already-extracted statement's text. Returns ``False`` on refusal.

    A file that is not an ``Extracto consolidado`` at all is reported and
    treated as **not a failure** (see the module docstring).
    """
    if not bbva.is_bbva_extracto(text):
        print(f"statement {source_hash} pages={pages} not an extracto consolidado")
        return True

    rows = bbva._layout(text)
    try:
        blocks = bbva._parse_blocks(rows)
        details = bbva._parse_debit_card_details(rows)
    except bbva.ExtractoConsolidadoParseError as exc:
        print(
            f"statement {source_hash} pages={pages} refused (class: ExtractoConsolidadoParseError)"
        )
        print(f"parse: FAILED ({exc})")
        print(f"cause: {_refusal_cause(exc)}")
        return False

    try:
        primary_close_date = bbva._resolve_close_date(
            blocks[0].close_day, blocks[0].close_month, anchor, None
        )
    except bbva.ExtractoConsolidadoParseError as exc:
        print(
            f"statement {source_hash} pages={pages} refused (class: ExtractoConsolidadoParseError)"
        )
        print(f"parse: FAILED ({exc})")
        print(f"cause: {_refusal_cause(exc)}")
        return False

    print(f"statement {source_hash} pages={pages} blocks={len(blocks)}")
    _print_blocks(blocks)
    _print_routing(blocks, _PLACEHOLDER_PERSON)
    _print_movement_counts(blocks)

    close_year = primary_close_date.year
    resolved_rows = _rows_with_dates(blocks, close_year, primary_close_date.month)
    transfers = bbva._parse_transfer_rows(rows, close_year, primary_close_date.month)
    _print_debit_join(_debit_join_shape(details, resolved_rows, primary_close_date))
    _print_transfer_join(_transfer_join_shape(transfers, resolved_rows))
    _print_close_date(primary_close_date, anchor)

    try:
        extracto = bbva.parse_extracto_consolidado(text, anchor=anchor)
    except bbva.ReconciliationError as exc:
        print(f"statement {source_hash} refused (class: ReconciliationError)")
        _print_checks(exc.checks)
        return False
    except bbva.ExtractoConsolidadoParseError as exc:
        # Same inputs as the calls above, so reaching this is not expected in
        # practice; handled anyway rather than assuming it cannot happen.
        print(f"statement {source_hash} refused (class: ExtractoConsolidadoParseError)")
        print(f"parse: FAILED ({exc})")
        print(f"cause: {_refusal_cause(exc)}")
        return False

    _print_checks(extracto.checks)
    return True


def _paths_from_env() -> list[str]:
    raw = os.environ.get(ENV_VAR, "")
    return [path for path in raw.split(os.pathsep) if path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/probe_bbva.py",
        description=(
            "Reconcile BBVA Extracto consolidado statements and print masked aggregates "
            "only. Never prints a date, concept, merchant, CUIT, account number, amount, "
            "balance or filename."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="STATEMENT",
        help=f"PDF statements to probe (falls back to ${ENV_VAR})",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="read already-extracted text files instead of PDFs (test seam)",
    )
    parser.add_argument(
        "--anchor",
        metavar="YYYY-MM-DD",
        help="anchor date, required together with --text (test seam; production reads "
        "each file's own PDF CreationDate)",
    )
    args = parser.parse_args(argv)

    if args.text and args.anchor is None:
        print("--anchor is required together with --text", file=sys.stderr)
        return 2

    text_anchor: dt.date | None = None
    if args.anchor is not None:
        try:
            text_anchor = dt.date.fromisoformat(args.anchor)
        except ValueError:
            print("--anchor must be an ISO date (YYYY-MM-DD)", file=sys.stderr)
            return 2

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
            if args.text:
                text, pages = _extract_text(data)
                anchor = text_anchor
            else:
                text, pages = read_pdf(path)
                anchor = read_creation_date(path)
        except Exception as exc:  # noqa: BLE001 - report the class, never the bytes
            print(
                f"statement {source_hash} refused (text extraction failed: {type(exc).__name__})",
                file=sys.stderr,
            )
            failed = True
            continue

        if anchor is None:
            print(
                f"statement {source_hash} pages={pages} refused (creation date missing or unparseable)"
            )
            failed = True
            continue

        if not _process_statement(source_hash, pages, text, anchor):
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
