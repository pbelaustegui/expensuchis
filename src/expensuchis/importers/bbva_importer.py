"""The BBVA importer: wiring the ``Extracto consolidado`` into the ledger.

The parser (:mod:`expensuchis.importers.bbva`) owns the statement's meaning and
its reconciliation gate; this module owns the ledger's side of it:

* **Whose account it is, derived from the path -- with one asymmetry.** A
  statement must live at ``<...>/Bbva/<person>/<file>.pdf`` (recon 2026-09-27:
  the real folder is titlecase ``Bbva``, not the bank's own all-caps
  ``BBVA``). :data:`SOURCE` (``"BBVA"``) is the *display* identity instead --
  the ledger accounts, the counterparty map's ``source`` column, the importer's
  ``name`` and every fixed-destination payee all say ``BBVA``, matching the
  T-07 owner decisions' own account templates. A path outside the ``Bbva``
  layout is refused through :class:`SourceAccountError`, and the refusal never
  echoes the path or any statement text.
* **One statement, more than one ledger account.** Unlike every other importer
  here, a consolidated statement can carry several sub-accounts at once. Each
  is mapped to its ledger account by **kind + currency**, with no private
  configuration (T-07b owner decision 2026-09-27):

  ==========  ==========  =====================================
  Kind        Currency    Ledger account
  ==========  ==========  =====================================
  ``cc``      ``$``       ``Assets:BBVA:<person>:CuentaCorriente``
  ``ca``      ``$``       ``Assets:BBVA:<person>:Caja``
  ``ca``      ``u$s``     ``Assets:BBVA:<person>:CajaUSD``
  ``ca``      ``eur``     ``Assets:BBVA:<person>:CajaEUR``
  ==========  ==========  =====================================

  Any other kind/currency pair, or two blocks sharing one kind + currency in
  the same statement, refuses through :class:`AccountMappingError` -- never
  merged, never silently dropped. The refusal names the offending kind and
  currency tokens only (a closed, non-sensitive vocabulary); it never echoes
  the block's real account number, which stays a :class:`Movement` field.
  Accepted tradeoff, recorded with the decision: two same-kind, same-currency
  accounts held in *separate* statements share one ledger account, and only
  the balance assertions would catch it.
* **Where each kind posts.** The cash leg always posts ``movement.amount`` to
  the sub-account's own mapped account (never a single fixed cash account) and
  the counterpart leg posts its negation to a destination chosen by
  :class:`~expensuchis.importers.bbva.MovementKind` (T-07 owner decision 4):

  ========================  ===========================================
  Kind                      Counterpart account
  ========================  ===========================================
  ``debit_card_purchase``   :class:`CounterpartyMap`, keyed by merchant
  ``visa_settlement``       ``Liabilities:BBVA:<person>:Visa``
  ``mastercard_settlement`` ``Liabilities:BBVA:<person>:Mastercard``
  ``cash_withdrawal``       ``Expenses:Efectivo``
  ``salary``                ``Income:<person>:Sueldo``
  ``interest``               ``Income:<person>:Intereses``
  ``transfer_out``          :class:`CounterpartyMap`, keyed by recipient CUIT
  ``transfer_in``           :class:`CounterpartyMap`, keyed by the name left
                            after stripping ``transferencia inmediata``
  ========================  ===========================================

  - **A sent transfer's map key is its joined recipient CUIT, not any name in
    its concept.** The concept's optional trailing word (see
    :mod:`expensuchis.importers.bbva`'s ``_TRANSFER_OUT_RE``) is present on
    some rows and absent on others, so it cannot identify the same real-world
    recipient across statements; the CUIT, joined from the sent-transfers
    section, is always present once the parser's own ``transfers-matched``
    check holds. It is "key material only" (T-07 owner decision 4): used to
    resolve the map and, once resolved, written to the private ledger as the
    entry's payee -- exactly where Provincia's own reference-row CUIT already
    lives by design -- but **never echoed in a refusal message**, where it is
    always redacted to ``<cuit>``.
  - **A received transfer's map key is a name, not a CUIT** --
    ``transfer_in`` carries no recipient CUIT (only ``transfer_out`` does).
    When no name is left after stripping the verb (a bare
    ``TRANSFERENCIA INMEDIATA`` row), the movement has no counterparty
    identity at all, and building its entry raises
    :class:`~expensuchis.importers.bbva.ExtractoConsolidadoParseError`
    (reusing the parser's own structural exception, as Provincia's
    ``build_entries`` does) rather than resolving an empty key.
  - **"Own accounts" post to** ``Assets:TransferenciaEnTransito`` **through the
    map, not a fixed destination.** Unlike Brubank's structurally-distinguished
    internal transfer, BBVA's vocabulary carries no such marker; the map's
    ``internal:`` prefix is what says "this counterparty is an account in this
    ledger" (:mod:`expensuchis.counterparties`), so the household's own BBVA
    account names/CUITs are simply classified ``internal:Assets:TransferenciaEnTransito``
    like any other counterparty, and an unrecognized one always surfaces for
    review -- never defaulted.
* **The natural key.** ``<account>:<date>:<amount>:<balance>`` from
  :func:`expensuchis.importers.bbva.movement_keys` is recorded as the entry's
  ``key`` metadata, which the pipeline deduplicates on.
* **The anchor date.** The statement never prints its year (T-07 owner decision
  6); ``extract`` reads the source PDF's ``CreationDate`` metadata
  (:func:`expensuchis.importers.pdf.read_creation_date`) and passes it to the
  parser as ``anchor``, unless an explicit ``year`` override was given to the
  importer at construction, in which case the metadata is never read at all --
  the override exists precisely for a re-downloaded or otherwise
  metadata-stale file the anchor could not help with anyway.

The importer follows the pipeline contract: a failure raises, it never returns
partial entries. ``identify`` never raises and never prints; the PDF engine and
the counterparty map are both created lazily, so constructing the importer
(and therefore :func:`expensuchis.importers.get_importers`) needs neither a
ledger directory nor a statement.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from beancount.core import data
from beangulp import Importer

from ..counterparties import CounterpartyMap
from .bbva import (
    AccountBlock,
    ExtractoConsolidado,
    ExtractoConsolidadoParseError,
    Movement,
    MovementKind,
    is_bbva_extracto,
    movement_keys,
    parse_extracto_consolidado,
)
from .pdf import read_creation_date, read_pdf

__all__ = [
    "MASTERCARD_ACCOUNT",
    "SOURCE",
    "VISA_ACCOUNT",
    "AccountMappingError",
    "BBVAImporter",
    "CounterpartyClassificationError",
    "SourceAccountError",
    "StatementReadError",
    "build_entries",
    "derive_person",
]

#: The display identity: the ledger accounts, the counterparty map's ``source``
#: column, the importer's ``name`` and every fixed-destination payee. Deliberately
#: not the same string as the statement folder -- see the module docstring.
SOURCE = "BBVA"

#: The literal per-person statement folder name on disk (recon 2026-09-27:
#: ``statements/Bbva/``), distinct from :data:`SOURCE`.
_PATH_SOURCE_SEGMENT = "Bbva"

#: Sub-account (kind, currency) -> ledger account template. T-07b owner decision
#: 2026-09-27: "kind + currency, with no private configuration." Any pair not in
#: this table refuses through :class:`AccountMappingError`.
_ACCOUNT_TEMPLATES: dict[tuple[str, str], str] = {
    ("cc", "$"): "Assets:BBVA:{person}:CuentaCorriente",
    ("ca", "$"): "Assets:BBVA:{person}:Caja",
    ("ca", "u$s"): "Assets:BBVA:{person}:CajaUSD",
    ("ca", "eur"): "Assets:BBVA:{person}:CajaEUR",
}

#: Statement currency marker -> beancount commodity. A block's movements post
#: in its own commodity, never a hardcoded ARS: the parser refuses a non-``$``
#: block with movement today, and this keeps a relaxed rule from booking
#: dollars or euros as pesos.
_COMMODITIES: dict[str, str] = {"$": "ARS", "u$s": "USD", "eur": "EUR"}

#: The card liabilities a settlement row posts to. Also reused, unchanged, by
#: :mod:`expensuchis.importers.bbva_card_importer` for a purchase's ARS leg
#: (T-07e): the same liability a settlement here reduces is the one a card
#: purchase there grows, so one pair of templates serves both importers.
VISA_ACCOUNT = "Liabilities:BBVA:{person}:Visa"
MASTERCARD_ACCOUNT = "Liabilities:BBVA:{person}:Mastercard"

#: Where a cash withdrawal posts: the tree's own coarse cash-spending category.
CASH_WITHDRAWAL_ACCOUNT = "Expenses:Efectivo"

#: Coarse income accounts, one per fixed-destination kind.
SALARY_INCOME_ACCOUNT = "Income:{person}:Sueldo"
INTEREST_INCOME_ACCOUNT = "Income:{person}:Intereses"

#: The prefix stripped from a received transfer's concept to get its map key.
#: See the module docstring, "A received transfer's map key is a name".
_TRANSFER_IN_PREFIX = "transferencia inmediata"

#: A placeholder anchor for the ``year`` override path. ``parse_extracto_consolidado``
#: never reads ``anchor`` when ``year`` is given (see its own ``_resolve_close_date``),
#: so any date works; a fixed constant, rather than "today", makes that plainly
#: visible instead of looking like a real clock read.
_UNUSED_ANCHOR = dt.date(1970, 1, 1)

#: A CUIT/CUIL is an 11-digit run. A sent transfer's map key *is* one (see the
#: module docstring); it must never reach a refusal message unredacted, because
#: the CLI prints that message and it can end up in an agent's transcript.
_CUIT_RUN_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")

_PATH_SEPARATORS = re.compile(r"[\\/]")

#: The one refusal message for a path that does not declare its account. It
#: names the expected structure and the account convention, and deliberately
#: carries no path, filename or statement text.
_SOURCE_ACCOUNT_MESSAGE = (
    "cannot derive the BBVA account from the statement path: expected "
    "'.../Bbva/<person>/<file>.pdf', which maps to the person's "
    "'Assets:BBVA:<person>:*' accounts by sub-account kind and currency. The "
    "<person> component must be a single directory name that is not empty, not "
    "'.' or '..', and contains no ':' and no whitespace."
)


class SourceAccountError(ValueError):
    """The statement path does not declare whose BBVA account it is.

    The message names the expected layout and the account convention only; it
    never echoes the offending path, so a diagnostic cannot leak a statement
    location.
    """


class AccountMappingError(ValueError):
    """A sub-account block cannot be mapped to a ledger account.

    Either its (kind, currency) pair is outside the known routing table, or
    the statement holds two blocks sharing one pair (T-07b owner decision:
    refuse rather than merge them). The message names only the offending
    kind/currency tokens -- a closed, non-sensitive vocabulary -- never the
    block's real account number.
    """


class CounterpartyClassificationError(ValueError):
    """A debit-card purchase or transfer names a counterparty absent from the map.

    The message lists each unique unclassified identity with the context a human
    needs to classify it and the ``counterparties.tsv`` row to append. A sent
    transfer's identity is its recipient CUIT, which the message always shows
    redacted -- see the module docstring.
    """


class StatementReadError(ValueError):
    """The statement text or its ``CreationDate`` metadata could not be read.

    A reader failure (a missing file, a corrupt PDF, an engine error) carries raw
    exception text that can include the real path -- and a statement filename can
    name its holder. ``extract`` wraps that failure in this exception with the
    exception **class only**, never the path or the reader's message, because
    ``pipeline.extract`` re-wraps the message and the CLI prints it, so it can end
    up in a remote agent's context. Also raised, with a fixed and already-safe
    message, when the metadata is present but empty or unparseable.
    """


def derive_person(filepath: str | Path) -> str:
    """Return the person token from ``<...>/Bbva/<person>/<file>``.

    The layout is checked structurally: a component named exactly ``Bbva``
    (the literal folder on disk; see the module docstring for why this differs
    from :data:`SOURCE`), then exactly one directory (the person), then the
    file. The person token must be a single non-empty directory name, not
    ``.`` or ``..``, with no ``:`` and no whitespace.

    Raises:
        SourceAccountError: when no single component satisfies the layout or
            the person token is invalid. The message never echoes the path.
    """
    parts = _PATH_SEPARATORS.split(str(filepath))
    if not parts or not parts[-1]:
        raise SourceAccountError(_SOURCE_ACCOUNT_MESSAGE)
    candidates = [
        index
        for index, part in enumerate(parts)
        if part == _PATH_SOURCE_SEGMENT and index + 2 == len(parts) - 1
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


def _account_mapping_message(
    unknown: Sequence[tuple[str, str]], duplicated: Sequence[tuple[str, str]]
) -> str:
    parts: list[str] = []
    if unknown:
        pairs = ", ".join(f"{kind}/{currency}" for kind, currency in unknown)
        parts.append(f"unknown sub-account kind/currency pair(s): {pairs}.")
    if duplicated:
        pairs = ", ".join(f"{kind}/{currency}" for kind, currency in duplicated)
        parts.append(
            f"the statement holds two or more sub-account blocks sharing one "
            f"kind/currency pair: {pairs}; each must be its own ledger account, "
            f"never merged."
        )
    return " ".join(parts)


def _resolve_account_routes(
    blocks: Sequence[AccountBlock], person: str
) -> dict[str, tuple[str, str]]:
    """Map every block's raw account number to its (ledger account, commodity), or refuse.

    Raises:
        AccountMappingError: at least one block's (kind, currency) pair is
            unknown, or the statement holds two blocks sharing one pair. Every
            offending pair is collected before the refusal, so one run tells
            the whole story.
    """
    pairs = [(block.kind, block.currency) for block in blocks]
    counts = Counter(pairs)
    unknown = sorted({pair for pair in counts if pair not in _ACCOUNT_TEMPLATES})
    duplicated = sorted(
        {pair for pair, count in counts.items() if count > 1 and pair not in unknown}
    )
    if unknown or duplicated:
        raise AccountMappingError(_account_mapping_message(unknown, duplicated))
    return {
        block.account: (
            _ACCOUNT_TEMPLATES[(block.kind, block.currency)].format(person=person),
            _COMMODITIES[block.currency],
        )
        for block in blocks
    }


def _destination_account(destination: str) -> str:
    """Return the ledger account named by a resolved counterparty-map destination.

    A map value is ``internal:<account>`` or ``expense:<category>``. The marker
    says whether the counterparty is inside this ledger; the part after it is
    the account, used verbatim -- splitting on every ``:`` would truncate
    ``internal:Assets:BBVA:P2:Caja`` to ``Assets``.
    """
    _marker, _separator, account = destination.partition(":")
    return account


def _fixed_destination(kind: MovementKind, person: str) -> str:
    """Return the counterpart account for a kind that is not map-resolved."""
    if kind is MovementKind.VISA_SETTLEMENT:
        return VISA_ACCOUNT.format(person=person)
    if kind is MovementKind.MASTERCARD_SETTLEMENT:
        return MASTERCARD_ACCOUNT.format(person=person)
    if kind is MovementKind.CASH_WITHDRAWAL:
        return CASH_WITHDRAWAL_ACCOUNT
    if kind is MovementKind.SALARY:
        return SALARY_INCOME_ACCOUNT.format(person=person)
    if kind is MovementKind.INTEREST:
        return INTEREST_INCOME_ACCOUNT.format(person=person)
    # DEBIT_CARD_PURCHASE, TRANSFER_OUT and TRANSFER_IN are map-resolved
    # before this is reached.
    raise AssertionError(f"no fixed destination for kind {kind!r}")  # pragma: no cover


def _transfer_in_raw_name(concept: str) -> str:
    """Return the counterparty name left after stripping the verb.

    ``concept`` is already folded (lowercase, unaccented) by the parser. When
    nothing is left, the caller raises rather than resolving an empty key --
    see the module docstring.
    """
    if concept.startswith(_TRANSFER_IN_PREFIX):
        return concept[len(_TRANSFER_IN_PREFIX) :].strip()
    return concept.strip()  # pragma: no cover - classify_concept guarantees the prefix


def _raw_name(movement: Movement) -> str:
    """Return the counterparty-map key for a map-resolved movement kind."""
    if movement.kind is MovementKind.DEBIT_CARD_PURCHASE:
        return movement.merchant
    if movement.kind is MovementKind.TRANSFER_OUT:
        return movement.recipient_cuit
    return _transfer_in_raw_name(movement.concept)


def _redact_cuit(text: str) -> str:
    """Replace any CUIT/CUIL-shaped 11-digit run in ``text`` with a placeholder.

    The refusal message is human-facing and the CLI prints it, so it must not
    carry a sent transfer's recipient CUIT into a transcript.
    """
    return _CUIT_RUN_RE.sub("<cuit>", text)


def _unclassified_message(
    unclassified: dict[str, tuple[Movement, str]], *, reveal: bool = False
) -> str:
    """Build the pinned refusal message for the unique unclassified counterparties.

    The count is of **unique counterparties**, not movements, and the first
    movement that named each counterparty supplies the displayed date, amount
    and concept. Both the displayed concept and the displayed raw name have
    any CUIT-shaped run redacted; when the raw name *is* a redacted CUIT (a
    sent transfer), the append suggestion cannot show it. This refusal fires
    from inside the importer's own ``extract``, before ``pipeline.extract``
    ever writes a staging directory (a failing importer raises before any
    entries are staged), so there is no ledger-side file yet that could carry
    the real value either: the message instead points the reader at the
    original statement's own sent-transfers section, matched by date and
    amount, which is the only place the digits already exist.

    ``reveal=True`` is the opt-in escape hatch, mirroring
    :mod:`expensuchis.leakguard`'s own ``--reveal`` (T-01d): it skips
    :func:`_redact_cuit` entirely for the displayed concept and raw name, and
    the append suggestion shows the real ``raw_name`` instead of the
    ``<recipient CUIT>`` placeholder -- there is nothing left to hide once the
    caller has opted in. It exists for the owner reading a terminal directly,
    never for output an agent or a remote model might read; like leakguard's
    own ``--reveal``, that boundary is enforced by the operator's own
    discipline, not by anything this function or the CLI checks -- a plain
    boolean flag, no TTY probe, no confirmation gate -- which is why the
    default stays redacted and this parameter is keyword-only rather than
    something a caller could reach accidentally.

    Whether a raw name *is* a CUIT is a structural fact, independent of
    ``reveal``: it decides which hint this function prints, while ``reveal``
    only decides whether the printed value is redacted. The two must stay
    separate checks -- collapsing them (e.g. computing the displayed name
    first and then comparing it to ``raw_name`` to decide the hint) makes the
    comparison trivially true whenever ``reveal=True``, silently dropping the
    CUIT hint for every revealed sent transfer regardless of whether it was
    ever redacted to begin with.
    """
    lines = [f"{len(unclassified)} counterparties are not classified."]
    for raw_name, (movement, commodity) in unclassified.items():
        is_cuit_identity = _redact_cuit(raw_name) != raw_name
        displayed_concept = movement.concept if reveal else _redact_cuit(movement.concept)
        displayed_name = raw_name if reveal else _redact_cuit(raw_name)
        lines.append(
            f"  {movement.date.isoformat()}  {movement.amount:,.2f} {commodity}  "
            f'"{displayed_concept}"  "{displayed_name}"'
        )
        if is_cuit_identity and not reveal:
            lines.append(
                "    this counterparty's identity is a redacted recipient CUIT, never "
                "shown here or written to any staged file at this point (extract "
                "refuses before staging anything). Read it from the original "
                "statement's own sent-transfers section, matched by this date and "
                "amount, before appending to counterparties.tsv:"
            )
            lines.append(f"      {SOURCE}\t<recipient CUIT>\texpense:<category>")
        else:
            lines.append("    append to counterparties.tsv:")
            lines.append(f"      {SOURCE}\t{raw_name}\texpense:<category>")
    return "\n".join(lines)


def _entry(
    movement: Movement,
    cash_account: str,
    commodity: str,
    counterpart: str,
    key: str,
    payee: str,
) -> data.Transaction:
    meta = {"key": key}
    postings = [
        data.Posting(cash_account, data.Amount(movement.amount, commodity), None, None, None, None),
        data.Posting(counterpart, data.Amount(-movement.amount, commodity), None, None, None, None),
    ]
    return data.Transaction(
        meta,
        movement.date,
        "*",
        payee,
        movement.concept,
        frozenset(),
        frozenset(),
        postings,
    )


def build_entries(
    extracto: ExtractoConsolidado,
    person: str,
    counterparty_map: CounterpartyMap,
    *,
    reveal: bool = False,
) -> list[data.Transaction]:
    """Turn a reconciled statement into beancount transactions.

    Pure over its arguments: it touches no filesystem and no environment, so
    it is the unit-test seam for account routing, posting logic and the
    counterparty refusal.

    Raises:
        AccountMappingError: at least one sub-account block cannot be routed
            to a ledger account. Checked before any movement is processed.
        ExtractoConsolidadoParseError: a received transfer has no counterparty
            name left after stripping its verb.
        CounterpartyClassificationError: at least one debit-card purchase,
            sent transfer or received transfer names a counterparty absent
            from the map. Every unknown counterparty is collected before the
            refusal, so one run tells the whole story. ``reveal`` is threaded
            straight to :func:`_unclassified_message`, unredacted only when the
            caller opts in.
    """
    account_for = _resolve_account_routes(extracto.blocks, person)
    keys = movement_keys(extracto.movements)
    entries: list[data.Transaction] = []
    unclassified: dict[str, tuple[Movement, str]] = {}
    for movement, key in zip(extracto.movements, keys):
        cash_account, commodity = account_for[movement.account]
        kind = movement.kind
        if kind in (
            MovementKind.DEBIT_CARD_PURCHASE,
            MovementKind.TRANSFER_OUT,
            MovementKind.TRANSFER_IN,
        ):
            raw_name = _raw_name(movement)
            if not raw_name:
                raise ExtractoConsolidadoParseError(
                    f"line {movement.line}: a received transfer has no counterparty "
                    f"name left after stripping its verb"
                )
            destination = counterparty_map.resolve(SOURCE, raw_name)
            if destination is None:
                unclassified.setdefault(raw_name, (movement, commodity))
                continue
            counterpart = _destination_account(destination)
            payee = raw_name
        else:
            counterpart = _fixed_destination(kind, person)
            payee = SOURCE
        entries.append(_entry(movement, cash_account, commodity, counterpart, key, payee))

    if unclassified:
        raise CounterpartyClassificationError(_unclassified_message(unclassified, reveal=reveal))
    return entries


class BBVAImporter(Importer):
    """Import one BBVA ``Extracto consolidado`` PDF per person.

    ``counterparty_map``, ``text_reader`` and ``date_reader`` are seams. The
    map is created lazily on the first ``extract`` so that ``identify`` and
    ``get_importers()`` need no ledger directory or environment variable;
    ``text_reader`` and ``date_reader`` replace PDF extraction in tests, and
    default to :func:`expensuchis.importers.pdf.read_pdf` and
    :func:`expensuchis.importers.pdf.read_creation_date`. ``year`` is the
    explicit anchor-year override the parser accepts (T-07 owner decision 6):
    when given, ``date_reader`` is never called at all. ``reveal`` is threaded
    straight to :func:`build_entries` and, from there, to
    :func:`_unclassified_message`: it opts a ``CounterpartyClassificationError``
    refusal into showing a sent transfer's real recipient CUIT instead of the
    default ``<cuit>`` placeholder, mirroring
    :mod:`expensuchis.leakguard`'s own ``--reveal`` -- owner-only, at a
    terminal, never where an agent or a remote model reads the output. As
    with leakguard's flag, that is a convention the operator honours, not
    something this class or the CLI enforces: a plain boolean, no TTY check.
    """

    def __init__(
        self,
        counterparty_map: CounterpartyMap | None = None,
        text_reader: Callable[[str], str] | None = None,
        date_reader: Callable[[str], dt.date | None] | None = None,
        year: int | None = None,
        reveal: bool = False,
    ) -> None:
        self._counterparty_map = counterparty_map
        self._text_reader = text_reader
        self._date_reader = date_reader
        self._year = year
        self._reveal = reveal

    @property
    def name(self) -> str:
        """The importer identity, and the map's ``source`` column."""
        return SOURCE

    def identify(self, filepath: str) -> bool:
        """Claim the file when its extracted text carries the statement's markers.

        Delegates to :func:`~expensuchis.importers.bbva.is_bbva_extracto`, the
        parser's own structural marker check, so identification and parsing
        can never disagree on what counts as the title. Never raises and
        never prints: any extraction failure is a non-match.
        """
        try:
            text = self._read_text(filepath)
        except Exception:  # noqa: BLE001 - an unreadable file is simply not ours
            return False
        return is_bbva_extracto(text)

    def account(self, filepath: str) -> str:
        """Return the archival account: the person's checking account.

        Required by beangulp's ``Importer`` ABC. A consolidated statement can
        carry more than one sub-account, so unlike every other importer here
        there is no single account the whole file is "about"; the checking
        account is picked as a fixed archival label -- this project's own
        pipeline never calls this method, only beangulp's own file-organizing
        feature would.
        """
        return _ACCOUNT_TEMPLATES[("cc", "$")].format(person=derive_person(filepath))

    def sort(self, entries, reverse: bool = False) -> None:
        """Sort in place by date, then by the natural key.

        The base ``Importer.sort`` delegates to ``data.entry_sortkey``, which
        reads ``meta["lineno"]``; entries from this importer carry only the
        natural ``key`` by design, so the base implementation would raise
        ``KeyError``. Document order is already chronological, so a
        date-then-key sort is total and deterministic, which is all the
        pipeline's month grouping needs.
        """
        entries.sort(key=lambda entry: (entry.date, entry.meta.get("key", "")), reverse=reverse)

    def extract(self, filepath: str, existing) -> list[data.Transaction]:
        """Parse and reconcile the statement, then build its transactions.

        The person comes from the path and the meaning from the parser;
        :class:`~expensuchis.importers.bbva.ReconciliationError` and
        :class:`~expensuchis.importers.bbva.ExtractoConsolidadoParseError`
        propagate unchanged because their messages are already masked. A
        reader failure (text or ``CreationDate``) is wrapped in
        :class:`StatementReadError` -- the raw message can carry the real
        path or a holder's name, and the CLI prints it. Nothing partial is
        ever returned.
        """
        person = derive_person(filepath)
        try:
            text = self._read_text(filepath)
        except Exception as exc:  # noqa: BLE001 - the class is the only safe detail
            # ``from None`` on purpose: the cause's message is exactly the leak.
            raise StatementReadError(
                f"cannot read the statement text: {type(exc).__name__}"
            ) from None

        if self._year is not None:
            anchor = _UNUSED_ANCHOR
        else:
            try:
                anchor = self._read_creation_date(filepath)
            except Exception as exc:  # noqa: BLE001 - the class is the only safe detail
                raise StatementReadError(
                    f"cannot read the statement's CreationDate metadata: {type(exc).__name__}"
                ) from None
            if anchor is None:
                raise StatementReadError(
                    "cannot resolve the anchor date: the statement's CreationDate "
                    "metadata is missing or unparseable; pass an explicit year "
                    "override instead"
                )

        extracto = parse_extracto_consolidado(text, anchor=anchor, year=self._year)
        counterparty_map = (
            self._counterparty_map if self._counterparty_map is not None else CounterpartyMap()
        )
        return build_entries(extracto, person, counterparty_map, reveal=self._reveal)

    def _read_text(self, filepath: str) -> str:
        if self._text_reader is not None:
            return self._text_reader(str(filepath))
        return read_pdf(filepath)[0]

    def _read_creation_date(self, filepath: str) -> dt.date | None:
        if self._date_reader is not None:
            return self._date_reader(str(filepath))
        return read_creation_date(filepath)
