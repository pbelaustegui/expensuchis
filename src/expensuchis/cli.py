"""The expensuchis command-line front end.

beangulp 0.2.0 ships no console scripts, so this module is the front end over its
``Importer`` API. Commands mirror the workflow contract:

    expensuchis bootstrap
    expensuchis identify [--accounts] <path>...
    expensuchis extract --source <name> <path>
    expensuchis report <batch-id>
    expensuchis approve <batch-id>
    expensuchis append <batch-id>
    expensuchis summary --month YYYY-MM --series {mep,ccl}
    expensuchis summary --from YYYY-MM --to YYYY-MM --series {mep,ccl}
    expensuchis installments --month YYYY-MM --series {mep,ccl}

Exit codes are stable: ``0`` success, ``1`` a refusal or validation failure (printed
with a greppable reason code and one human sentence), ``2`` a usage error, ``141`` the
consumer closed the output pipe (the SIGPIPE status; the command itself succeeded).
Output is plain text, never colour, so it can be piped and grepped.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from . import pipeline, summary
from .bootstrap import BootstrapError, bootstrap
from .fx import Series
from .ledger import LedgerDirError
from .paths import LedgerPaths
from .pipeline import PipelineError
from .summary import SummaryError

__all__ = ["build_parser", "main"]

_REFUSAL_PREFIX = "refused"


def build_parser() -> argparse.ArgumentParser:
    """Return the top-level argparse parser with one subparser per command."""
    parser = argparse.ArgumentParser(
        prog="expensuchis",
        description="Beancount-based family ledger ingest pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "bootstrap", help="create the ledger skeleton and layout (refuses to overwrite)"
    )

    identify_parser = subparsers.add_parser(
        "identify", help="report which importer claims each statement file"
    )
    identify_parser.add_argument("paths", nargs="+", metavar="PATH")
    identify_parser.add_argument(
        "--accounts",
        action="store_true",
        help=(
            "also list the accounts each statement would post to and the ones not yet "
            "opened in the ledger, with a suggested open line for each; writes nothing"
        ),
    )

    extract_parser = subparsers.add_parser(
        "extract", help="parse a statement into a staging batch for review"
    )
    extract_parser.add_argument("--source", required=True, metavar="NAME", help="source name")
    extract_parser.add_argument("path", metavar="PATH", help="statement file to extract")
    extract_parser.add_argument(
        "--reveal",
        action="store_true",
        help=(
            "show the real recipient CUIT in a redacted refusal message instead of "
            "the default placeholder; for the owner at a terminal only -- never pass "
            "this where the output is read by an agent or a remote model. Not "
            "enforced by the tool, same as leakguard's --reveal: a plain flag, "
            "trusted to the operator"
        ),
    )

    report_parser = subparsers.add_parser("report", help="print a staged batch's review report")
    report_parser.add_argument("batch_id", metavar="BATCH-ID")

    approve_parser = subparsers.add_parser(
        "approve", help="bind the reviewed batch bytes by sha256"
    )
    approve_parser.add_argument("batch_id", metavar="BATCH-ID")

    append_parser = subparsers.add_parser("append", help="append an approved batch to the ledger")
    append_parser.add_argument("batch_id", metavar="BATCH-ID")

    summary_parser = subparsers.add_parser(
        "summary", help="print the month's Expenses totals in USD at date"
    )
    summary_parser.add_argument("--month", metavar="YYYY-MM", help="a single month")
    summary_parser.add_argument(
        "--from", dest="from_month", metavar="YYYY-MM", help="range start, with --to"
    )
    summary_parser.add_argument(
        "--to", dest="to_month", metavar="YYYY-MM", help="range end, with --from"
    )
    summary_parser.add_argument("--series", required=True, choices=["mep", "ccl"])

    installments_parser = subparsers.add_parser(
        "installments", help="print the plans with a cuota due in a given month"
    )
    installments_parser.add_argument("--month", required=True, metavar="YYYY-MM")
    installments_parser.add_argument("--series", required=True, choices=["mep", "ccl"])

    return parser


def _validate_summary_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    has_month = args.month is not None
    has_range = args.from_month is not None or args.to_month is not None
    if has_month and has_range:
        parser.error("--month cannot be combined with --from/--to")
    if has_range and (args.from_month is None or args.to_month is None):
        parser.error("--from and --to must be given together")
    if not has_month and not has_range:
        parser.error("summary requires --month, or --from and --to")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "summary":
        _validate_summary_args(parser, args)

    try:
        paths = LedgerPaths()
    except LedgerDirError as exc:
        print(f"{_REFUSAL_PREFIX}: ledger-dir: {exc}", file=sys.stderr)
        return 1

    try:
        code = _dispatch(args, paths)
        sys.stdout.flush()  # surface a buffered BrokenPipeError inside the guard
        return code
    except (PipelineError, BootstrapError, SummaryError) as exc:
        print(f"{_REFUSAL_PREFIX}: {exc.reason}: {exc.message}", file=sys.stderr)
        return 1
    except LedgerDirError as exc:
        print(f"{_REFUSAL_PREFIX}: ledger-dir: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        _silence_stdout()
        return 141


def _silence_stdout() -> None:
    """Point stdout at ``os.devnull``; tolerate a stdout with no file descriptor."""
    try:
        descriptor = sys.stdout.fileno()
    except (OSError, ValueError):
        return
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), descriptor)
    except OSError:
        pass


def _print_account_diagnoses(paths: LedgerPaths, statements: Sequence[str]) -> None:
    """Print each statement's importer, posted accounts and unopened accounts.

    Account names and dates only: never a counterparty, narration or holder name.
    Every statement is diagnosed before anything is printed, so a refusal on a later
    statement never leaves partial output behind.
    """
    diagnoses = [(statement, pipeline.diagnose(paths, statement)) for statement in statements]
    for statement, diagnosis in diagnoses:
        print(f"{statement}\t{diagnosis.importer}")
        print("accounts:")
        for account in diagnosis.accounts:
            print(f"  {account}")
        if not diagnosis.missing:
            print("missing: none")
        else:
            print("missing (paste into accounts.beancount; the tool never opens accounts):")
            for account in diagnosis.missing:
                print(f"{diagnosis.first_dates[account].isoformat()} open {account}")
            print(
                "note: each date is the earliest entry in this statement; "
                "use an earlier date if older movements exist."
            )
        if diagnosis.opened_late:
            print(
                "opened too late (an open directive exists but is dated after this "
                "statement's earliest entry; move its date to on or before the date shown):"
            )
            for account in sorted(diagnosis.opened_late):
                first = diagnosis.late_first_dates[account].isoformat()
                current = diagnosis.opened_late[account].isoformat()
                print(f"{first} open {account}  # currently {current}")


def _dispatch(args: argparse.Namespace, paths: LedgerPaths) -> int:
    if args.command == "bootstrap":
        for path in bootstrap(paths):
            print(f"created: {path}")
        return 0

    if args.command == "identify" and args.accounts:
        _print_account_diagnoses(paths, args.paths)
        return 0

    if args.command == "identify":
        for identification in pipeline.identify(args.paths):
            print(f"{identification.filepath}\t{identification.importer.name}")
        return 0

    if args.command == "extract":
        result = pipeline.extract(
            paths, args.source, args.path, importers=pipeline.get_importers(reveal=args.reveal)
        )
        print(f"batch_id: {result.batch_id}")
        print(f"report_path: {result.report_path}")
        print(f"entries: {result.entries}  skipped: {result.skipped}")
        print(f"next: expensuchis report {result.batch_id}")
        return 0

    if args.command == "report":
        print(pipeline.report(paths, args.batch_id), end="")
        return 0

    if args.command == "approve":
        approval = pipeline.approve(paths, args.batch_id)
        print(f"approved: {args.batch_id}")
        print(f"sha256: {approval['sha256']}")
        return 0

    if args.command == "append":
        result = pipeline.append(paths, args.batch_id)
        print(f"appended: {result.batch_id}")
        for path in result.files:
            print(f"wrote: {path}")
        return 0

    if args.command == "summary":
        series = Series.MEP if args.series == "mep" else Series.CCL
        if args.month is not None:
            result = summary.summarize(paths, args.month, series)
            for account, usd in result.by_account.items():
                print(f"{account}\t{usd:.2f} USD")
            print(f"total\t{result.total:.2f} USD")
        else:
            results = summary.summarize_range(paths, args.from_month, args.to_month, series)
            for result in results:
                print(f"{result.month}\t{result.total:.2f} USD")
        return 0

    if args.command == "installments":
        series = Series.MEP if args.series == "mep" else Series.CCL
        result = summary.installments(paths, args.month, series)
        for row in result.rows:
            print(f"{row.payee}\t{row.number}/{row.total_installments}\t{row.amount_usd:.2f} USD")
        print(f"total\t{result.total:.2f} USD")
        return 0

    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover
