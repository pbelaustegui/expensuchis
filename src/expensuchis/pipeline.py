"""The import workflow contract: identify -> extract -> review -> approve -> append -> bean-check.

The gate between a parsed statement and the ledger is a **digest-bound approval**,
not an interactive prompt. ``extract`` stages a batch; the human reads the report;
``approve`` records the sha256 of the exact proposed bytes; ``append`` recomputes
that digest and refuses on any mismatch. Nothing is re-derived between review and
write: the staged bytes are the appended bytes.

The honest boundary: the CLI cannot prove a human read the report. What it enforces
is the separation of the two commands and the binding of the approval to exact
bytes. ``approve`` proves the digest, not the reading.

Every refusal carries a stable, greppable ``reason`` code on :class:`PipelineError`,
so callers (and the CLI) can branch on it and a reader can search for it.

The deduplication index is **derived, never stored**: it is built by parsing the
``transactions/*.beancount`` files with beancount's own loader and collecting each
entry's ``key`` metadata. There is no regex over the ledger text, because a key
that only appears in a comment is not a key.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from beancount import loader
from beancount.ops import validation
from beancount.parser import printer
from beangulp import Importer
from beangulp import identify as beangulp_identify
from beangulp.exceptions import Error as BeangulpIdentifyError

from .importers import get_importers
from .importers.mercadopago_importer import (
    CounterpartyClassificationError as _MercadoPagoCounterpartyClassificationError,
)
from .importers.mercadopago_importer import (
    StatementReadError as _MercadoPagoStatementReadError,
)
from .importers.provincia_importer import (
    CounterpartyClassificationError as _ProvinciaCounterpartyClassificationError,
)
from .importers.provincia_importer import (
    StatementReadError as _ProvinciaStatementReadError,
)
from .importers.provincia_visa_importer import (
    CounterpartyClassificationError as _ProvinciaVisaCounterpartyClassificationError,
)
from .importers.provincia_visa_importer import (
    StatementReadError as _ProvinciaVisaStatementReadError,
)
from .paths import LedgerPaths
from .redact import RedactionKeyError, content_hash, load_or_create_key

__all__ = [
    "AMBIGUOUS_IMPORTER",
    "APPENDED",
    "APPROVAL_MISSING",
    "BATCH_ALREADY_APPENDED",
    "BATCH_EMPTY",
    "BATCH_EXISTS",
    "BATCH_NOT_FOUND",
    "DIGEST_MISMATCH",
    "IMPORTER_RAISED",
    "INVALID_SOURCE",
    "KEY_ALREADY_PRESENT",
    "KEY_DUPLICATE",
    "KEY_MISSING",
    "LEDGER_DIRTY",
    "MAIN_MISSING",
    "NO_IMPORTER",
    "POST_WRITE_DIRTY",
    "STAGING_CORRUPT",
    "STATEMENT_MISSING",
    "AppendResult",
    "ExtractResult",
    "Identification",
    "PipelineError",
    "append",
    "approve",
    "build_key_index",
    "extract",
    "identify",
    "ledger_errors",
    "report",
]

#: Stable, greppable refusal reason codes. Never renumber or rename silently.
BATCH_NOT_FOUND = "batch-not-found"
BATCH_ALREADY_APPENDED = "batch-already-appended"
BATCH_EMPTY = "batch-empty"
BATCH_EXISTS = "batch-exists"
APPROVAL_MISSING = "approval-missing"
DIGEST_MISMATCH = "digest-mismatch"
MAIN_MISSING = "main-missing"
LEDGER_DIRTY = "ledger-dirty"
KEY_ALREADY_PRESENT = "key-already-present"
KEY_MISSING = "key-missing"
KEY_DUPLICATE = "key-duplicate"
IMPORTER_RAISED = "importer-raised"
NO_IMPORTER = "no-importer"
AMBIGUOUS_IMPORTER = "ambiguous-importer"
POST_WRITE_DIRTY = "post-write-dirty"
STAGING_CORRUPT = "staging-corrupt"
INVALID_SOURCE = "invalid-source"
STATEMENT_MISSING = "statement-missing"

APPENDED = "appended"

_PROPOSED = "proposed.beancount"
_BATCH = "batch.json"
_REPORT = "report.txt"
_APPROVAL = "approval.json"
_APPEND = "append.json"

#: Every source's own exception types whose message is already built deliberately
#: to be safe -- one pair of classes per importer module, by design:
#:
#: * ``CounterpartyClassificationError`` (T-04c: 'never defaulted'). Its message
#:   lists each unclassified counterparty with the context a human needs to
#:   classify it, and ``docs/import-workflow.md`` ('Never defaulted') documents it
#:   appearing verbatim after ``importer-raised``.
#: * ``StatementReadError`` (T-05b). A reader failure is wrapped in this exception
#:   with the failing exception's **class name only**, never the path or the raw
#:   reader message, specifically so that ``pipeline.extract`` can re-wrap it and
#:   the CLI can print it safely.
#:
#: These are the exception types whose ``str()`` the ``importer-raised`` refusal
#: preserves; every other importer exception is reduced to its class name
#: (R1-002/R3-002 below).
_PRESERVED_MESSAGE_TYPES: tuple[type[Exception], ...] = (
    _MercadoPagoCounterpartyClassificationError,
    _MercadoPagoStatementReadError,
    _ProvinciaCounterpartyClassificationError,
    _ProvinciaStatementReadError,
    _ProvinciaVisaCounterpartyClassificationError,
    _ProvinciaVisaStatementReadError,
)

#: Fallback statement identifier for `importer-raised` when the statement's bytes
#: or the redaction key cannot be read at all (R4-001/R3-001/R2-004 below). Naming
#: is more useful than this most of the time, but a typed refusal must never turn
#: into an unhandled OSError or RedactionKeyError while building one.
_STATEMENT_ID_UNREADABLE = "unreadable"


class PipelineError(RuntimeError):
    """A refused pipeline step, carrying a stable ``reason`` code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclasses.dataclass(frozen=True)
class Identification:
    """One statement file and the single importer that claims it."""

    filepath: Path
    importer: Importer


@dataclasses.dataclass(frozen=True)
class ExtractResult:
    """The outcome of staging one statement."""

    batch_id: str
    batch_dir: Path
    report_path: Path
    entries: int
    skipped: int


@dataclasses.dataclass(frozen=True)
class AppendResult:
    """The outcome of appending one approved batch."""

    batch_id: str
    files: tuple[Path, ...]


# --------------------------------------------------------------------------------- Seams


def identify(
    filepaths: Sequence[str | Path], importers: Sequence[Importer] | None = None
) -> list[Identification]:
    """Return the single importer that claims each file, or refuse.

    Raises:
        PipelineError: with :data:`NO_IMPORTER` when no importer claims a file, or
            :data:`AMBIGUOUS_IMPORTER` when more than one does.
    """
    resolved = list(get_importers() if importers is None else importers)
    results: list[Identification] = []
    for filepath in filepaths:
        path = Path(filepath)
        try:
            importer = beangulp_identify.identify(resolved, str(path))
        except BeangulpIdentifyError as exc:
            raise PipelineError(
                AMBIGUOUS_IMPORTER,
                f"{path.name} is claimed by more than one importer: {exc}",
            ) from exc
        if importer is None:
            raise PipelineError(NO_IMPORTER, f"No importer claims {path}.")
        results.append(Identification(path, importer))
    return results


def ledger_errors(paths: LedgerPaths) -> list:
    """Return the beancount errors for ``main.beancount``, as ``bean-check`` reports them.

    ``bean-check`` passes ``extra_validations=validation.HARDCORE_VALIDATIONS``, whose
    entire list is ``[validate_data_types]``; that validator checks the types of
    in-memory entry objects, so it cannot fire for a ledger parsed from text (verified:
    the option makes no difference on any text fixture). The option is passed anyway, so
    the in-process gate and the CLI stay equal if beancount ever adds a hardcore
    validation that can fire on text. An empty list is a clean ledger.

    Raises:
        PipelineError: with :data:`MAIN_MISSING` when there is no main file to check.
    """
    main = paths.main()
    if not main.is_file():
        raise PipelineError(MAIN_MISSING, f"{main} does not exist.")
    # Keep this option in lock-step with beancount/scripts/check.py; see the docstring.
    _entries, errors, _options = loader.load_file(
        main, extra_validations=validation.HARDCORE_VALIDATIONS
    )
    return list(errors)


def build_key_index(paths: LedgerPaths) -> set[str]:
    """Return every ``key`` the loaded ledger root contains, via beancount's loader.

    The index is **derived, never stored**, and it is read from ``main.beancount`` —
    the file ``bean-check`` runs. Globbing ``transactions/*.beancount`` directly would
    miss a key hand-written into ``main.beancount`` or any other included file, and a
    missed key is a silent duplicate: the project's primary correctness risk. Parsing
    with beancount's loader (not a regex) means a key inside a comment or a string is
    not mistaken for a real entry. A ledger with no ``main.beancount`` yet has no keys.
    """
    main = paths.main()
    if not main.is_file():
        return set()
    entries, _errors, _options = loader.load_file(main)
    index: set[str] = set()
    for entry in entries:
        key = _entry_key(entry)
        if key is not None:
            index.add(key)
    return index


# ------------------------------------------------------------------------------ Extract


def extract(
    paths: LedgerPaths,
    source: str,
    statement_path: str | Path,
    importers: Sequence[Importer] | None = None,
) -> ExtractResult:
    """Stage one statement into ``staging/<batch-id>/`` for human review.

    Writes the exact bytes ``append`` will write (produced by beancount's own
    printer), a machine-readable ``batch.json`` and the human ``report.txt``.
    Entries whose ``key`` is already in the ledger are dropped and counted.
    """
    _validate_source(source)
    statement = Path(statement_path)
    if not statement.is_file():
        raise PipelineError(STATEMENT_MISSING, f"Statement {statement} does not exist.")
    (identification,) = identify([statement], importers=importers)
    importer = identification.importer

    existing = _load_existing(paths)
    try:
        entries = list(importer.extract(str(statement.resolve()), existing) or [])
    except Exception as exc:
        # The statement is named by a content hash, never by its basename: the
        # basename can name the account holder (T-01d), and this message is what
        # the CLI prints after "refused:". Neither the hash nor the failure text
        # below may raise: a statement that is unreadable here, or a redaction key
        # that cannot be loaded, must still produce this typed refusal, never an
        # unhandled OSError or RedactionKeyError (R4-001/R3-001/R2-004).
        statement_id = _safe_statement_id(paths, statement)
        raise PipelineError(
            IMPORTER_RAISED,
            f"Importer {importer.name} refused statement {statement_id}: "
            f"{_importer_failure_text(exc)}",
        ) from exc

    importer.sort(entries)
    _validate_keys(entries)

    ledger_keys = build_key_index(paths)
    kept = [entry for entry in entries if _entry_key(entry) not in ledger_keys]
    skipped_entries = [entry for entry in entries if _entry_key(entry) in ledger_keys]
    proposed = _format_entries(kept)

    created = dt.datetime.now(dt.UTC)
    digest = hashlib.sha256(proposed).hexdigest()
    batch_id = f"{source}-{created:%Y%m%dT%H%M%SZ}-{digest[:8]}"
    batch_dir = paths.staging_batch(batch_id)
    if any((batch_dir / name).exists() for name in (_PROPOSED, _APPROVAL, _APPEND)):
        raise PipelineError(
            BATCH_EXISTS,
            f"Staging directory {batch_id!r} already holds a batch; refusing to replace "
            f"it or discard a hand edit. Remove it if it is stale.",
        )
    batch_dir.mkdir(parents=True, exist_ok=True)

    (batch_dir / _PROPOSED).write_bytes(proposed)
    metadata = {
        "source": source,
        "statement": statement.name,
        "statement_sha256": _sha256_file(statement),
        "created": created.isoformat(),
        "entries": len(kept),
        "skipped": len(skipped_entries),
    }
    (batch_dir / _BATCH).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (batch_dir / _REPORT).write_text(
        _build_report(batch_id, metadata, kept, skipped_entries), encoding="utf-8"
    )

    return ExtractResult(batch_id, batch_dir, batch_dir / _REPORT, len(kept), len(skipped_entries))


# ------------------------------------------------------------------------------ Approve


def approve(paths: LedgerPaths, batch_id: str) -> dict:
    """Record the sha256 of a staged batch's proposed bytes, binding the review.

    Raises:
        PipelineError: with :data:`BATCH_NOT_FOUND` when the batch does not exist.
    """
    batch_dir = paths.staging_batch(batch_id)
    proposed = batch_dir / _PROPOSED
    if not batch_dir.is_dir() or not proposed.is_file():
        raise PipelineError(BATCH_NOT_FOUND, f"No staged batch {batch_id!r}.")
    approval = {
        "batch_id": batch_id,
        "sha256": hashlib.sha256(proposed.read_bytes()).hexdigest(),
        "approved_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    (batch_dir / _APPROVAL).write_text(
        json.dumps(approval, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return approval


# ------------------------------------------------------------------------------- Append


def append(paths: LedgerPaths, batch_id: str) -> AppendResult:
    """Append an approved batch to the monthly transaction files, atomically.

    Refuses at the first failure in this order: batch exists, not already appended,
    approval exists, digest matches, main exists, the ledger is already clean, every
    staged key is absent. On a post-write gate failure, every touched file is
    restored byte-for-byte and files that did not exist are deleted.
    """
    batch_dir = paths.staging_batch(batch_id)
    proposed_path = batch_dir / _PROPOSED
    if not batch_dir.is_dir() or not proposed_path.is_file():
        raise PipelineError(BATCH_NOT_FOUND, f"No staged batch {batch_id!r}.")
    if (batch_dir / _APPEND).is_file():
        raise PipelineError(
            BATCH_ALREADY_APPENDED,
            f"Batch {batch_id!r} was already appended; refusing a second append.",
        )
    approval_path = batch_dir / _APPROVAL
    if not approval_path.is_file():
        raise PipelineError(
            APPROVAL_MISSING,
            f"Batch {batch_id!r} has no approval.json; run 'expensuchis approve {batch_id}' "
            f"after reviewing the report.",
        )

    proposed = proposed_path.read_bytes()
    digest = hashlib.sha256(proposed).hexdigest()
    try:
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(
            DIGEST_MISMATCH, f"Batch {batch_id!r} has an unreadable approval.json: {exc}"
        ) from exc
    if approval.get("sha256") != digest:
        raise PipelineError(
            DIGEST_MISMATCH,
            f"Batch {batch_id!r} changed after approval: the proposed bytes no longer "
            f"match the approved digest. Review and approve again.",
        )

    if not proposed.strip():
        raise PipelineError(
            BATCH_EMPTY,
            f"Batch {batch_id!r} is empty: every entry is already in the ledger, so "
            f"there is nothing to append.",
        )

    if not paths.main().is_file():
        raise PipelineError(
            MAIN_MISSING, f"{paths.main()} does not exist; the ledger was never bootstrapped."
        )

    errors = ledger_errors(paths)
    if errors:
        raise PipelineError(
            LEDGER_DIRTY,
            f"The ledger is already dirty ({errors[0].message}); refusing to append. "
            f"Fix the ledger first: an append must not 'fix' it.",
        )

    staged_entries = _staged_entries(proposed)
    ledger_keys = build_key_index(paths)
    present = sorted(
        key for key in (_entry_key(entry) for entry in staged_entries) if key in ledger_keys
    )
    if present:
        raise PipelineError(
            KEY_ALREADY_PRESENT,
            f"The ledger already contains key(s) {present}; the batch cannot be appended twice. "
            f"The ledger moved between staging and appending.",
        )

    chunks = _proposed_chunks(proposed, staged_entries)
    grouped: dict[Path, list[str]] = {}
    for entry, chunk in zip(staged_entries, chunks):
        grouped.setdefault(paths.transaction_file(entry.date), []).append(chunk)

    snapshots = {path: (path.read_bytes() if path.is_file() else None) for path in grouped}
    try:
        for path, group in grouped.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_append_chunks(snapshots[path] or b"", group))
    except OSError as exc:
        _restore(snapshots)
        raise PipelineError(
            POST_WRITE_DIRTY, f"Writing batch {batch_id!r} failed; rolled back: {exc}"
        ) from exc

    post_errors = ledger_errors(paths)
    # Beancount's load cache is keyed on (st_mtime_ns, st_size) per file, so writing
    # bytes to a month file always invalidates its cache entry; this gate reads fresh
    # bytes. Caching stays enabled (no loader.initialize(False)).
    if post_errors:
        _restore(snapshots)
        raise PipelineError(
            POST_WRITE_DIRTY,
            f"Appending batch {batch_id!r} broke the bean-check gate "
            f"({post_errors[0].message}); every touched file was rolled back.",
        )

    record = {
        "batch_id": batch_id,
        "status": APPENDED,
        "sha256": digest,
        "appended_at": dt.datetime.now(dt.UTC).isoformat(),
        "files": [str(path.relative_to(paths.root)) for path in grouped],
    }
    (batch_dir / _APPEND).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return AppendResult(batch_id, tuple(grouped))


def report(paths: LedgerPaths, batch_id: str) -> str:
    """Return the staged human review report for ``batch_id``."""
    batch_dir = paths.staging_batch(batch_id)
    path = batch_dir / _REPORT
    if not path.is_file():
        raise PipelineError(BATCH_NOT_FOUND, f"No staged batch {batch_id!r}.")
    return path.read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- Helpers


def _validate_source(source: str) -> None:
    if not source or source in {".", ".."} or "/" in source or "\\" in source or "\x00" in source:
        raise PipelineError(
            INVALID_SOURCE,
            f"Invalid source {source!r}: it must be a single name, not a path.",
        )


def _importer_failure_text(exc: Exception) -> str:
    """Return the text to embed in the ``importer-raised`` refusal for ``exc``.

    Only the exception's class name by default (R1-002/R3-002): an importer's raw
    ``str()`` can carry the statement's path -- an ``OSError`` names it directly,
    and a parser error can quote a line that includes it. ``exc`` stays chained
    with ``from`` at the call site for local debugging.

    :data:`_PRESERVED_MESSAGE_TYPES` is the exception -- each source's own
    ``CounterpartyClassificationError`` and ``StatementReadError``, whose messages
    are already built deliberately to be safe (see that constant's docstring).
    Reducing either to a bare class name would silently break the documented
    'never defaulted' workflow, or throw away the one safe detail
    ``StatementReadError`` exists to carry, so both are preserved instead.
    """
    if isinstance(exc, _PRESERVED_MESSAGE_TYPES):
        return str(exc)
    return type(exc).__name__


def _safe_statement_id(paths: LedgerPaths, statement: Path) -> str:
    """Return a keyed content hash naming ``statement``, without letting I/O escape.

    Called only while already handling an importer failure (T-01d follow-up,
    R4-001/R3-001/R2-004): a statement that becomes unreadable between ``extract``'s
    existence check and this point (for example, deleted by the same call that
    raised), or a redaction key that cannot be loaded or is corrupt, must still
    yield the typed ``importer-raised`` refusal below -- never an unhandled
    ``OSError`` or :class:`~expensuchis.redact.RedactionKeyError`. Either failure
    falls back to :data:`_STATEMENT_ID_UNREADABLE`.
    """
    try:
        key = load_or_create_key(paths.redaction_key())
        data = statement.read_bytes()
    except (OSError, RedactionKeyError):
        return _STATEMENT_ID_UNREADABLE
    return content_hash(data, key)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry_key(entry: object) -> str | None:
    meta = getattr(entry, "meta", None)
    if not meta:
        return None
    key = meta.get("key")
    return key if isinstance(key, str) and key else None


def _validate_keys(entries: Sequence) -> None:
    seen: set[str] = set()
    for entry in entries:
        key = _entry_key(entry)
        if key is None:
            date = getattr(entry, "date", "?")
            raise PipelineError(
                KEY_MISSING,
                f"Entry dated {date} carries no non-empty 'key' metadata field; "
                f"the pipeline refuses the batch.",
            )
        if key in seen:
            raise PipelineError(KEY_DUPLICATE, f"Key {key!r} appears more than once in the batch.")
        seen.add(key)


def _load_existing(paths: LedgerPaths) -> list:
    main = paths.main()
    if not main.is_file():
        return []
    entries, _errors, _options = loader.load_file(main)
    return list(entries)


def _format_entries(entries: Sequence) -> bytes:
    if not entries:
        return b""
    return "\n".join(printer.format_entry(entry) for entry in entries).encode("utf-8")


def _staged_entries(proposed: bytes) -> list:
    try:
        text = proposed.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PipelineError(STAGING_CORRUPT, f"Staged bytes are not UTF-8: {exc}") from exc
    entries, _errors, _options = loader.load_string(text)
    return list(entries)


def _proposed_chunks(proposed: bytes, entries: Sequence) -> list[str]:
    """Split staged bytes into blocks; every block must parse to exactly one entry."""
    text = proposed.decode("utf-8")
    body = text.removesuffix("\n")
    chunks = body.split("\n\n") if body else []
    if len(chunks) != len(entries):
        raise PipelineError(
            STAGING_CORRUPT,
            f"Staged bytes hold {len(chunks)} blocks but parse to {len(entries)} entries.",
        )
    for index, chunk in enumerate(chunks):
        parsed, _errors, _options = loader.load_string(chunk)
        if len(parsed) != 1:
            raise PipelineError(
                STAGING_CORRUPT,
                f"Staged block {index} parses to {len(parsed)} entries, not exactly one.",
            )
    return chunks


def _append_chunks(previous: bytes, chunks: Sequence[str]) -> bytes:
    body = ("\n\n".join(chunks) + "\n").encode("utf-8")
    if not previous:
        return body
    prefix = previous if previous.endswith(b"\n") else previous + b"\n"
    return prefix + b"\n" + body


def _restore(snapshots: dict[Path, bytes | None]) -> None:
    for path, before in snapshots.items():
        if before is None:
            if path.exists():
                path.unlink()
        else:
            path.write_bytes(before)


def _build_report(batch_id: str, metadata: dict, kept: Sequence, skipped: Sequence) -> str:
    lines = [
        f"batch_id: {batch_id}",
        f"source: {metadata['source']}",
        f"statement: {metadata['statement']}",
        f"statement_sha256: {metadata['statement_sha256']}",
        f"created: {metadata['created']}",
        f"entries: {metadata['entries']}",
        f"skipped: {metadata['skipped']}",
        "",
        "Proposed entries",
        "----------------",
        "",
    ]
    for entry in kept:
        lines.append(printer.format_entry(entry).rstrip("\n"))
        lines.append("")

    totals_header = (
        "Debit-side totals per currency (sum of positive postings; a balanced batch nets to zero)"
    )
    lines.append(totals_header)
    lines.append("-" * len(totals_header))
    totals = _totals_per_currency(kept)
    if totals:
        for currency, value in sorted(totals.items()):
            lines.append(f"  {currency}: {value:.2f}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Skipped (already present in the ledger)")
    lines.append("---------------------------------------")
    if skipped:
        for entry in skipped:
            key = _entry_key(entry)
            date = getattr(entry, "date", "?")
            payee = getattr(entry, "payee", "") or ""
            lines.append(f"  {key}  ({date}, {payee})")
    else:
        lines.append("  (none)")
    lines.append("")
    return "\n".join(lines)


def _totals_per_currency(entries: Sequence) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for entry in entries:
        for posting in getattr(entry, "postings", []) or []:
            units = getattr(posting, "units", None)
            if units is None or units.number is None:
                continue
            if units.number > 0:
                totals[units.currency] = totals.get(units.currency, Decimal(0)) + units.number
    return totals
