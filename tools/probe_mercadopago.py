"""Local acceptance probe for the Mercado Pago parser, run by a human.

This is the only part of the workflow that touches a **real** statement, and it is
run locally so the household's data never enters an agent's context. The models that
drive this repository are remote APIs, so anything printed here may end up in a
transcript: that is why the probe prints **aggregates only** and why it exists as a
separate program instead of "just parse it in the REPL".

It never prints a movement's date, description, operation id, amount or balance, and
it never prints the statement's filename. It prints:

* the sha256 of the file's bytes truncated to 12 hex characters (an identity a human
  can compare, without the name), the page count and the movement count;
* the count per :class:`~expensuchis.importers.mercadopago.MovementKind`;
* one line per reconciliation check with ``ok`` or ``FAILED``, and on failure the
  check's **masked** detail (counts, indexes and line numbers only).

It exits non-zero when any check fails, or when the document cannot be parsed at all.

Usage::

    python tools/probe_mercadopago.py statement.pdf [statement.pdf ...]
    EXPENSUCHIS_MP_STATEMENTS=a.pdf:b.pdf python tools/probe_mercadopago.py

``--text`` reads already-extracted text files instead of PDFs. It exists so the probe
itself can be exercised against the synthetic fixtures in ``tests/fixtures`` without a
PDF engine; production runs read PDFs through
:func:`expensuchis.importers.pdf.read_pdf`, which imports ``pypdfium2`` lazily inside the
function.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter
from pathlib import Path

from expensuchis.importers.mercadopago import (
    MovementKind,
    ReconciliationError,
    Resumen,
    ResumenParseError,
    parse_resumen,
)
from expensuchis.importers.pdf import read_pdf

#: Path-separator-separated (``os.pathsep``) list of statement paths, used when argv is empty.
ENV_VAR = "EXPENSUCHIS_MP_STATEMENTS"


def _hash12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _extract_text(data: bytes) -> tuple[str, int]:
    text = data.decode("utf-8")
    return text, text.count("\f") + 1


def _print_checks(checks: tuple) -> None:
    for check in checks:
        if check.ok:
            print(f"check {check.name}: ok")
        else:
            print(f"check {check.name}: FAILED ({check.detail})")


def _print_success(source_hash: str, pages: int, resumen: Resumen) -> None:
    print(f"statement {source_hash} pages={pages} movements={len(resumen.movements)}")
    counts = Counter(movement.kind for movement in resumen.movements)
    for kind in MovementKind:
        print(f"  kind {kind.value}: {counts.get(kind, 0)}")
    _print_checks(resumen.checks)


def _paths_from_env() -> list[str]:
    raw = os.environ.get(ENV_VAR, "")
    return [path for path in raw.split(os.pathsep) if path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/probe_mercadopago.py",
        description=(
            "Reconcile Mercado Pago account statements and print masked aggregates only. "
            "Never prints a date, description, id, amount, balance or filename."
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
        try:
            resumen = parse_resumen(text)
        except ReconciliationError as exc:
            print(f"statement {source_hash} pages={pages} refused")
            _print_checks(exc.checks)
            failed = True
            continue
        except ResumenParseError as exc:
            print(f"statement {source_hash} pages={pages} refused")
            print(f"parse: FAILED ({exc})")
            failed = True
            continue
        _print_success(source_hash, pages, resumen)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
