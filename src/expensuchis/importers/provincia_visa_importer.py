"""The Banco Provincia card importer: wiring the ``Liquidación Visa`` into the ledger.

The parser (:mod:`expensuchis.importers.provincia_visa`) owns the card statement's
meaning and its reconciliation gate; this module owns the ledger's side of it, in
the same shape as the account extracto importer
(:mod:`expensuchis.importers.provincia_importer`): a pure ``build_entries`` seam,
a lazily created counterparty map, a ``text_reader`` seam for tests, and refusals
that never echo statement content.

* **SOURCE is deliberately the account extracto's source.** The counterparty map
  is keyed by ``(source, raw_name)``, and the same merchant appears in both
  products, so one ``counterparties.tsv`` row classifies a merchant everywhere
  the household spends under the name the map knows. ``SOURCE`` is used in the
  map key, in the plan key and in the ``counterparties.tsv`` suggestion line.
  The visible importer identity is a different animal: ``NAME`` (``ProvinciaVisa``)
  names **the product** in pipeline refusals and CLI output, where two importers
  both called ``Provincia`` would be ambiguous, while ``SOURCE`` (``Provincia``)
  names **the bank** — and the bank is what the map is keyed on. One name for the
  ledger's books, one for the human reading them.
* **Where each kind posts.** The card is a liability, so the signs are inverted
  relative to the account extracto: a consumption grows the liability.

  =====================================  =======================================
  Row kind                               Postings
  =====================================  =======================================
  charge (ARS)                           ``Expenses:<category>`` +ARS,
                                         ``Liabilities:Provincia:<person>:Visa`` −ARS
  charge (USD-only)                      ``Expenses:<category>`` +USD,
                                         ``Liabilities:Provincia:<person>:VisaUSD`` −USD, no ``@`` price
  installment row (``C.NN/NN``)          one entry for the **whole plan** (below)
  post-total surcharge (``SELLOS``)      ``Expenses:Impuestos:Sellos`` +,
                                         the card liability of that currency −
  post-total surcharge (``PERCEPCION``)  ``Expenses:Impuestos:Percepciones`` +,
                                         the card liability of that currency −
  payment row (``SU PAGO EN PESOS``)     **nothing** (below)
  =====================================  =======================================

  - **A USD-only charge carries no price.** Model case (b) in
    ``docs/accounting-model.md``: the card holds a USD balance, the statement
    shows no conversion, and deriving a rate from a market price is exactly what
    the model forbids — the two legs are the expense and the USD liability, and
    the conversion, if ever recorded, is a separate decision.
  - **An installment row is realized, not accrued row by row.** The model's
    convention: one transaction dated at the purchase date for the per-installment
    amount × the plan total, with the plan captured as metadata — ``installments``
    (the integer total, as a ``Decimal``: the contract calls it an integer and a
    ``Decimal`` prints as one, but beancount's printer rejects a bare ``int`` in
    metadata, and ``pipeline.extract`` renders every kept entry), ``first_due`
    (``"YYYY-MM"``, the statement's due month minus ``installment_number - 1``
    months, so the first installment's month, even across a year boundary) and
    ``installment_amount`` (an :class:`beancount.core.amount.Amount`, never a
    bare number). Its ``key`` is a **plan** key —
    ``provincia-visa:<person>:<purchase date>:<total>:<currency>:<canonical identity>``
    — deliberately independent of the row's comprobante and installment number, so
    the same plan repeated on a later statement (``C.04/12`` of the same purchase)
    yields the same key and the pipeline's "key already in the ledger" rule drops it
    instead of charging the plan twice. The **per-installment amount is
    deliberately not a field**: it is exactly what a later statement may change (a
    cent of rounding on one cuota would silently split the key and post the plan
    twice). The trade-off: two genuinely different plans that share person,
    purchase date, total, currency and canonical merchant collapse to one key —
    inside one batch the pipeline refuses loudly (``KEY_DUPLICATE``); on a later
    statement the second plan is dropped silently. The parsed data carries no
    stable plan identifier (the per-row comprobante is distinct on every row of
    the real document, and no plan or coupon id is printed), which is why the key
    is built from exactly these five fields.
  - **Surcharges resolve with no map lookup.** ``SELLOS`` and ``PERCEPCION`` are
    taxes on the card's own operation, not spending at a counterparty: the shape
    classification in the parser is the whole decision, and the account is fixed.
    The key carries the person (as the plan key does) and repeats the ``#N``
    discipline: stable across repeated imports of the same document, unique
    inside a batch.
  - **A payment row emits nothing.** ``SU PAGO EN PESOS`` is the card payment:
    not a consumption, not an expense. Its cash movement belongs to the account
    extracto, where ``pago visa`` already posts the settlement against the same
    ``Liabilities:Provincia:<person>:Visa`` liability — an entry here would
    reduce the liability twice. The parser reconciles the payment block anyway
    (the sums depend on it being understood), and the importer simply never
    posts it: nothing partial is exposed in its place.
* **The merchant identity.** The map key is derived from the verbatim
  description by dropping the processor prefix (``PAGOAPP*``, ``SHOPONLINE*``,
  ``BUSCADOR *`` and siblings — the identity is the segment after the last
  ``*``) and any pure-digit, date-like, time-like or **CUIT-shaped** token (on a
  card description the run is the merchant's tax id — per-row noise, never the
  name), then whitespace-collapsing **with casing and punctuation kept**. This
  printed identity is the map key and the payee, in the extracto's own style: the
  map is keyed on ``(source, raw_name)`` and the extracto does not canonicalize,
  so one ``counterparties.tsv`` row classifies a merchant for **both** Provincia
  products. The casefolded, punctuation-free **canonical form lives only inside
  the plan key**, where month-to-month text drift actually matters — the split is
  deliberate: reusing one map row across products beats case-insensitive map
  keys. A row whose identity would be an identifier rather than a merchant name
  (every token dropped, or an 11-digit run glued inside name characters) refuses
  loudly instead of falling back to the raw description — the fallback would put
  the CUIT back into the key and print an append row the lookup can never match.
* **The refusal.** An identity absent from the map stops the whole import with
  :class:`~expensuchis.importers.provincia_importer.CounterpartyClassificationError`,
  collecting every unique identity first so one run tells the whole story. The
  message lists, per identity, the date, the amount, the verbatim description and
  the identity — **every interpolated field passed through the redactor** (the
  dotted, hyphenated and spaced CUIT shapes included), because the CLI prints it.
  The printed identity is already identifier-free by construction, so the printed
  ``append to counterparties.tsv:`` row is **exactly the lookup key**: pasting it
  classifies the merchant on the next run, for both Provincia products.

The importer follows the pipeline contract: a failure raises, it never returns
partial entries. ``identify`` never raises and never prints; the PDF engine and
the counterparty map are both created lazily, so constructing the importer needs
neither a ledger directory nor a statement.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from decimal import Decimal

from beancount.core import data
from beangulp import Importer

from ..counterparties import CounterpartyMap
from . import provincia_visa
from .bbva_card import is_bbva_card_liquidacion
from .pdf import read_pdf
from .provincia_importer import (
    CARD_ACCOUNT,
    SOURCE,
    CounterpartyClassificationError,
    SourceAccountError,
    StatementReadError,
    derive_person,
)
from .provincia_visa import (
    Charge,
    Liquidacion,
    LiquidacionParseError,
    Surcharge,
    SurchargeKind,
    charge_keys,
)

__all__ = [
    "CARD_ACCOUNT",
    "CARD_USD_ACCOUNT",
    "MARKER",
    "NAME",
    "PERCEPCION_ACCOUNT",
    "SELLOS_ACCOUNT",
    "SOURCE",
    "CounterpartyClassificationError",
    "ProvinciaVisaImporter",
    "SourceAccountError",
    "StatementReadError",
    "build_entries",
    "derive_person",
]

#: The visible importer identity: the product, not the bank. ``pipeline`` puts it
#: in refusal messages and the CLI names it as the importer that claimed a file,
#: so it must distinguish this importer from the account extracto's. The
#: counterparty map stays keyed on ``SOURCE`` — the bank both products belong to.
NAME = "ProvinciaVisa"

#: The marker the card statement carries in its title. Matched after folding
#: (casefold + accent stripping) because the title casing varies between
#: renderings, so ``Liquidación`` and ``LIQUIDACION`` are one marker. It appears
#: in the card statement and not in the account extracto (``Extracto de Cuenta``),
#: so the two Provincia importers never claim each other's files.
MARKER = "Liquidacion"

#: The USD liability of the same card. The model opens ``Visa`` and ``VisaUSD``
#: per entity: a card holding two currencies settles as two accounts.
CARD_USD_ACCOUNT = "Liabilities:Provincia:{person}:VisaUSD"

#: Where the stamp tax posts: a tax on the card's own operation, fixed by shape.
SELLOS_ACCOUNT = "Expenses:Impuestos:Sellos"

#: Where a regime perception posts, for the same reason.
PERCEPCION_ACCOUNT = "Expenses:Impuestos:Percepciones"

#: Per-row noise tokens in a card description: a pure-digit run, a date-like
#: ``dd/mm``/``dd/mm/yy`` or a time-like ``hh:mm`` run. They differ from row to
#: row for the same merchant, so they must not enter the map key.
_DIGIT_TOKEN_RE = re.compile(r"\d+")
_DATE_TOKEN_RE = re.compile(r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?")
_TIME_TOKEN_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
#: A CUIT/CUIL: an 11-digit run, or the ``20-12345678-9`` shape with any mix of
#: dot, hyphen or space separators (the hyphenated form covers the plain 11-digit
#: run, which is kept as its own alternative to document the original rule; the
#: dotted and space-separated forms are what the description field shows). It
#: must never reach a log line: the refusal message is printed by the CLI and can
#: end up in an agent's transcript.
_CUIT_RUN_RE = re.compile(r"(?<!\d)(?:\d{2}[ .-]?\d{8}[ .-]?\d|\d{11})(?!\d)")
#: The same shapes as a **single token** of a description: these are dropped from
#: the identity exactly where the noise tokens are dropped, because on a card
#: description a CUIT is the merchant's tax id — per-row noise, never the name.
_CUIT_TOKEN_RE = re.compile(r"\d{2}[ .-]\d{8}[ .-]\d")
#: The raw-description sweep: the same separator-flexible shapes, matched **before
#: any whitespace splitting** — a run with space separators straddles two tokens
#: (``20.12345678 9``). Word characters flanking the run keep it for the guard.
_CUIT_SWEEP_RE = re.compile(r"(?<!\w)(?:\d{2}[ .-]?\d{8}[ .-]?\d|\d{11})(?!\w)")
#: A run of seven or more digits: what a printed identity must never carry once
#: separators are ignored — a partially consumed CUIT is not CUIT-shaped any more.
_SEVEN_DIGIT_RUN_RE = re.compile(r"\d{7,}")
#: Punctuation inside an identity: replaced by a space before collapsing, so a
#: dotted or hyphenated merchant name canonicalizes without merging neighbouring
#: words (``S.A.`` becomes ``s a``, never ``sa``).
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _is_noise_token(token: str) -> bool:
    """Whether a token is per-row noise: pure digits, a date, a time or a CUIT.

    A CUIT-shaped token belongs here on purpose: on a card description the run is
    the merchant's tax id, printed on some rows and not on others, so letting it
    into the identity would split one merchant into unclassifiable fragments — and
    put an identifier into a message that must stay transcribable.
    """
    return bool(
        _DIGIT_TOKEN_RE.fullmatch(token)
        or _DATE_TOKEN_RE.fullmatch(token)
        or _TIME_TOKEN_RE.fullmatch(token)
        or _CUIT_TOKEN_RE.fullmatch(token)
    )


def _refuse_identifier(page: int, line: int) -> None:
    """Refuse a row whose identity would key or print an identifier; masked."""
    raise LiquidacionParseError(
        f"page {page} line {line}: the charge row carries an identifier and "
        f"no merchant name; refusing the document — accepting such a row "
        f"needs a deliberate code decision"
    )


def _canonical_identity(text: str) -> str:
    """Return the identity's canonical form: casefolded, punctuation-free, collapsed.

    The canonical form lives **only inside the plan key**: it is where
    month-to-month text drift actually matters (the same plan repeated on a later
    statement must key the same), while the map key and the payee keep the printed
    identity — consistent with the extracto's own style, so one
    ``counterparties.tsv`` row serves both Provincia products (the map is keyed on
    ``(source, raw_name)`` and the extracto does not canonicalize).
    """
    return " ".join(_PUNCTUATION_RE.sub(" ", text.casefold()).split())


def _merchant_identity(description: str, page: int, line: int) -> str:
    """Return the charge's **printed** counterparty identity.

    The processor prefix (``PAGOAPP*``, ``SHOPONLINE*``, ``BUSCADOR *`` and
    siblings) is dropped by taking the segment after the last ``*``; then any
    pure-digit, date-like, time-like or CUIT-shaped token is dropped, and what
    remains is whitespace-collapsed with its casing and punctuation kept. This is
    the string the map is keyed on, the payee, and the identity the refusal
    prints — consistent with the account extracto's own identity style, so a row
    classified once in ``counterparties.tsv`` serves **both** Provincia products.
    (The casefolded, punctuation-free canonical form lives only inside the plan
    key, where month-to-month text drift actually matters.)

    Dropping the CUIT-shaped tokens is correct at the root, not cosmetic: the run
    is the merchant's tax id (per-row noise on a card description), so an
    identifier must never enter a key. A row whose identity would be an
    identifier rather than a merchant name — every token dropped, or an 11-digit
    run glued inside name characters — **refuses loudly** with a masked message
    instead of falling back to the raw description: the fallback would put the
    CUIT back into the key and print an append row the lookup can never match.
    The identity therefore never carries a run of seven or more digits once
    separators are ignored — a property of the string that is actually printed —
    which is what makes the printed ``append to counterparties.tsv:``
    row exactly the lookup key — transcribable, the whole point of the message.

    Raises:
        LiquidacionParseError: when no merchant name remains after the drops, or
            when the remaining identity still carries a CUIT-shaped run. The
            messages carry page and line numbers only.
    """
    candidate = description.rsplit("*", 1)[1] if "*" in description else description
    # Root sweep, before any whitespace splitting: a run with space separators
    # straddles two tokens and the token-level check below never sees it whole.
    swept = _CUIT_SWEEP_RE.sub(" ", candidate)
    tokens = [token for token in swept.split() if not _is_noise_token(token)]
    printed = " ".join(tokens)
    if not printed:
        if candidate.strip():
            # Every token was noise or an identifier: there is a row here but no
            # name to classify, and the raw-description fallback would put the
            # identifier back into the key.
            _refuse_identifier(page, line)
        raise LiquidacionParseError(
            f"page {page} line {line}: the charge row carries no merchant identity "
            f"after normalization; refusing the document"
        )
    if _SEVEN_DIGIT_RUN_RE.search(re.sub(r"[ .-]", "", printed)):
        # Defence in depth, on the string actually printed and keyed: a partial
        # CUIT is not CUIT-shaped any more, so redaction equality cannot see it.
        _refuse_identifier(page, line)
    return printed


def _redact_cuit(text: str) -> str:
    """Replace any CUIT/CUIL-shaped 11-digit run in ``text`` with a placeholder.

    The refusal message is human-facing and the CLI prints it, so it must not
    carry the counterparty's CUIT into a transcript. The merchant identity stays
    readable; only the identifier is redacted.
    """
    return _CUIT_RUN_RE.sub("<cuit>", text)


def _category_account(destination: str) -> str:
    """Return the ledger account named by a resolved counterparty-map destination.

    A map value is ``internal:<account>`` or ``expense:<category>``; the part
    after the marker is the account, used verbatim. Only the marker is removed —
    splitting on every ``:`` would truncate ``internal:Assets:Provincia:P2:Caja``
    to ``Assets``. This mirrors the account extracto importer's own helper: the
    logic is duplicated rather than imported because private names stay private.
    """
    _marker, _separator, account = destination.partition(":")
    return account


def _card_account(person: str, currency: str) -> str:
    """Return the card liability account for the row's currency."""
    template = CARD_ACCOUNT if currency == "ARS" else CARD_USD_ACCOUNT
    return template.format(person=person)


def _row_amount(row: Charge | Surcharge) -> tuple[Decimal, str]:
    """Return a row's amount and currency: the ARS amount, or the USD one."""
    if row.amount_ars is not None:
        return row.amount_ars, "ARS"
    return row.amount_usd, "USD"  # type: ignore[return-value]  # one of the two is set


def _first_due(liquidacion: Liquidacion, charge: Charge) -> str:
    """Return the plan's first installment month as ``"YYYY-MM"``.

    The statement's due month minus ``installment_number - 1`` months: an
    installment first seen at ``C.03/12`` records the month its **first**
    installment was due. The subtraction is done in month arithmetic, so it
    crosses a year boundary correctly.
    """
    months_back = charge.installment_number - 1
    absolute = liquidacion.due_year * 12 + (liquidacion.due_month - 1) - months_back
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def _plan_key(charge: Charge, person: str, currency: str, identity: str) -> str:
    """Return the plan key: identical for the same plan seen in a later statement.

    Five fields — ``provincia-visa:<person>:<purchase date>:<total>:<currency>:
    <canonical identity>`` — deliberately independent of the row's comprobante and
    installment number, which are exactly what a later statement's row of the same
    plan changes. The **per-installment amount is deliberately not a field**: it is
    exactly what a later statement may change (a cent of rounding on one cuota
    would silently split the key and post the plan twice).

    The trade-off, stated honestly in both directions. Without the amount, two
    genuinely different plans that share person, purchase date, installment total,
    currency and canonical merchant collapse to one key: inside one batch the
    pipeline refuses loudly (``KEY_DUPLICATE``); on a later statement the second
    plan is dropped silently. The parsed data carries **no stable plan identifier**
    — the per-row comprobante is distinct on every row of the real document, and
    no plan or coupon id is printed — which is why the key is built from exactly
    these five fields and no more.
    """
    return (
        f"provincia-visa:{person}:{charge.when.isoformat()}:"
        f"{charge.installment_total}:{currency}:{identity}"
    )


def _charge_entry(
    liquidacion: Liquidacion, charge: Charge, person: str, category: str, identity: str, key: str
) -> data.Transaction:
    """Build the transaction for one charge row (installment or not)."""
    amount, currency = _row_amount(charge)
    if charge.installment_total is not None:
        posted = amount * charge.installment_total
        meta = {
            "key": key,
            # A Decimal, never a bare int: the ledger contract calls ``installments``
            # an integer and a Decimal prints as one, but beancount's printer accepts
            # only str/Decimal/date/Amount/bool/None in metadata — a bare int makes
            # the pipeline's renderer raise on every kept plan entry.
            "installments": Decimal(charge.installment_total),
            "first_due": _first_due(liquidacion, charge),
            "installment_amount": data.Amount(amount, currency),
        }
    else:
        posted = amount
        meta = {"key": key}
    card = _card_account(person, currency)
    postings = [
        data.Posting(category, data.Amount(posted, currency), None, None, None, None),
        data.Posting(card, data.Amount(-posted, currency), None, None, None, None),
    ]
    return data.Transaction(
        meta, charge.when, "*", identity, charge.description, frozenset(), frozenset(), postings
    )


def _surcharge_key(
    surcharge: Surcharge, person: str, when: dt.date, currency: str, occurrence: int
) -> str:
    """Return the surcharge's key: stable per document, unique inside a batch.

    ``provincia-visa:<person>:<date>:<kind>:<amount>:<currency>`` with a ``#N``
    suffix on genuine repeats, mirroring the plan key's discipline: the person is
    part of the key, so two persons' surcharges that share closing month, kind,
    amount and currency never collapse to one key (the pipeline's ledger dedup
    would silently drop the second person's row). Stable across repeated imports
    of the same document (the pipeline drops them), while two identical rows
    inside one batch stay distinct. The currency is part of the key because the
    pinned format must not collide an ARS row with a USD row of the same kind and
    amount — a collision that a zero-amount SELLOS pair hits in every document.
    """
    amount = surcharge.amount_ars if surcharge.amount_ars is not None else surcharge.amount_usd
    base = (
        f"provincia-visa:{person}:{when.isoformat()}:{surcharge.kind.value}:{amount:.2f}:{currency}"
    )
    return base if occurrence == 1 else f"{base}#{occurrence}"


def _surcharge_entry(
    surcharge: Surcharge, person: str, when: dt.date, key: str
) -> data.Transaction:
    """Build the transaction for one post-total surcharge row."""
    amount, currency = _row_amount(surcharge)
    if surcharge.kind is SurchargeKind.SELLOS:
        category = SELLOS_ACCOUNT
    else:
        category = PERCEPCION_ACCOUNT
    card = _card_account(person, currency)
    postings = [
        data.Posting(category, data.Amount(amount, currency), None, None, None, None),
        data.Posting(card, data.Amount(-amount, currency), None, None, None, None),
    ]
    return data.Transaction(
        {"key": key},
        when,
        "*",
        SOURCE,
        surcharge.label,
        frozenset(),
        frozenset(),
        postings,
    )


def _unclassified_message(unclassified: dict[str, Charge]) -> str:
    """Build the pinned refusal message for the unique unclassified counterparties.

    The count is of **unique counterparties**, not rows, and the first charge that
    named each counterparty supplies the displayed date, amount and description.
    The message is always plural — ``1 counterparties are not classified.`` — the
    same single string form the account extracto pins. Every interpolated field —
    the quoted description **and the quoted identity, in both places** — goes
    through the redactor: the identity is text from the same statement, so a
    CUIT-shaped run inside it (glued to other characters or hyphenated) must not
    reach the CLI's output any more than one in the description.
    """
    lines = [f"{len(unclassified)} counterparties are not classified."]
    for identity, charge in unclassified.items():
        amount, currency = _row_amount(charge)
        lines.append(
            f"  {charge.when.isoformat()}  {amount:,.2f} {currency}  "
            f'"{_redact_cuit(charge.description)}"  "{_redact_cuit(identity)}"'
        )
        lines.append("    append to counterparties.tsv:")
        lines.append(f"      {SOURCE}\t{_redact_cuit(identity)}\texpense:<category>")
    return "\n".join(lines)


def build_entries(
    liquidacion: Liquidacion, person: str, counterparty_map: CounterpartyMap
) -> list[data.Transaction]:
    """Turn a reconciled card statement into beancount transactions.

    Pure over its arguments: it touches no filesystem and no environment, so it is
    the unit-test seam for posting logic and the counterparty refusal.

    Payment rows emit nothing (see the module docstring): they are the card
    settlement, already posted by the account extracto's ``pago visa``.

    Raises:
        LiquidacionParseError: a charge row that needs a counterparty has no
            merchant identity after normalization. The message is masked.
        CounterpartyClassificationError: at least one charge names a counterparty
            absent from the map. Every unknown counterparty is collected before
            the refusal, so one run tells the whole story.
    """
    keys = charge_keys(liquidacion.charges)
    entries: list[data.Transaction] = []
    unclassified: dict[str, Charge] = {}
    for charge, row_key in zip(liquidacion.charges, keys):
        printed = _merchant_identity(charge.description, charge.page, charge.line)
        destination = counterparty_map.resolve(SOURCE, printed)
        if destination is None:
            unclassified.setdefault(printed, charge)
            continue
        category = _category_account(destination)
        # A plan's entry carries the plan key; any other charge keeps the
        # parser's per-row key unchanged. The plan key is the one place the
        # canonical identity lives: the map key and the payee stay printed.
        if charge.installment_total is not None:
            key = _plan_key(charge, person, _row_amount(charge)[1], _canonical_identity(printed))
        else:
            key = row_key
        entries.append(_charge_entry(liquidacion, charge, person, category, printed, key))

    surcharge_occurrences: dict[tuple[str, str, str, str], int] = {}
    for surcharge in liquidacion.surcharges:
        # A surcharge row carries no date of its own in the real document; the
        # statement's closing date stands in, deterministically, so the entry
        # (and its key) is stable across repeated imports — the liquidación
        # bills the charge when it closes, so the closing date is the honest
        # stand-in. ``Liquidacion`` exposes the closing month but not the closing
        # day, so the month's first day is the closest deterministic value the
        # parser as-is offers.
        when = surcharge.when or dt.date(liquidacion.closing_year, liquidacion.closing_month, 1)
        amount, currency = _row_amount(surcharge)
        seen = (when.isoformat(), surcharge.kind.value, f"{amount:.2f}", currency)
        occurrence = surcharge_occurrences.get(seen, 0) + 1
        surcharge_occurrences[seen] = occurrence
        key = _surcharge_key(surcharge, person, when, currency, occurrence)
        entries.append(_surcharge_entry(surcharge, person, when, key))

    if unclassified:
        raise CounterpartyClassificationError(_unclassified_message(unclassified))

    entries.sort(key=lambda entry: (entry.date, entry.meta["key"]))
    return entries


class ProvinciaVisaImporter(Importer):
    """Import one Banco Provincia ``Liquidación Visa`` card statement PDF per person.

    ``counterparty_map`` and ``text_reader`` are seams, exactly like the account
    extracto importer's. The map is created lazily on the first ``extract`` so
    that ``identify`` and :func:`expensuchis.importers.get_importers` need no
    ledger directory or environment variable; ``text_reader`` replaces PDF
    extraction in tests and defaults to :func:`expensuchis.importers.pdf.read_pdf`.
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
        """The visible importer identity: the product, not the bank.

        ``pipeline`` embeds this name in refusal messages and the CLI prints it
        as the importer that claimed a file, so it must not collide with the
        account extracto importer's. The counterparty map keeps ``SOURCE`` — the
        identity names the product; the source names the bank, and the bank is
        what the map is keyed on.
        """
        return NAME

    def identify(self, filepath: str) -> bool:
        """Claim the file when its extracted text carries the card marker.

        The marker is matched after folding (casefold + accent stripping) because
        the statement's title casing varies between renderings. A BBVA card
        statement is a *Liquidación* too, so the file is yielded whenever BBVA's
        structural check claims it: two claimants make ``extract`` refuse the
        file as ``ambiguous-importer``. Never raises and never prints: any
        extraction failure is a non-match.
        """
        try:
            text = self._read_text(filepath)
        except Exception:  # noqa: BLE001 - an unreadable file is simply not ours
            return False
        return provincia_visa.fold(MARKER) in provincia_visa.fold(text) and not (
            is_bbva_card_liquidacion(text)
        )

    def account(self, filepath: str) -> str:
        """Return the account the statement is about: the ARS card liability.

        The statement grows ``Liabilities:Provincia:<person>:Visa``; its USD rows
        grow the sibling ``VisaUSD`` account, but the archival account of a card
        statement is the peso liability, the one the settlement in the account
        extracto pays.
        """
        return CARD_ACCOUNT.format(person=derive_person(filepath))

    def sort(self, entries, reverse: bool = False) -> None:
        """Sort in place by date, then by the natural key.

        The base ``Importer.sort`` delegates to ``data.entry_sortkey``, which reads
        ``meta["lineno"]``; entries from this importer carry only the natural ``key``
        by design, so the base implementation would raise ``KeyError``. A
        date-then-key sort is total and deterministic, which is all the pipeline's
        month grouping needs.
        """
        entries.sort(key=lambda entry: (entry.date, entry.meta.get("key", "")), reverse=reverse)

    def extract(self, filepath: str, existing) -> list[data.Transaction]:
        """Parse and reconcile the statement, then build its transactions.

        The person comes from the path and the meaning from the parser;
        :class:`~expensuchis.importers.provincia_visa.ReconciliationError` and
        :class:`~expensuchis.importers.provincia_visa.LiquidacionParseError`
        propagate unchanged because their messages are already masked. A reader
        failure is wrapped in :class:`StatementReadError` — the raw message can
        carry the real path or a holder's name, and the CLI prints it. Nothing
        partial is ever returned.
        """
        person = derive_person(filepath)
        try:
            text = self._read_text(filepath)
        except Exception as exc:  # noqa: BLE001 - the class is the only safe detail
            # ``from None`` on purpose: the cause's message is exactly the leak.
            raise StatementReadError(
                f"cannot read the statement text: {type(exc).__name__}"
            ) from None
        liquidacion = provincia_visa.parse_liquidacion(text)
        counterparty_map = (
            self._counterparty_map if self._counterparty_map is not None else CounterpartyMap()
        )
        return build_entries(liquidacion, person, counterparty_map)

    def _read_text(self, filepath: str) -> str:
        if self._text_reader is not None:
            return self._text_reader(str(filepath))
        return read_pdf(filepath)[0]
