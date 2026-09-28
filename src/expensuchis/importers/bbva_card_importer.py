"""The BBVA card importer: wiring the Visa/Mastercard ``Liquidación`` into the ledger.

The parser (:mod:`expensuchis.importers.bbva_card`) owns the card statement's
meaning and its reconciliation gate; this module owns the ledger's side of it, in
the same shape as the Banco Provincia card importer
(:mod:`expensuchis.importers.provincia_visa_importer`): a pure ``build_entries``
seam, a lazily created counterparty map, reader seams for tests, and refusals
that never echo statement content.

* **The path layout is the account importer's.** A card statement lives at
  ``<...>/Bbva/<person>/<file>.pdf``, exactly like the ``Extracto consolidado``
  (T-07b owner decision): :func:`~expensuchis.importers.bbva_importer.derive_person`,
  :class:`~expensuchis.importers.bbva_importer.SourceAccountError`,
  :class:`~expensuchis.importers.bbva_importer.StatementReadError`,
  :class:`~expensuchis.importers.bbva_importer.CounterpartyClassificationError`
  and :data:`~expensuchis.importers.bbva_importer.SOURCE` are reused directly
  from that module rather than redefined -- one path contract, one set of
  exception types, so ``pipeline.py``'s preserved-message allowlist covers both
  importers with the same entries. :data:`NAME` is the visible identity (the
  product), distinct from ``SOURCE`` (the bank) -- mirroring
  ``ProvinciaVisaImporter``'s own ``NAME``/``SOURCE`` split.
* **Two readers, because the parser reads two different things.** ``identify``
  needs flat text (:func:`~expensuchis.importers.bbva_card.is_bbva_card_liquidacion`
  is a text predicate); ``extract`` needs :class:`~expensuchis.importers.pdf.PositionedRow`
  values (the parser's own input, because the two money columns are only
  distinguishable by position). ``text_reader`` and ``rows_reader`` are
  independent seams, defaulting to :func:`~expensuchis.importers.pdf.read_pdf`
  and :func:`~expensuchis.importers.pdf.read_pdf_rows` respectively. No
  ``CreationDate`` anchor is read: every row already carries its own ``dd-mmm-yy``
  year (T-07 owner decisions), unlike the account extracto.
* **Where each kind posts.** The card is a liability, so a purchase or charge
  grows it (T-06b's precedent, carried over per the T-07 owner decisions,
  2026-09-27):

  =====================================  =======================================
  Row kind                               Postings
  =====================================  =======================================
  purchase (ARS)                         ``Expenses:<category>`` +ARS,
                                         the brand's ARS liability -ARS
  purchase (USD)                         ``Expenses:<category>`` +USD,
                                         the brand's USD liability -USD, no ``@``
  installment row (``C.NN/NN``)          one entry for the **whole plan** (below)
  charge (PERCEPCION)                    ``Expenses:Impuestos:Percepciones`` +,
                                         the brand's liability of that currency -
  charge (IVA)                           ``Expenses:Impuestos:IVA`` +,
                                         the brand's liability of that currency -
  payment (``SU PAGO EN PESOS/USD``)     **nothing** (below)
  =====================================  =======================================

  - **The liability depends on both the card's brand and the row's currency.**
    :data:`~expensuchis.importers.bbva_importer.VISA_ACCOUNT` and
    :data:`~expensuchis.importers.bbva_importer.MASTERCARD_ACCOUNT` (the ARS
    liabilities the account extracto's own settlement postings already use) are
    reused here for a purchase's ARS leg; :data:`VISA_USD_ACCOUNT` and
    :data:`MASTERCARD_USD_ACCOUNT` are this module's own USD siblings, symmetric
    with Provincia's ``CARD_USD_ACCOUNT`` (T-07 owner decision 3).
  - **An installment row is realized once, not per statement.** T-06b's model,
    carried over explicitly (T-07 owner decisions, 2026-09-27): one transaction
    dated at the row's own purchase date, for the per-installment amount × the
    plan total, with ``installments``/``first_due``/``installment_amount``
    metadata (``docs/accounting-model.md``, "Installments"). Its ``key`` is a
    **plan** key -- independent of the row's comprobante and installment
    counter -- so the same plan seen on a later statement produces the same key
    and the pipeline's dedup drops the repeat instead of charging it twice.
    ``first_due`` needs the statement's due month, which
    :mod:`expensuchis.importers.bbva_card` never reads (``VENCIMIENTO ACTUAL``
    is documented page furniture -- see that module's docstring, "Document
    order"): this importer uses the statement's own ``close_date`` as the
    deterministic stand-in, exactly as ``ProvinciaVisaImporter`` stands in the
    closing date for an undated surcharge's own date. **Open question, not
    decided here:** whether a future statement's ``CIERRE``-to-``VENCIMIENTO``
    gap ever crosses a month boundary in a way that would shift ``first_due`` by
    one month from the true due date; the real files have not been checked for
    this gap because the parser does not expose it.
  - **A payment row emits nothing.** Same reasoning as T-06b: the account
    extracto already posts the settlement against the same liability.
  - **The additional cardholder's rows carry ``holder`` metadata; the holder's
    carry no such key at all** (T-07 owner decisions, point 2): see
    :data:`ADDITIONAL_HOLDER_TAG`. Both post to the *same* liability and the
    *same* shared ``Expenses:*`` categories -- ``docs/accounting-model.md``
    keeps expense categories household-wide, and this is the one source that
    would otherwise split them by person.
  - **Charges resolve with no map lookup.** ``PERCEPCION``/``IVA`` are taxes on
    the card's own operation, not spending at a counterparty: the parser's own
    shape classification is the whole decision, and the account is fixed (T-07
    owner decision 4).
* **The merchant identity.** :func:`_merchant_identity` starts from
  ``ProvinciaVisaImporter``'s own normalization (drop a leading ``*`` marker's
  processor prefix, then any pure-digit, date-like, time-like or CUIT-shaped
  token), then adds a **BBVA-specific departure** the real Visa file's own
  refusals forced (T-07e follow-up, owner decision "clean the noise",
  2026-09-27+1) -- deliberately **not** ported back into Provincia's own
  module, which stays untouched:

  - Any token containing a run of **7 or more digits**, once ``-``, ``.`` and
    ``/`` separators are ignored, is noise (a reference/coupon code) and is
    dropped **whole** -- *except* when the token is letters immediately
    followed by that long digit run and nothing else (:data:`_LETTER_PREFIX_DIGIT_SUFFIX_RE`):
    then the letters are kept and only the digit run is dropped, because that
    shape is a merchant name glued to its own reference, not a bare code.
  - An amount-shaped token (Argentine ``9.999,99``/``99,99``, mirroring the
    parser's own :data:`~expensuchis.importers.bbva_card._AMOUNT_TOKEN_RE`
    shape) is always dropped -- an inline original-currency restatement is
    never the merchant. A **3-letter uppercase token immediately before** an
    amount-shaped one is dropped too (a currency code introducing that
    restatement); a bare 3-letter token *not* followed by an amount is left
    alone, since nothing here says it isn't part of a name.
  - The ``*`` rule gains a fallback: if the segment **after** the last ``*``
    carries no letter once the above is applied, the segment **before** it is
    used instead (cleaned the same way) -- the real file's own
    ``<name>*<reference>`` shape, the mirror image of the ordinary
    ``<processor>*<merchant>`` one this importer already handled. Otherwise
    the existing after-``*`` rule is unchanged.

  A row whose identity would be an identifier rather than a merchant name (no
  letter survives anywhere) still refuses loudly, masked, exactly as before
  and as Provincia's own does.
* **The refusal.** An identity absent from the map stops the whole import with
  :class:`~expensuchis.importers.bbva_importer.CounterpartyClassificationError`
  (reused, not redefined), collecting every unique identity first. Every
  interpolated field passes through the redactor.
* **The natural key** (no parser-level ``*_keys`` helper exists for this format,
  unlike the account extracto's or Provincia's card's -- designed here, see
  :func:`_purchase_key`, :func:`_plan_key` and :func:`_charge_key`):

  - A **purchase** (comprobante present, unique across the whole document --
    both consumption sections share one tracking dict in the parser): ``bbva-card:
    <person>:<brand>:<when>:<comprobante>``. The comprobante alone is already
    document-unique; person, brand and date are added so a comprobante that
    happens to recur on some *later*, unrelated statement of the same brand and
    person is a documented, accepted residual (mirroring the plan key's own
    trade-off) rather than a silent collision across different people or cards.
  - A **plan** (installment row): ``bbva-card:<person>:<brand>:<purchase date>:
    <installment total>:<currency>:<canonical identity>`` -- Provincia's own
    five-field shape, with the brand added because one importer here serves two
    card products for the same person.
  - A **charge** (no comprobante): ``bbva-card:<person>:<brand>:<when>:
    <charge class>:<amount>:<currency>``, with a ``#N`` suffix on a genuine
    intra-document repeat -- mirroring ``ProvinciaVisaImporter``'s own surcharge
    key exactly, person and brand included from the start (the R4-1 lesson).

The importer follows the pipeline contract: a failure raises, it never returns
partial entries. ``identify`` never raises and never prints; the PDF engine and
the counterparty map are both created lazily, so constructing the importer needs
neither a ledger directory nor a statement.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from decimal import Decimal

from beancount.core import data
from beangulp import Importer

from ..counterparties import CounterpartyMap
from . import bbva_card
from .bbva_card import (
    CardBrand,
    CardLiquidacion,
    Currency,
    Movement,
    MovementKind,
    is_bbva_card_liquidacion,
    parse_card_liquidacion,
)
from .bbva_importer import (
    MASTERCARD_ACCOUNT,
    SOURCE,
    VISA_ACCOUNT,
    CounterpartyClassificationError,
    SourceAccountError,
    StatementReadError,
    derive_person,
)
from .pdf import PositionedRow, read_pdf, read_pdf_rows

__all__ = [
    "ADDITIONAL_HOLDER_TAG",
    "IVA_ACCOUNT",
    "MASTERCARD_USD_ACCOUNT",
    "NAME",
    "PERCEPCION_ACCOUNT",
    "SOURCE",
    "VISA_USD_ACCOUNT",
    "BBVACardImporter",
    "CounterpartyClassificationError",
    "SourceAccountError",
    "StatementReadError",
    "build_entries",
    "derive_person",
]

#: The visible importer identity: the product, not the bank. Distinguishes this
#: importer from ``BBVAImporter`` (the account extracto's) in pipeline refusals
#: and CLI output. The counterparty map stays keyed on ``SOURCE`` -- the bank.
NAME = "BBVACard"

#: The USD liabilities of each brand's card. The account extracto's own
#: ``VISA_ACCOUNT``/``MASTERCARD_ACCOUNT`` (ARS) are reused for the peso leg.
VISA_USD_ACCOUNT = "Liabilities:BBVA:{person}:VisaUSD"
MASTERCARD_USD_ACCOUNT = "Liabilities:BBVA:{person}:MastercardUSD"

#: Where a shape-classified charge posts (T-07 owner decision 4). ``IVA`` is
#: new to this codebase -- Provincia's own card never carries one.
PERCEPCION_ACCOUNT = "Expenses:Impuestos:Percepciones"
IVA_ACCOUNT = "Expenses:Impuestos:IVA"

#: The additional cardholder's transaction metadata value (T-07 owner decisions,
#: point 2, 2026-09-27): every movement inside the statement's *second*
#: consumption section (:attr:`~expensuchis.importers.bbva_card.Movement.is_additional_holder`)
#: carries ``holder: "P2"``; the holder's own rows carry no ``holder`` key at
#: all (absent means the card's owner). Fixed by the owner decision, not
#: derived from the statement (the additional cardholder's own name is never
#: read by the parser).
ADDITIONAL_HOLDER_TAG = "P2"

#: Per-row noise tokens in a card description, mirrored from
#: ``ProvinciaVisaImporter`` exactly (see the module docstring's documented
#: gap): a pure-digit run, a date-like ``dd/mm``/``dd/mm/yy`` or a time-like
#: ``hh:mm`` run.
_DIGIT_TOKEN_RE = re.compile(r"\d+")
_DATE_TOKEN_RE = re.compile(r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?")
_TIME_TOKEN_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
#: A CUIT/CUIL-shaped token: an 11-digit run, or the dotted/hyphenated/spaced
#: shape, as a single token.
_CUIT_TOKEN_RE = re.compile(r"\d{2}[ .-]\d{8}[ .-]\d")
#: The raw-description sweep, matched **before** any whitespace splitting -- a
#: run with space separators straddles two tokens.
_CUIT_SWEEP_RE = re.compile(r"(?<!\w)(?:\d{2}[ .-]?\d{8}[ .-]?\d|\d{11})(?!\w)")
#: A run of seven or more digits: what a printed identity must never carry once
#: separators are ignored -- a partially consumed CUIT is not CUIT-shaped any more.
_SEVEN_DIGIT_RUN_RE = re.compile(r"\d{7,}")
#: A CUIT/CUIL-shaped run, for redacting a refusal message.
_CUIT_RUN_RE = re.compile(r"(?<!\d)(?:\d{2}[ .-]?\d{8}[ .-]?\d|\d{11})(?!\d)")
#: Punctuation inside an identity, replaced by a space before collapsing.
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)

#: BBVA-specific noise, on top of the shapes above (T-07e follow-up, owner
#: decision "clean the noise", 2026-09-27+1) -- see the module docstring, "The
#: merchant identity". Deliberately not ported into
#: ``ProvinciaVisaImporter``, which carries none of this.
#:
#: An amount-shaped token, mirroring the parser's own
#: ``bbva_card._AMOUNT_TOKEN_RE`` shape (comma-decimal, optional dot-grouped
#: thousands): an inline original-currency restatement inside a description,
#: never the merchant.
_AMOUNT_TOKEN_RE = re.compile(r"^-?\$?\d{1,3}(?:\.\d{3})*,\d{2}-?$")
#: A bare 3-letter uppercase token -- a currency code, but only when it sits
#: immediately before an amount-shaped token (checked positionally, not here).
_CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}$")
#: Letters immediately followed by a 7+ digit run, and nothing else: a
#: merchant name glued to its own reference code, unlike a bare reference
#: (which carries no letters, or carries them on both sides of the digits).
#: The letters are kept; the digit run is dropped.
_LETTER_PREFIX_DIGIT_SUFFIX_RE = re.compile(r"^([A-Za-z]+)(\d{7,})$")
#: The separators a long digit run may be broken up by, per the owner decision.
_SEPARATOR_CHARS_RE = re.compile(r"[-./]")


def _is_noise_token(token: str) -> bool:
    """Whether a token is per-row noise: pure digits, a date, a time or a CUIT.

    Provincia's own four categories, unchanged. The BBVA-specific additions
    (amount-shaped, currency-code-before-amount, long-digit-run) are handled
    separately by :func:`_clean_bbva_tokens`, since the currency-code rule
    needs to look at the *next* token and the long-digit-run rule can
    transform a token instead of only dropping it.
    """
    return bool(
        _DIGIT_TOKEN_RE.fullmatch(token)
        or _DATE_TOKEN_RE.fullmatch(token)
        or _TIME_TOKEN_RE.fullmatch(token)
        or _CUIT_TOKEN_RE.fullmatch(token)
    )


def _has_long_digit_run(token: str) -> bool:
    """Whether ``token`` carries a run of 7+ digits once ``-``/``.``/``/`` are ignored."""
    return _SEVEN_DIGIT_RUN_RE.search(_SEPARATOR_CHARS_RE.sub("", token)) is not None


def _clean_bbva_tokens(tokens: Sequence[str]) -> list[str]:
    """Drop or transform BBVA's own per-row noise; see the module docstring.

    Runs Provincia's four noise categories (:func:`_is_noise_token`) plus
    three BBVA-specific rules the real Visa file's refusals forced (T-07e
    follow-up): an amount-shaped token is always dropped; a bare 3-letter
    uppercase token immediately before an amount-shaped one is dropped with
    it (a currency code introducing an inline original-currency restatement);
    and any other token carrying a 7+ digit run (separators ignored) is
    dropped whole, *unless* it is letters directly followed by that run and
    nothing else, in which case the letters are kept.
    """
    cleaned: list[str] = []
    count = len(tokens)
    index = 0
    while index < count:
        token = tokens[index]
        if _AMOUNT_TOKEN_RE.fullmatch(token) is not None:
            index += 1
            continue
        if (
            _CURRENCY_CODE_RE.fullmatch(token) is not None
            and index + 1 < count
            and _AMOUNT_TOKEN_RE.fullmatch(tokens[index + 1]) is not None
        ):
            index += 1  # the amount itself is dropped on the next iteration
            continue
        if _is_noise_token(token):
            index += 1
            continue
        letter_prefix_match = _LETTER_PREFIX_DIGIT_SUFFIX_RE.fullmatch(token)
        if letter_prefix_match is not None:
            cleaned.append(letter_prefix_match.group(1))
            index += 1
            continue
        if _has_long_digit_run(token):
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return cleaned


def _has_letter(text: str) -> bool:
    return any(character.isalpha() for character in text)


def _clean_and_join(segment: str) -> str:
    """Sweep a raw-text CUIT, split into tokens and apply :func:`_clean_bbva_tokens`."""
    swept = _CUIT_SWEEP_RE.sub(" ", segment)
    return " ".join(_clean_bbva_tokens(swept.split()))


def _refuse_identifier(page: int, row: int) -> None:
    raise bbva_card.CardLiquidacionParseError(
        f"page {page}, row {row}: the row carries an identifier and no merchant "
        f"name; refusing the document -- accepting such a row needs a "
        f"deliberate code decision"
    )


def _canonical_identity(text: str) -> str:
    """Return the identity's canonical form: casefolded, punctuation-free, collapsed.

    Lives only inside the plan key, where month-to-month text drift matters.
    """
    return " ".join(_PUNCTUATION_RE.sub(" ", text.casefold()).split())


def _merchant_identity(description: str, page: int, row: int) -> str:
    """Return a purchase's **printed** counterparty identity.

    Starts from ``ProvinciaVisaImporter._merchant_identity``'s own rule (drop
    the segment up to and including a leading ``*`` marker, then whitespace-
    collapse what survives noise-cleaning, casing and punctuation kept), with
    the BBVA-specific additions :func:`_clean_bbva_tokens` applies and the
    ``*`` fallback below -- see the module docstring, "The merchant identity".

    When the description carries a ``*`` and the segment **after** the last
    one has no letter left once cleaned, the segment **before** it is used
    instead (cleaned the same way): the real file's own ``<name>*<reference>``
    shape.

    Raises:
        CardLiquidacionParseError: no merchant name remains after the drops, or
            the remaining identity still carries a 7+ digit run. Masked.
    """
    if "*" in description:
        before, after = description.rsplit("*", 1)
        printed = _clean_and_join(after)
        raw_candidate = after
        if not _has_letter(printed):
            printed = _clean_and_join(before)
            raw_candidate = before
    else:
        printed = _clean_and_join(description)
        raw_candidate = description
    if not printed:
        if raw_candidate.strip():
            _refuse_identifier(page, row)
        raise bbva_card.CardLiquidacionParseError(
            f"page {page}, row {row}: the row carries no merchant identity after "
            f"normalization; refusing the document"
        )
    if _SEVEN_DIGIT_RUN_RE.search(re.sub(r"[ .-]", "", printed)):
        _refuse_identifier(page, row)
    return printed


def _redact_cuit(text: str) -> str:
    return _CUIT_RUN_RE.sub("<cuit>", text)


def _category_account(destination: str) -> str:
    """Return the ledger account named by a resolved counterparty-map destination."""
    _marker, _separator, account = destination.partition(":")
    return account


def _brand_token(brand: CardBrand) -> str:
    return brand.value


def _card_account(brand: CardBrand, currency: Currency, person: str) -> str:
    """Return the card liability account for this brand and this row's currency."""
    if brand is CardBrand.VISA:
        template = VISA_ACCOUNT if currency is Currency.ARS else VISA_USD_ACCOUNT
    else:
        template = MASTERCARD_ACCOUNT if currency is Currency.ARS else MASTERCARD_USD_ACCOUNT
    return template.format(person=person)


def _commodity(currency: Currency) -> str:
    return "ARS" if currency is Currency.ARS else "USD"


def _first_due(liquidacion: CardLiquidacion, movement: Movement) -> str:
    """Return the plan's first installment month as ``"YYYY-MM"``.

    The statement's ``close_date`` stands in for ``VENCIMIENTO ACTUAL``, which
    the parser never reads (documented page furniture -- see the module
    docstring). The subtraction is in month arithmetic, so it crosses a year
    boundary correctly.
    """
    months_back = movement.installment_number - 1  # type: ignore[operator]
    close = liquidacion.close_date
    absolute = close.year * 12 + (close.month - 1) - months_back
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def _purchase_key(person: str, brand: CardBrand, movement: Movement) -> str:
    """Return a non-plan purchase's key.

    ``bbva-card:<person>:<brand>:<when>:<comprobante>``: the comprobante is
    already unique across the whole document (both consumption sections share
    one tracking dict in the parser); person, brand and date are added so a
    comprobante recurrence across a *different*, unrelated statement of the
    same brand and person is a documented residual rather than a silent
    cross-person or cross-brand collision.
    """
    return f"bbva-card:{person}:{_brand_token(brand)}:{movement.when.isoformat()}:{movement.comprobante}"


def _plan_key(person: str, brand: CardBrand, movement: Movement, identity: str) -> str:
    """Return the plan key: identical for the same plan seen in a later statement.

    Mirrors ``ProvinciaVisaImporter._plan_key``'s five fields, with the brand
    added (one importer serves two card products per person). Deliberately
    independent of the row's comprobante and installment counter -- exactly
    what a later statement's row of the same plan changes.
    """
    return (
        f"bbva-card:{person}:{_brand_token(brand)}:{movement.when.isoformat()}:"
        f"{movement.installment_total}:{_commodity(movement.currency)}:{identity}"
    )


def _charge_key(person: str, brand: CardBrand, movement: Movement, occurrence: int) -> str:
    """Return a charge's key: stable per document, unique inside a batch.

    Mirrors ``ProvinciaVisaImporter._surcharge_key`` exactly, person and brand
    included from the start (the R4-1 lesson: a natural key must carry every
    dimension that distinguishes two legitimately different facts).
    """
    base = (
        f"bbva-card:{person}:{_brand_token(brand)}:{movement.when.isoformat()}:"
        f"{movement.charge_class.value}:{movement.amount:.2f}:{_commodity(movement.currency)}"
    )
    return base if occurrence == 1 else f"{base}#{occurrence}"


def _purchase_entry(
    liquidacion: CardLiquidacion,
    movement: Movement,
    brand: CardBrand,
    person: str,
    category: str,
    identity: str,
    key: str,
) -> data.Transaction:
    """Build one entry for a purchase row: a plan (installment) or a plain purchase."""
    currency = _commodity(movement.currency)
    meta: dict[str, object] = {"key": key}
    if movement.installment_total is not None:
        posted = movement.amount * movement.installment_total
        meta["installments"] = Decimal(movement.installment_total)
        meta["first_due"] = _first_due(liquidacion, movement)
        meta["installment_amount"] = data.Amount(movement.amount, currency)
    else:
        posted = movement.amount
    if movement.is_additional_holder:
        meta["holder"] = ADDITIONAL_HOLDER_TAG
    card = _card_account(brand, movement.currency, person)
    postings = [
        data.Posting(category, data.Amount(posted, currency), None, None, None, None),
        data.Posting(card, data.Amount(-posted, currency), None, None, None, None),
    ]
    return data.Transaction(
        meta, movement.when, "*", identity, movement.description, frozenset(), frozenset(), postings
    )


def _charge_entry(movement: Movement, brand: CardBrand, person: str, key: str) -> data.Transaction:
    """Build the entry for one charge row (PERCEPCION or IVA)."""
    currency = _commodity(movement.currency)
    category = (
        PERCEPCION_ACCOUNT
        if movement.charge_class is bbva_card.ChargeClass.PERCEPCION
        else IVA_ACCOUNT
    )
    card = _card_account(brand, movement.currency, person)
    postings = [
        data.Posting(category, data.Amount(movement.amount, currency), None, None, None, None),
        data.Posting(card, data.Amount(-movement.amount, currency), None, None, None, None),
    ]
    return data.Transaction(
        {"key": key},
        movement.when,
        "*",
        SOURCE,
        movement.description,
        frozenset(),
        frozenset(),
        postings,
    )


def _unclassified_message(unclassified: dict[str, Movement]) -> str:
    """Build the pinned refusal message for the unique unclassified counterparties.

    Mirrors ``ProvinciaVisaImporter``'s own message shape exactly.
    """
    lines = [f"{len(unclassified)} counterparties are not classified."]
    for identity, movement in unclassified.items():
        currency = _commodity(movement.currency)
        lines.append(
            f"  {movement.when.isoformat()}  {movement.amount:,.2f} {currency}  "
            f'"{_redact_cuit(movement.description)}"  "{_redact_cuit(identity)}"'
        )
        lines.append("    append to counterparties.tsv:")
        lines.append(f"      {SOURCE}\t{_redact_cuit(identity)}\texpense:<category>")
    return "\n".join(lines)


def build_entries(
    liquidacion: CardLiquidacion, person: str, counterparty_map: CounterpartyMap
) -> list[data.Transaction]:
    """Turn a reconciled card statement into beancount transactions.

    Pure over its arguments: it touches no filesystem and no environment, so it
    is the unit-test seam for posting logic and the counterparty refusal.

    Payment rows emit nothing (see the module docstring): they are the card
    settlement, already posted by the account extracto.

    Raises:
        CardLiquidacionParseError: a purchase row that needs a counterparty has
            no merchant identity after normalization. The message is masked.
        CounterpartyClassificationError: at least one purchase names a
            counterparty absent from the map. Every unknown counterparty is
            collected before the refusal, so one run tells the whole story.
    """
    brand = liquidacion.brand
    entries: list[data.Transaction] = []
    unclassified: dict[str, Movement] = {}
    charge_occurrences: dict[str, int] = {}

    for movement in liquidacion.movements:
        if movement.kind is MovementKind.PAYMENT:
            continue
        if movement.kind is MovementKind.CHARGE:
            base_key = _charge_key(person, brand, movement, occurrence=1)
            occurrence = charge_occurrences.get(base_key, 0) + 1
            charge_occurrences[base_key] = occurrence
            key = _charge_key(person, brand, movement, occurrence)
            entries.append(_charge_entry(movement, brand, person, key))
            continue

        # MovementKind.PURCHASE
        identity = _merchant_identity(movement.description, movement.page, movement.row)
        destination = counterparty_map.resolve(SOURCE, identity)
        if destination is None:
            unclassified.setdefault(identity, movement)
            continue
        category = _category_account(destination)
        if movement.installment_total is not None:
            key = _plan_key(person, brand, movement, _canonical_identity(identity))
        else:
            key = _purchase_key(person, brand, movement)
        entry = _purchase_entry(liquidacion, movement, brand, person, category, identity, key)
        entries.append(entry)

    if unclassified:
        raise CounterpartyClassificationError(_unclassified_message(unclassified))

    entries.sort(key=lambda entry: (entry.date, entry.meta["key"]))
    return entries


class BBVACardImporter(Importer):
    """Import one BBVA Visa or Mastercard ``Liquidación`` PDF per person.

    ``counterparty_map``, ``text_reader`` and ``rows_reader`` are seams. The map
    is created lazily on the first ``extract`` so that ``identify`` and
    :func:`expensuchis.importers.get_importers` need no ledger directory or
    environment variable. ``text_reader`` replaces flat-text extraction in
    tests (used by ``identify`` only) and defaults to
    :func:`expensuchis.importers.pdf.read_pdf`; ``rows_reader`` replaces
    positioned-row extraction (used by ``extract`` only) and defaults to
    :func:`expensuchis.importers.pdf.read_pdf_rows`.
    """

    def __init__(
        self,
        counterparty_map: CounterpartyMap | None = None,
        text_reader: Callable[[str], str] | None = None,
        rows_reader: Callable[[str], Sequence[PositionedRow]] | None = None,
    ) -> None:
        self._counterparty_map = counterparty_map
        self._text_reader = text_reader
        self._rows_reader = rows_reader

    @property
    def name(self) -> str:
        """The visible importer identity: the product, not the bank."""
        return NAME

    def identify(self, filepath: str) -> bool:
        """Claim the file when its extracted text satisfies the parser's own marker.

        Delegates to :func:`~expensuchis.importers.bbva_card.is_bbva_card_liquidacion`,
        so identification and parsing can never disagree on what counts as a
        card statement. Never raises and never prints: any extraction failure
        is a non-match.
        """
        try:
            text = self._read_text(filepath)
        except Exception:  # noqa: BLE001 - an unreadable file is simply not ours
            return False
        return is_bbva_card_liquidacion(text)

    def account(self, filepath: str) -> str:
        """Return a fixed archival account: the person's ARS Visa liability.

        Required by beangulp's ``Importer`` ABC. The real brand is only known
        after reading the statement's rows, and this project's own pipeline
        never calls this method (only beangulp's own file-organizing feature
        would) -- mirroring ``BBVAImporter.account``'s own rationale.
        """
        return VISA_ACCOUNT.format(person=derive_person(filepath))

    def sort(self, entries, reverse: bool = False) -> None:
        """Sort in place by date, then by the natural key (no ``lineno`` carried)."""
        entries.sort(key=lambda entry: (entry.date, entry.meta.get("key", "")), reverse=reverse)

    def extract(self, filepath: str, existing) -> list[data.Transaction]:
        """Parse and reconcile the statement, then build its transactions.

        The person comes from the path and the meaning from the parser;
        :class:`~expensuchis.importers.bbva_card.ReconciliationError` and
        :class:`~expensuchis.importers.bbva_card.CardLiquidacionParseError`
        propagate unchanged because their messages are already masked. A
        reader failure is wrapped in :class:`StatementReadError` -- the raw
        message can carry the real path. Nothing partial is ever returned.
        """
        person = derive_person(filepath)
        try:
            rows = self._read_rows(filepath)
        except Exception as exc:  # noqa: BLE001 - the class is the only safe detail
            raise StatementReadError(
                f"cannot read the statement rows: {type(exc).__name__}"
            ) from None
        liquidacion = parse_card_liquidacion(rows)
        counterparty_map = (
            self._counterparty_map if self._counterparty_map is not None else CounterpartyMap()
        )
        return build_entries(liquidacion, person, counterparty_map)

    def _read_text(self, filepath: str) -> str:
        if self._text_reader is not None:
            return self._text_reader(str(filepath))
        return read_pdf(filepath)[0]

    def _read_rows(self, filepath: str) -> Sequence[PositionedRow]:
        if self._rows_reader is not None:
            return self._rows_reader(str(filepath))
        return read_pdf_rows(filepath)
