"""Local acceptance probe for the Brubank ``Resumen de Movimientos`` parser, run by
a human.

This is the only part of the workflow that touches a **real** statement, and it is
run locally so the household's data never enters an agent's context. The models that
drive this repository are remote APIs, so anything printed here may end up in a
transcript: that is why the probe prints **aggregates only** and why it exists as a
separate program instead of "just parse it in the REPL".

It never prints a movement's date, ``#Ref``, description, amount, balance or the
statement's filename. It prints:

* the sha256 of the file's bytes truncated to 12 hex characters (an identity a human
  can compare, without the name), the page count and the movement count;
* the count per :class:`~expensuchis.importers.brubank.MovementKind`, and the count of
  ``Intereses pagados`` rows by direction (credit/debit) — T-08b's fixed-destination
  posting assumed this is always a credit; this is the number that would prove it wrong;
* one line per reconciliation check with ``ok`` or ``FAILED``, and on failure the
  check's **masked** detail (counts, indexes and line numbers only);
* one ``refused (class: ...)`` line and, for a structural refusal, a short masked
  ``cause:`` bucket built only from the parser's own already-masked message
  (see :func:`_refusal_cause`) — never a bare ``{exc}`` from a generic exception path,
  because an engine error (unlike this module's own exceptions) can carry a real file
  path (the lesson recorded for T-05a/T-05b in ``odd/tasks/family-ledger.md``);
* the two **shape facts** T-08c exists to confirm against the real file, which
  :func:`~expensuchis.importers.brubank.parse_resumen` itself never exposes: whether
  any date-prefixed line ever falls outside a movement table region, and how many
  regions closed on the footer/period line versus on a header/recap field
  (:func:`_scan_table_regions`). Both are derived from the extracted text using the
  parser's own module-level regexes and helpers, without changing the parser: reusing
  ``brubank._layout``, ``brubank._is_table_header``, ``brubank._DATE_PREFIX_RE``,
  ``brubank._FOOTER_LINE_RE`` and ``brubank._header_field`` means this scan can never
  disagree with the parser about what a table header, a footer line or a header field
  looks like — it is a read-only shadow of the same state machine, not a second
  opinion.

It exits non-zero when any check fails, or when the document cannot be parsed at all.

Usage::

    python tools/probe_brubank.py statement.pdf [statement.pdf ...]
    EXPENSUCHIS_BRUBANK_STATEMENTS=a.pdf:b.pdf python tools/probe_brubank.py

``--text`` reads already-extracted text files instead of PDFs. It exists so the probe
itself can be exercised against the synthetic fixtures in ``tests/fixtures/brubank``
without a PDF engine; production runs read PDFs through
:func:`expensuchis.importers.pdf.read_pdf`, which imports ``pypdfium2`` lazily inside
the function.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from expensuchis.importers import brubank
from expensuchis.importers.pdf import read_pdf

#: Path-separator-separated (``os.pathsep``) list of statement paths, used when argv is empty.
ENV_VAR = "EXPENSUCHIS_BRUBANK_STATEMENTS"


def _hash12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _extract_text(data: bytes) -> tuple[str, int]:
    text = data.decode("utf-8")
    return text, text.count("\f") + 1


@dataclass(frozen=True)
class _RegionShape:
    """The two table-region shape facts :func:`~expensuchis.importers.brubank.parse_resumen`
    does not expose on its own (see the module docstring)."""

    stray_date_lines_outside_region: int
    regions_closed_by_footer: int
    regions_closed_by_header: int
    regions_closed_by_end_of_page: int


def _scan_table_regions(text: str) -> _RegionShape:
    """Re-derive facts (1) and (2) from T-08c without touching the parser.

    Mirrors :func:`expensuchis.importers.brubank._parse_movements`'s region state
    machine exactly (open on a table header, close on a footer/period line or a known
    header/recap field) but never raises: this is a read-only shadow built from the
    parser's own module-level regexes and helpers, so it can never disagree with the
    parser about what those shapes look like. It does not change the parser's
    behavior and it does not decide anything the parser would not also decide — it
    only counts what the parser's own rules already imply, for reporting.

    A line inside an open region that is none of the recognized shapes (the one case
    that makes the real parser raise ``ResumenParseError`` with "an unrecognized line
    appears inside the movement table") is deliberately not classified further here:
    that refusal is :func:`~expensuchis.importers.brubank.parse_resumen`'s job, not
    this scan's.
    """
    stray_date_lines = 0
    closed_by_footer = 0
    closed_by_header = 0
    closed_by_end_of_page = 0

    in_region = False
    current_page: int | None = None
    for page, _line, raw in brubank._layout(text):
        if page != current_page:
            if in_region:
                closed_by_end_of_page += 1
            current_page = page
            in_region = False

        stripped = raw.strip()

        if not in_region:
            if brubank._is_table_header(stripped):
                in_region = True
            elif brubank._DATE_PREFIX_RE.match(stripped) is not None:
                stray_date_lines += 1
            continue

        if stripped == "" or brubank._is_table_header(stripped):
            continue
        if brubank._DATE_PREFIX_RE.match(stripped) is not None:
            continue
        if brubank._FOOTER_LINE_RE.fullmatch(stripped) is not None:
            closed_by_footer += 1
            in_region = False
            continue
        if brubank._header_field(stripped) is not None:
            closed_by_header += 1
            in_region = False
            continue
        # An unrecognized line inside an open region: the real parser refuses on
        # this. Nothing to count here beyond what is already tracked above.

    if in_region:
        closed_by_end_of_page += 1

    return _RegionShape(
        stray_date_lines_outside_region=stray_date_lines,
        regions_closed_by_footer=closed_by_footer,
        regions_closed_by_header=closed_by_header,
        regions_closed_by_end_of_page=closed_by_end_of_page,
    )


def _refusal_cause(exc: brubank.ResumenParseError) -> str:
    """A short, already-masked bucket for a structural refusal's message.

    ``str(exc)`` is guaranteed masked by the parser's own contract (its module
    docstring, "Masking": every message reports page/line numbers, counts and field
    names only, never statement text), so reading it here is exactly as safe as the
    parser's own callers printing it directly. This only buckets that message into
    one of the shapes T-08c exists to confirm; it never inspects anything the parser
    itself did not already decide to print.
    """
    message = str(exc)
    if "outside any movement table" in message:
        return "date-prefixed line outside a table region"
    if "Imp. Trans. Financieras" in message:
        return "header-only total appeared as a movement row"
    if "unrecognized line appears inside the movement table" in message:
        return "unrecognized line inside a table region"
    return "other structural mismatch"


def _print_checks(checks: tuple) -> None:
    for check in checks:
        if check.ok:
            print(f"check {check.name}: ok")
        else:
            print(f"check {check.name}: FAILED ({check.detail})")


def _print_shape(shape: _RegionShape) -> None:
    print(f"  shape stray-date-lines-outside-region: {shape.stray_date_lines_outside_region}")
    print(f"  shape regions-closed-by-footer: {shape.regions_closed_by_footer}")
    print(f"  shape regions-closed-by-header: {shape.regions_closed_by_header}")
    print(f"  shape regions-closed-by-end-of-page: {shape.regions_closed_by_end_of_page}")


def _print_success(
    source_hash: str, pages: int, resumen: brubank.Resumen, shape: _RegionShape
) -> None:
    print(f"statement {source_hash} pages={pages} movements={len(resumen.movements)}")
    kinds = Counter(movement.kind for movement in resumen.movements)
    for kind in brubank.MovementKind:
        print(f"  movement {kind.value}: {kinds.get(kind, 0)}")
    interest_movements = [
        movement for movement in resumen.movements if movement.kind is brubank.MovementKind.INTEREST
    ]
    credit_interest = sum(1 for movement in interest_movements if movement.amount > 0)
    debit_interest = len(interest_movements) - credit_interest
    print(f"  intereses-pagados credit: {credit_interest}")
    print(f"  intereses-pagados debit: {debit_interest}")
    _print_checks(resumen.checks)
    _print_shape(shape)


def _paths_from_env() -> list[str]:
    raw = os.environ.get(ENV_VAR, "")
    return [path for path in raw.split(os.pathsep) if path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/probe_brubank.py",
        description=(
            "Reconcile Brubank account statements and print masked aggregates only. "
            "Never prints a date, #Ref, description, amount, balance or filename."
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
            text, pages = _extract_text(data) if args.text else read_pdf(path)
        except Exception as exc:  # noqa: BLE001 - report the class, never the bytes
            print(
                f"statement {source_hash} refused (text extraction failed: {type(exc).__name__})",
                file=sys.stderr,
            )
            failed = True
            continue

        shape = _scan_table_regions(text)
        try:
            resumen = brubank.parse_resumen(text)
        except brubank.ReconciliationError as exc:
            print(f"statement {source_hash} pages={pages} refused (class: ReconciliationError)")
            _print_checks(exc.checks)
            _print_shape(shape)
            failed = True
            continue
        except brubank.ResumenParseError as exc:
            print(f"statement {source_hash} pages={pages} refused (class: ResumenParseError)")
            print(f"parse: FAILED ({exc})")
            print(f"cause: {_refusal_cause(exc)}")
            _print_shape(shape)
            failed = True
            continue
        _print_success(source_hash, pages, resumen, shape)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
