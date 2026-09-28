"""The Brubank importer: wiring the ``Resumen de Movimientos`` into the ledger.

The parser (:mod:`expensuchis.importers.brubank`) owns the statement's meaning
and its reconciliation gate; this module owns the ledger's side of it:

* **Whose account it is, derived from the path.** A statement must live at
  ``<...>/Brubank/<person>/<file>.pdf``; the cash account is
  ``Assets:Brubank:<person>:Caja``. The person is a directory name, so the
  declaration needs no lookup and no extra file. A path outside that layout is
  refused through :class:`SourceAccountError`, and the refusal never echoes the
  path or any statement text.
* **Where each kind posts.** The cash leg always posts ``movement.amount`` to the
  cash account and the counterpart leg posts its negation to a destination chosen
  by :class:`~expensuchis.importers.brubank.MovementKind`:

  ==========================================  =================================
  Kind                                        Counterpart account
  ==========================================  =================================
  ``ordinary`` (every named counterparty)      :class:`CounterpartyMap`
  ``intereses`` (``Intereses pagados``)        ``Income:<person>:Intereses``
  ``internal_transfer`` (``De una cuenta      ``Assets:TransferenciaEnTransito``
  tuya - <bank>``)
  ==========================================  =================================

  **Unlike Provincia and Mercado Pago, the counterparty map here sees both
  credits and debits.** Brubank's vocabulary carries no verb: every ordinary
  row simply names a counterparty (recon: "categorization here is name
  matching, never verb parsing"), so the map is consulted for every direction,
  not only spending. The map's two-value contract from
  :mod:`expensuchis.counterparties` (``internal:<account>`` or
  ``expense:<category>``) does not change: ``internal:`` still means "this
  counterparty is an account in this ledger" and ``expense:`` still means "the
  account to post the other leg to", whichever direction the money moves — a
  credit classified ``expense:Income:<person>:Devoluciones`` is exactly as
  legal as a debit classified ``expense:Expenses:Servicios``, because only the
  literal account after the marker is used.

  Decisions recorded 2026-09-26 with T-08 (see ``odd/tasks/family-ledger.md``):

  - **``Intereses pagados`` posts to** ``Income:<person>:Intereses``, mirroring
    Provincia's positive-interest branch. Unlike Provincia, there is no observed
    negative counterpart in Brubank's vocabulary (reconnaissance: the only
    occurrence is a credit) and none is coded for; a future statement carrying a
    debit ``Intereses pagados`` row would still post here, which is an
    assumption this unit's acceptance probe (T-08c) should check against the
    real file.
  - **``De una cuenta tuya - <bank>`` posts to**
    ``Assets:TransferenciaEnTransito`` regardless of which bank is named,
    following the 2026-09-24 clearing-account decision: the clearing account is
    for transfers between accounts already in this ledger, and every such
    Brubank row is exactly that (the reconnaissance's driving example was
    ``De una cuenta tuya - BBVA``, appearing in both the Brubank and the BBVA
    statements). ``Movement.transfer_bank`` is not used to choose the account —
    there is only one clearing account — and is kept on the movement only as a
    diagnostic, never posted or echoed here.
  - **``Imp. Trans. Financieras`` is never posted.** It is the header's own
    total (:attr:`~expensuchis.importers.brubank.Resumen.financial_transactions_tax`)
    and the parser refuses any row carrying that description; this module has
    nothing to do with it.
* **The natural key.** ``date:#Ref:amount`` from
  :func:`expensuchis.importers.brubank.movement_keys` is recorded as the entry's
  ``key`` metadata, which the pipeline deduplicates on.

The importer follows the pipeline contract: a failure raises, it never returns
partial entries. ``identify`` never raises and never prints; the PDF engine and the
counterparty map are both created lazily, so constructing the importer (and
therefore :func:`expensuchis.importers.get_importers`) needs neither a ledger
directory nor a statement.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from beancount.core import data
from beangulp import Importer

from ..counterparties import CounterpartyMap
from .brubank import (
    Movement,
    MovementKind,
    Resumen,
    is_resumen_movimientos,
    movement_keys,
    parse_resumen,
)
from .pdf import read_pdf

__all__ = [
    "CASH_ACCOUNT",
    "SOURCE",
    "TRANSFER_ACCOUNT",
    "BrubankImporter",
    "CounterpartyClassificationError",
    "SourceAccountError",
    "StatementReadError",
    "build_entries",
    "derive_person",
]

#: The source name used both as the beangulp importer identity and as the
#: ``source`` column of the counterparty map.
SOURCE = "Brubank"

#: The cash account for a person, derived from the per-person statement folder.
CASH_ACCOUNT = "Assets:Brubank:{person}:Caja"

#: The clearing account an internal transfer posts to. See the posting-map
#: decisions above; shared with every other importer's own clearing postings, so
#: both sides of one household transfer net to zero once both statements are
#: imported (2026-09-24 decision).
TRANSFER_ACCOUNT = "Assets:TransferenciaEnTransito"

#: Where ``Intereses pagados`` posts. See the posting-map decisions above.
INTEREST_INCOME_ACCOUNT = "Income:{person}:Intereses"

_PATH_SEPARATORS = re.compile(r"[\\/]")

#: The one refusal message for a path that does not declare its account. It names
#: the expected structure and the account convention, and deliberately carries no
#: path, filename or statement text.
_SOURCE_ACCOUNT_MESSAGE = (
    "cannot derive the Brubank cash account from the statement path: expected "
    "'.../Brubank/<person>/<file>.pdf', which maps to "
    "'Assets:Brubank:<person>:Caja'. The <person> component must be a single "
    "directory name that is not empty, not '.' or '..', and contains no ':' and no "
    "whitespace."
)


class SourceAccountError(ValueError):
    """The statement path does not declare whose Brubank account it is.

    The message names the expected layout and the account convention only; it never
    echoes the offending path, so a diagnostic cannot leak a statement location.
    """


class CounterpartyClassificationError(ValueError):
    """An ordinary movement names a counterparty absent from the map.

    The message lists each unknown counterparty with the context a human needs to
    classify it and the ``counterparties.tsv`` row to append. The importer never
    chooses a category: the suggestion carries the literal ``expense:<category>``
    placeholder, whether the movement is a credit or a debit.
    """


class StatementReadError(ValueError):
    """The statement text could not be read.

    A reader failure (a missing file, a corrupt PDF, an engine error) carries raw
    exception text that can include the real path — and a statement filename can
    name its holder. ``extract`` wraps that failure in this exception with the
    exception **class only**, never the path or the reader's message, because
    ``pipeline.extract`` re-wraps the message and the CLI prints it, so it can end
    up in a remote agent's context.
    """


def derive_person(filepath: str | Path) -> str:
    """Return the person token from ``<...>/Brubank/<person>/<file>``.

    The layout is checked structurally: a component named exactly ``Brubank``,
    then exactly one directory (the person), then the file. The person token must be
    a single non-empty directory name, not ``.`` or ``..``, with no ``:`` and no
    whitespace.

    Raises:
        SourceAccountError: when no single component satisfies the layout or the
            person token is invalid. The message never echoes the path.
    """
    parts = _PATH_SEPARATORS.split(str(filepath))
    if not parts or not parts[-1]:
        raise SourceAccountError(_SOURCE_ACCOUNT_MESSAGE)
    candidates = [
        index for index, part in enumerate(parts) if part == SOURCE and index + 2 == len(parts) - 1
    ]
    if len(candidates) != 1:
        raise SourceAccountError(_SOURCE_ACCOUNT_MESSAGE)
    return _validate_person(parts[candidates[0] + 1])


def _validate_person(person: str) -> str:
    if (
        not person
        or person in {".", ".."}
        or ":" in person
        or any(character.isspace() for character in person)
    ):
        raise SourceAccountError(_SOURCE_ACCOUNT_MESSAGE)
    return person


def _cash_account(person: str) -> str:
    return CASH_ACCOUNT.format(person=person)


def _destination_account(destination: str) -> str:
    """Return the ledger account named by a resolved counterparty-map destination.

    A map value is ``internal:<account>`` or ``expense:<category>``. The marker says
    whether the counterparty is inside this ledger; the part after it is the account,
    used verbatim — regardless of the movement's own sign. Only the marker is
    removed — splitting on every ``:`` would truncate
    ``internal:Assets:Brubank:P2:Caja`` to ``Assets``. The prefixed form cannot be
    the posting account itself: beancount requires a capitalised root, so
    ``expense:Expenses:X`` is a syntax error.
    """
    _marker, _separator, account = destination.partition(":")
    return account


def _fixed_destination(kind: MovementKind, person: str) -> str:
    """Return the counterpart account for a kind that is not map-resolved."""
    if kind is MovementKind.INTEREST:
        return INTEREST_INCOME_ACCOUNT.format(person=person)
    if kind is MovementKind.INTERNAL_TRANSFER:
        return TRANSFER_ACCOUNT
    # ORDINARY is map-resolved before this is reached.
    raise AssertionError(f"no fixed destination for kind {kind!r}")  # pragma: no cover


def _entry(
    movement: Movement, person: str, counterpart: str, key: str, payee: str
) -> data.Transaction:
    meta = {"key": key}
    cash = _cash_account(person)
    postings = [
        data.Posting(cash, data.Amount(movement.amount, "ARS"), None, None, None, None),
        data.Posting(counterpart, data.Amount(-movement.amount, "ARS"), None, None, None, None),
    ]
    return data.Transaction(
        meta,
        movement.date,
        "*",
        payee,
        movement.description,
        frozenset(),
        frozenset(),
        postings,
    )


def _unclassified_message(unclassified: dict[str, Movement]) -> str:
    """Build the pinned refusal message for the unique unclassified counterparties.

    The count is of **unique counterparties**, not movements, and the first movement
    that named each counterparty supplies the displayed date, amount and description.
    The message is always plural — ``1 counterparties are not classified.`` — because
    the pinned T-04c shape is a single string form, and a human reading it at a
    terminal loses nothing to the fixed plural.
    """
    lines = [f"{len(unclassified)} counterparties are not classified."]
    for raw_name, movement in unclassified.items():
        lines.append(
            f"  {movement.date.isoformat()}  {movement.amount:,.2f} ARS  "
            f'"{movement.description}"  "{raw_name}"'
        )
        lines.append("    append to counterparties.tsv:")
        lines.append(f"      {SOURCE}\t{raw_name}\texpense:<category>")
    return "\n".join(lines)


def build_entries(
    resumen: Resumen, person: str, counterparty_map: CounterpartyMap
) -> list[data.Transaction]:
    """Turn a reconciled statement into beancount transactions.

    Pure over its arguments: it touches no filesystem and no environment, so it is
    the unit-test seam for posting logic and the counterparty refusal.

    Every :attr:`~expensuchis.importers.brubank.MovementKind.ORDINARY` movement is
    resolved through ``counterparty_map`` by its description — the identity the
    parser already reports, with no verb to strip — whether it is a credit or a
    debit. :attr:`~expensuchis.importers.brubank.MovementKind.INTEREST` and
    :attr:`~expensuchis.importers.brubank.MovementKind.INTERNAL_TRANSFER` always
    post to their fixed accounts and are never map-resolved.

    Raises:
        CounterpartyClassificationError: at least one ordinary movement names a
            counterparty absent from the map. Every unknown counterparty is
            collected before the refusal, so one run tells the whole story.
    """
    keys = movement_keys(resumen.movements)
    entries: list[data.Transaction] = []
    unclassified: dict[str, Movement] = {}
    for movement, key in zip(resumen.movements, keys):
        kind = movement.kind
        if kind is MovementKind.ORDINARY:
            raw_name = movement.description
            destination = counterparty_map.resolve(SOURCE, raw_name)
            if destination is None:
                unclassified.setdefault(raw_name, movement)
                continue
            counterpart = _destination_account(destination)
            payee = raw_name
        else:
            counterpart = _fixed_destination(kind, person)
            payee = SOURCE
        entries.append(_entry(movement, person, counterpart, key, payee))

    if unclassified:
        raise CounterpartyClassificationError(_unclassified_message(unclassified))
    return entries


class BrubankImporter(Importer):
    """Import one Brubank ``Resumen de Movimientos`` PDF per person.

    ``counterparty_map`` and ``text_reader`` are seams. The map is created lazily on
    the first ``extract`` so that ``identify`` and ``get_importers()`` need no ledger
    directory or environment variable; ``text_reader`` replaces PDF extraction in
    tests, and defaults to :func:`expensuchis.importers.pdf.read_pdf`.
    """

    def __init__(
        self,
        counterparty_map: CounterpartyMap | None = None,
        text_reader: Callable[[str], str] | None = None,
    ) -> None:
        self._counterparty_map = counterparty_map
        self._text_reader = text_reader

    @property
    def name(self) -> str:
        """The importer identity, and the map's ``source`` column."""
        return SOURCE

    def identify(self, filepath: str) -> bool:
        """Claim the file when its extracted text carries the statement's title line.

        Delegates to :func:`~expensuchis.importers.brubank.is_resumen_movimientos`,
        the parser's own structural marker check, so identification and parsing can
        never disagree on what counts as the title. Never raises and never prints:
        any extraction failure is a non-match.
        """
        try:
            text = self._read_text(filepath)
        except Exception:  # noqa: BLE001 - an unreadable file is simply not ours
            return False
        return is_resumen_movimientos(text)

    def account(self, filepath: str) -> str:
        """Return the archival account: the person's derived cash account.

        Required by beangulp's ``Importer`` ABC. The statement belongs to the cash
        account its per-person folder declares.
        """
        return _cash_account(derive_person(filepath))

    def sort(self, entries, reverse: bool = False) -> None:
        """Sort in place by date, then by the natural key.

        The base ``Importer.sort`` delegates to ``data.entry_sortkey``, which reads
        ``meta["lineno"]``; entries from this importer carry only the natural ``key``
        by design, so the base implementation would raise ``KeyError``. Document order
        is already chronological, so a date-then-key sort is total and deterministic,
        which is all the pipeline's month grouping needs.
        """
        entries.sort(key=lambda entry: (entry.date, entry.meta.get("key", "")), reverse=reverse)

    def extract(self, filepath: str, existing) -> list[data.Transaction]:
        """Parse and reconcile the statement, then build its transactions.

        The person comes from the path and the meaning from the parser;
        :class:`~expensuchis.importers.brubank.ReconciliationError` and
        :class:`~expensuchis.importers.brubank.ResumenParseError` propagate
        unchanged because their messages are already masked. A reader failure is
        wrapped in :class:`StatementReadError` — the raw message can carry the real
        path or a holder's name, and the CLI prints it. Nothing partial is ever
        returned.
        """
        person = derive_person(filepath)
        try:
            text = self._read_text(filepath)
        except Exception as exc:  # noqa: BLE001 - the class is the only safe detail
            # ``from None`` on purpose: the cause's message is exactly the leak.
            raise StatementReadError(
                f"cannot read the statement text: {type(exc).__name__}"
            ) from None
        resumen = parse_resumen(text)
        counterparty_map = (
            self._counterparty_map if self._counterparty_map is not None else CounterpartyMap()
        )
        return build_entries(resumen, person, counterparty_map)

    def _read_text(self, filepath: str) -> str:
        if self._text_reader is not None:
            return self._text_reader(str(filepath))
        return read_pdf(filepath)[0]
