"""The Banco Provincia importer: wiring the ``Extracto de cuenta`` into the ledger.

The parser (:mod:`expensuchis.importers.provincia`) owns the statement's meaning
and its reconciliation gate; this module owns the ledger's side of it:

* **Whose account it is, derived from the path.** A statement must live at
  ``<...>/Provincia/<person>/<file>.pdf``; the cash account is
  ``Assets:Provincia:<person>:Caja``. The person is a directory name, so the
  declaration needs no lookup and no extra file. A path outside that layout is
  refused through :class:`SourceAccountError`, and the refusal never echoes the
  path or any statement text.
* **Where each kind posts.** The cash leg always posts ``movement.amount`` to the
  cash account and the counterpart leg posts its negation to a destination chosen
  by :class:`~expensuchis.importers.provincia.MovementKind`:

  ============================  =============================================
  Kind                          Counterpart account
  ============================  =============================================
  ``compra``                    :class:`CounterpartyMap`
  ``pago`` (bill payment)       :class:`CounterpartyMap`
  ``compra tarjeta``            :class:`CounterpartyMap` (debit-card purchase)
  ``pago visa``                 ``Liabilities:Provincia:<person>:Visa``
  ``sueldo``, ``haberes``       ``Income:<person>:Sueldo``
  ``depósito``                  ``Income:<person>:Depositos``
  ``crédito``                   ``Income:<person>:Creditos``
  ``devolución``                ``Income:<person>:Devoluciones``
  ``intereses`` (positive)      ``Income:<person>:Intereses``
  ``intereses`` (negative)      ``Expenses:CargosBancarios``
  ``comisión(es)``, ``cargos``  ``Expenses:CargosBancarios``
  ``débito``                    ``Expenses:CargosBancarios``
  ``recarga``                   ``Expenses:Otros``
  reference rows                ``Assets:TransferenciaEnTransito``
  ============================  =============================================

  Decisions worth recording, all made 2026-09-25 with T-06a (the card-purchase leg
  corrected the same day after independent verification and explicit user
  confirmation):

  - **Purchases, debit-card purchases and bill payments are resolved through the
    learned :class:`CounterpartyMap`;** anything unclassified stops the import with
    :class:`CounterpartyClassificationError`. ``compra tarjeta`` is a **debit-card
    purchase** (user-confirmed correction): the money leaves the account at purchase
    time, so it is spending that names a counterparty the map can classify, posted
    exactly like a ``compra`` — it never accrues on the card liability. Only the
    ``pago visa`` settlement — matched as the whole leading token pair, never a
    substring, so ``pago VISALUD`` stays a bill payment — brings the credit-card
    liability back toward zero.
  - **Reference rows post to** ``Assets:TransferenciaEnTransito`` — a
    **user-confirmed classification** (2026-09-25): the rows are immediate transfers
    (``pagos por transferencia inmediata``), not a structural guess left silent.
    The frozen vocabulary has no transfer verb, and the description-less reference
    row is the only shape left to carry them; the clearing account is loud by
    design — if a reference row were anything else, it would not return to zero and
    the discrepancy surfaces in review. What is never done is defaulting them into
    an expense category. The recipient CUIT (the head's ``X:XXXXXXXXXXX`` field) is
    personal data: it is key material that reaches the private ledger's staged and
    report bytes by design, never a module or probe log line.
  - **``intereses`` posts by sign:** bank interest the account earns is coarse
    income, interest charged is a bank fee. The statement gives no separate verb
    for the two directions; the sign is the only discriminator it offers.
  - **``recarga`` posts to** ``Expenses:Otros`` as a deliberately coarse choice: a
    top-up (phone, transport card) is spending, but the statement does not say of
    what, and inventing a specific category for one row per quarter would be a
    category for completeness. A future re-classification is a find-and-replace.
  - **``sueldo`` and ``haberes`` share** ``Income:<person>:Sueldo``: both name the
    same origin (wages), so two accounts would split one total the user reads as
    one. ``depósito``/``crédito``/``devolución`` keep their own coarse accounts.
* **The natural key.** ``date:identity:amount`` from
  :func:`expensuchis.importers.provincia.movement_keys` is recorded as the entry's
  ``key`` metadata, which the pipeline deduplicates on; for a reference row the
  identity is the recipient CUIT when the head carries one, so two transfers to the
  same person on the same day are distinct entries.

The importer follows the pipeline contract: a failure raises, it never returns
partial entries. ``identify`` never raises and never prints; the PDF engine and the
counterparty map are both created lazily, so constructing the importer (and
therefore :func:`expensuchis.importers.get_importers`) needs neither a ledger
directory nor a statement.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from beancount.core import data
from beangulp import Importer

from ..counterparties import CounterpartyMap
from . import provincia
from .bbva import is_bbva_extracto
from .pdf import read_pdf
from .provincia import (
    Extracto,
    ExtractoParseError,
    Movement,
    MovementKind,
    movement_keys,
)

__all__ = [
    "CARD_ACCOUNT",
    "CASH_ACCOUNT",
    "FEES_ACCOUNT",
    "MARKER",
    "SOURCE",
    "TOPUP_ACCOUNT",
    "TRANSFER_ACCOUNT",
    "CounterpartyClassificationError",
    "ProvinciaImporter",
    "SourceAccountError",
    "StatementReadError",
    "build_entries",
    "derive_person",
]

#: The source name used both as the beangulp importer identity and as the
#: ``source`` column of the counterparty map.
SOURCE = "Provincia"

#: The marker every account statement carries in its title. Matched after folding
#: (casefold + accent stripping) because the statement's own title casing varies
#: between renderings; the folded form of every plausible rendering is this string.
MARKER = "Extracto de Cuenta"

#: The cash account for a person, derived from the per-person statement folder.
CASH_ACCOUNT = "Assets:Provincia:{person}:Caja"

#: The card liability the card settlement posts to; the sample model opens
#: ``Liabilities:Provincia:P2:Visa``. Only ``pago visa`` reaches it: a
#: ``compra tarjeta`` is a debit-card purchase resolved through the map.
CARD_ACCOUNT = "Liabilities:Provincia:{person}:Visa"

#: The clearing account reference rows post to. See the posting-map decisions above.
TRANSFER_ACCOUNT = "Assets:TransferenciaEnTransito"

#: Where bank fees and charged interest post; the tree's own bank-charges category.
FEES_ACCOUNT = "Expenses:CargosBancarios"

#: Where a top-up posts: spending, but the statement does not say of what.
TOPUP_ACCOUNT = "Expenses:Otros"

#: Coarse income accounts, one per origin, per the accounting model.
INCOME_ACCOUNTS: dict[MovementKind, str] = {
    MovementKind.SALARY: "Income:{person}:Sueldo",
    MovementKind.EARNINGS: "Income:{person}:Sueldo",
    MovementKind.DEPOSIT: "Income:{person}:Depositos",
    MovementKind.CREDIT: "Income:{person}:Creditos",
    MovementKind.REFUND: "Income:{person}:Devoluciones",
}
INTEREST_INCOME_ACCOUNT = "Income:{person}:Intereses"

#: The one refusal message for a path that does not declare its account. It names
#: the expected structure and the account convention, and deliberately carries no
#: path, filename or statement text.
_SOURCE_ACCOUNT_MESSAGE = (
    "cannot derive the Banco Provincia cash account from the statement path: expected "
    "'.../Provincia/<person>/<file>.pdf', which maps to "
    "'Assets:Provincia:<person>:Caja'. The <person> component must be a single "
    "directory name that is not empty, not '.' or '..', and contains no ':' and no "
    "whitespace."
)


class SourceAccountError(ValueError):
    """The statement path does not declare whose Banco Provincia account it is.

    The message names the expected layout and the account convention only; it never
    echoes the offending path, so a diagnostic cannot leak a statement location.
    """


class CounterpartyClassificationError(ValueError):
    """A purchase or bill payment names a counterparty absent from the map.

    The message lists each unknown counterparty with the context a human needs to
    classify it and the ``counterparties.tsv`` row to append. The importer never
    chooses a category: the suggestion carries the literal ``expense:<category>``
    placeholder.
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


_PATH_SEPARATORS = re.compile(r"[\\/]")

#: A parenthesized token in a description — which can be the counterparty's
#: CUIT/CUIL — is per-row structure, never part of the merchant identity.
_PARENTHESIZED_RE = re.compile(r"\([^)]*\)")
#: Per-row noise tokens: a pure-digit run, a date-like ``dd/mm``/``dd/mm/yy``
#: (either separator) or a time-like ``hh:mm`` run. They differ from row to row
#: for the same merchant, so they must not enter the map key.
_DIGIT_TOKEN_RE = re.compile(r"\d+")
_DATE_TOKEN_RE = re.compile(r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?")
_TIME_TOKEN_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
#: A CUIT/CUIL is an 11-digit run. It must never reach a log line: the refusal
#: message is printed by the CLI and can end up in an agent's transcript.
_CUIT_RUN_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")


def derive_person(filepath: str | Path) -> str:
    """Return the person token from ``<...>/Provincia/<person>/<file>``.

    The layout is checked structurally: a component named exactly ``Provincia``,
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


def _is_noise_token(token: str) -> bool:
    """Whether a token is per-row noise: pure digits, a date or a time."""
    return bool(
        _DIGIT_TOKEN_RE.fullmatch(token)
        or _DATE_TOKEN_RE.fullmatch(token)
        or _TIME_TOKEN_RE.fullmatch(token)
    )


def _raw_name(movement: Movement) -> str:
    """Return the movement's stable counterparty identity from its description.

    Real description rows wrap the merchant's name in per-row structure — a ``DE``
    marker, a ``TARJETA`` token with the purchase's date and time, the
    counterparty's CUIT/CUIL in parentheses, stray digit, date and time tokens —
    so the verbatim remainder after the verb is not stable across rows of the
    same merchant. The identity is the description with all of that stripped:
    the leading verb, a following ``DE``, a ``TARJETA`` token, any parenthesized
    token and any pure-digit, date-like or time-like token, whitespace
    collapsed. Two rows of one merchant that differ only in their per-row id and
    date/time therefore yield the same identity, which is both the
    ``(source, raw_name)`` map key and the transaction payee.

    The parenthesized token can be the counterparty's CUIT/CUIL: it is stripped
    here and never reaches a module or probe log line. The verbatim description
    still flows into the natural key and the narration, which live only in the
    private ledger's staged and report bytes by design.

    Card settlements never reach the map, so their remainder is never resolved.
    """
    unparenthesized = _PARENTHESIZED_RE.sub(" ", movement.description)
    tokens = unparenthesized.split()
    if not tokens:
        return ""
    tokens = tokens[1:]  # the leading verb
    if tokens and provincia.fold(tokens[0]) == "de":
        tokens = tokens[1:]
    if tokens and provincia.fold(tokens[0]) == "tarjeta":
        tokens = tokens[1:]
    return " ".join(token for token in tokens if not _is_noise_token(token))


def _fixed_destination(kind: MovementKind, person: str, amount: Decimal) -> str:
    """Return the counterpart account for a kind that is not map-resolved."""
    if kind is MovementKind.CARD_SETTLEMENT:
        return CARD_ACCOUNT.format(person=person)
    if kind is MovementKind.REFERENCE:
        return TRANSFER_ACCOUNT
    if kind is MovementKind.INTEREST:
        template = INTEREST_INCOME_ACCOUNT if amount > 0 else FEES_ACCOUNT
        return template.format(person=person)
    if kind in (MovementKind.FEES, MovementKind.CHARGES, MovementKind.DEBIT):
        return FEES_ACCOUNT
    if kind is MovementKind.TOPUP:
        return TOPUP_ACCOUNT
    account = INCOME_ACCOUNTS.get(kind)
    if account is None:
        # UNKNOWN is refused by build_entries before this is reached.
        raise AssertionError(f"no fixed destination for kind {kind!r}")  # pragma: no cover
    return account.format(person=person)


def _destination_account(destination: str) -> str:
    """Return the ledger account named by a resolved counterparty-map destination.

    A map value is ``internal:<account>`` or ``expense:<category>``. The marker says
    whether the counterparty is inside this ledger; the part after it is the account,
    used verbatim. Only the marker is removed — splitting on every ``:`` would
    truncate ``internal:Assets:Provincia:P2:Caja`` to ``Assets``. The prefixed form
    cannot be the posting account itself: beancount requires a capitalised root, so
    ``expense:Expenses:X`` is a syntax error.
    """
    _marker, _separator, account = destination.partition(":")
    return account


def _entry(
    movement: Movement, person: str, counterpart: str, key: str, payee: str
) -> data.Transaction:
    meta = {"key": key}
    cash = _cash_account(person)
    narration = movement.description or movement.reference
    postings = [
        data.Posting(cash, data.Amount(movement.amount, "ARS"), None, None, None, None),
        data.Posting(counterpart, data.Amount(-movement.amount, "ARS"), None, None, None, None),
    ]
    return data.Transaction(
        meta,
        movement.date,
        "*",
        payee,
        narration,
        frozenset(),
        frozenset(),
        postings,
    )


def _redact_cuit(text: str) -> str:
    """Replace any CUIT/CUIL-shaped 11-digit run in ``text`` with a placeholder.

    The refusal message is human-facing and the CLI prints it, so it must not
    carry the counterparty's CUIT into a transcript. The merchant identity stays
    readable; only the identifier is redacted.
    """
    return _CUIT_RUN_RE.sub("<cuit>", text)


def _unclassified_message(unclassified: dict[str, Movement]) -> str:
    """Build the pinned refusal message for the unique unclassified counterparties.

    The count is of **unique counterparties**, not movements, and the first movement
    that named each counterparty supplies the displayed date, amount and description.
    The message is always plural — ``1 counterparties are not classified.`` — because
    the pinned T-04c shape is a single string form, and a human reading it at a
    terminal loses nothing to the fixed plural. The displayed description has any
    CUIT/CUIL-shaped run redacted, because the CLI prints this message.
    """
    lines = [f"{len(unclassified)} counterparties are not classified."]
    for raw_name, movement in unclassified.items():
        lines.append(
            f"  {movement.date.isoformat()}  {movement.amount:,.2f} ARS  "
            f'"{_redact_cuit(movement.description)}"  "{raw_name}"'
        )
        lines.append("    append to counterparties.tsv:")
        lines.append(f"      {SOURCE}\t{raw_name}\texpense:<category>")
    return "\n".join(lines)


def build_entries(
    extracto: Extracto, person: str, counterparty_map: CounterpartyMap
) -> list[data.Transaction]:
    """Turn a reconciled statement into beancount transactions.

    Pure over its arguments: it touches no filesystem and no environment, so it is
    the unit-test seam for posting logic and the counterparty refusal.

    Raises:
        ExtractoParseError: a purchase, debit-card purchase or bill payment has no
            counterparty name after its verb, or (defensively) a movement's head is
            outside the vocabulary and the reference shape.
        CounterpartyClassificationError: at least one purchase, debit-card purchase
            or bill payment names a counterparty absent from the map. Every unknown
            counterparty is collected before the refusal, so one run tells the whole
            story.
    """
    keys = movement_keys(extracto.movements)
    entries: list[data.Transaction] = []
    unclassified: dict[str, Movement] = {}
    for index, (movement, key) in enumerate(zip(extracto.movements, keys)):
        kind = movement.kind
        if kind is MovementKind.UNKNOWN:
            raise ExtractoParseError(
                f"movement {index + 1} (line {movement.line}) uses a head outside the "
                f"frozen vocabulary and the reference shape; refusing to build an entry for it"
            )
        if kind in (MovementKind.PURCHASE, MovementKind.BILL_PAYMENT, MovementKind.CARD_PURCHASE):
            raw_name = _raw_name(movement)
            if not raw_name:
                raise ExtractoParseError(
                    f"movement {index + 1} (line {movement.line}): the purchase, card "
                    f"purchase or bill payment has no counterparty name after its verb"
                )
            destination = counterparty_map.resolve(SOURCE, raw_name)
            if destination is None:
                unclassified.setdefault(raw_name, movement)
                continue
            counterpart = _destination_account(destination)
            payee = raw_name
        else:
            counterpart = _fixed_destination(kind, person, movement.amount)
            payee = SOURCE
        entries.append(_entry(movement, person, counterpart, key, payee))

    if unclassified:
        raise CounterpartyClassificationError(_unclassified_message(unclassified))
    return entries


class ProvinciaImporter(Importer):
    """Import one Banco Provincia ``Extracto de cuenta`` PDF per person.

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
        """Claim the file when its extracted text carries the statement marker.

        The marker is matched after folding (casefold + accent stripping) because
        the statement's title casing varies between renderings, but the match is a
        raw substring over the whole document, not a structural check — and a real
        BBVA caja de ahorro statement can carry the phrase "Extracto de Cuenta"
        somewhere in its own boilerplate (not the table itself). So the file is
        yielded whenever BBVA's structural check claims it: two claimants make
        ``extract`` refuse the file as ``ambiguous-importer``. Never raises and
        never prints: any extraction failure is a non-match.
        """
        try:
            text = self._read_text(filepath)
        except Exception:  # noqa: BLE001 - an unreadable file is simply not ours
            return False
        return provincia.fold(MARKER) in provincia.fold(text) and not (is_bbva_extracto(text))

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
        :class:`~expensuchis.importers.provincia.ReconciliationError` and
        :class:`~expensuchis.importers.provincia.ExtractoParseError` propagate
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
        extracto = provincia.parse_extracto(text)
        counterparty_map = (
            self._counterparty_map if self._counterparty_map is not None else CounterpartyMap()
        )
        return build_entries(extracto, person, counterparty_map)

    def _read_text(self, filepath: str) -> str:
        if self._text_reader is not None:
            return self._text_reader(str(filepath))
        return read_pdf(filepath)[0]
