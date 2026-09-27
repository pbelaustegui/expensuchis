"""Parser for BBVA Argentina's Visa and Mastercard card ``Liquidación``.

Pure over :class:`expensuchis.importers.pdf.PositionedRow` values: no PDF
library, no filesystem, no clock and no environment. The caller (the
importer's ``extract``, a later task) owns text extraction via
:func:`expensuchis.importers.pdf.read_pdf_rows`; this module owns meaning.

The parser is a **gate**, not a reader. :func:`parse_card_liquidacion`
reconciles the document against itself and raises :class:`ReconciliationError`
whenever a reconciliation check fails; it never returns a partially trusted
result. There is no ``strict=False``: the caller cannot opt out of a check it
does not like.

Geometry: why positioned rows
------------------------------

BBVA's own flat text extraction interleaves two money columns, ``PESOS`` and
``DÓLARES``, printed side by side -- the T-07 card reconnaissance found that a
plain line-based parser cannot tell which column an amount belongs to; only
its horizontal position on the page can. :func:`expensuchis.importers.pdf.read_pdf_rows`
supplies that position as :class:`~expensuchis.importers.pdf.PositionedRow`
values (tokens with ``x0``/``x1``), and this parser derives the two columns'
right edges from the document's own **detail header row**
(``FECHA … PESOS DÓLARES``, ``_is_detail_header``) rather than hardcoding them
-- the reconnaissance measured real edges near x≈502 (PESOS) and x≈574
(DÓLARES), but the parser trusts the document, not that measurement, within
:data:`_COLUMN_TOLERANCE` points. An amount whose right edge lands near
neither derived edge (or near both) is refused rather than guessed
(:func:`_classify_column`).

An amount printed to the **left** of the columns -- an inline original
foreign-currency amount or a perception's base, both observed in the real
document -- is never mistaken for the movement: only the row's **last**
token is ever read as the movement amount (see "Row grammar" below), so an
earlier amount-shaped token is always kept as plain description text.

Document order
---------------

::

    (brand marker: VISA or MASTERCARD, anywhere)
    CIERRE ACTUAL <dd-mmm-yy>                          -- the close date
    ... summary box furniture (PAGO MÍNIMO, limits, rates) ...
    SALDO ACTUAL <ARS> [<USD>]                         -- the summary figure
    SALDO ANTERIOR <ARS> [<USD>]                       -- opening balances
    SU PAGO EN PESOS <amount>                          -- zero or more
    SU PAGO EN USD <amount>                            --   payment rows
    FECHA ... PESOS [DÓLARES]                          -- detail header:
      <consumption rows>                                  opens a section
    TOTAL CONSUMOS DE <name> <ARS> [<USD>]             -- closes it
    [ FECHA ... PESOS [DÓLARES]                        -- a second section
      <consumption rows>                                  (the additional
      TOTAL CONSUMOS DE <name> <ARS> [<USD>]           --  cardholder's) ]
    [ Impuestos, cargos e intereses                    -- charges heading
      <charge rows> ]                                     (both optional)
    SALDO ACTUAL <ARS> [<USD>]                         -- the detail figure
    ... prose, "cuotas a vencer" ...                   -- ignored

Everything before the first detail header, and between a section's
``TOTAL CONSUMOS`` and the next boundary, is scanned leniently: an
unrecognized row there is furniture (mirrors ``bbva.py``'s "outside a
region" leniency). **Inside** a consumption or charges section every row must
be a recognized shape, its section's closing marker, or -- for the charges
zone only -- the one recognized heading line; anything else refuses with a
masked diagnostic (the Brubank/T-08 lesson: never skip silently). This is
also how the reconnaissance's documented "known unknown" is handled: the real
Visa holder section carries one undated line with a column amount, very
likely a wrapped description. This parser does **not** guess a merge rule for
it -- an undated row inside a section refuses, by the same general rule, until
the real-file probe (T-07f) shows the actual shape.

There are between one and two consumption sections. The **second** one, when
present, is the additional cardholder's -- :attr:`Movement.is_additional_holder`
marks every movement inside it. The holder's own name and the additional
cardholder's are never read: :func:`_row_two_column_amounts` only ever
extracts amount-shaped tokens from a ``TOTAL CONSUMOS DE <name>`` row, so the
name -- of unknown, variable length -- is skipped without ever being
inspected, let alone stored or echoed.

Row grammar
------------

A **consumption row**: ``<dd-mmm-yy date> <description words...>
[C.NN/NN] <comprobante: 6 digits> <amount>``. The row's very last token is
always the movement amount (column-classified); the token before it is
always the comprobante; the token before *that*, when it matches
``C.NN/NN``, is the installment marker; everything between the date and
there is the description, kept verbatim (it may itself contain an inline
``USD <amount>`` pair or a lone ``*`` merchant marker -- never parsed as the
movement, precisely because it is not the row's last token).

A **charge row** (post-total, no date and no comprobante): ``<description
words...> <amount>``, classified **by shape**, never by trusting a label
alone (owner decision, 2026-09-27): ``IIBB PERCEP-*`` or ``DB.RG NNNN`` is a
:attr:`ChargeClass.PERCEPCION`; ``IVA RG NNNN`` is a
:attr:`ChargeClass.IVA`; any other shape refuses the document.

A **payment row**: ``SU PAGO EN PESOS <amount>`` or ``SU PAGO EN USD
<amount>``, kept as a :attr:`MovementKind.PAYMENT` movement -- **never
dropped by this parser** (the importer drops it, mirroring the Provincia
precedent: the extracto owns the cash movement, so reconciliation here still
needs the payment counted). Amounts are kept **as printed**: a trailing
minus is a credit, exactly as :mod:`expensuchis.importers.provincia_visa`
keeps its own payment rows.

Reconciliation
---------------

Every check is computed and reported, even after an earlier one fails, so one
run tells the whole story (see :data:`CHECK_NAMES`): each consumption
section's movement sum, per currency, must equal its own printed
``TOTAL CONSUMOS``; per currency, opening balance plus every movement
(payments, consumption, charges, all signed as printed) must equal the
**detail** ``SALDO ACTUAL``; and the **summary** box's ``SALDO ACTUAL`` must
agree with the **detail**'s.

Masking
-------

The real statements are household data and the models that run this workflow
are remote. **No diagnostic may echo a raw statement token.** Every
:class:`CheckResult` detail and every :class:`CardLiquidacionParseError`
message reports page and row numbers, counts and field names only -- never an
amount, a merchant, a cardholder name or a comprobante. ``Movement.description``
and ``Movement.comprobante`` are legitimate carried *data*, exactly like
``provincia_visa.Charge.description`` -- masking is a property of diagnostics,
never of the parsed result.

Open assumptions for the real-file probe (T-07f)
--------------------------------------------------

The reconnaissance did not pin down every shape used here; each of the
following is a documented, testable choice, not a guess smuggled in as fact:

* ``CIERRE ACTUAL`` is followed directly (within two tokens) by a
  ``dd-mmm-yy`` date, the same shape consumption rows use.
* The summary box's ``SALDO ACTUAL`` and ``SALDO ANTERIOR`` are each **one**
  row carrying one or two column-amounts (mirroring the detail's own
  ``SALDO ACTUAL`` row), not two separate per-currency lines.
* Neither ``SU PAGO EN *`` rows nor charge rows carry their own date (unlike
  Provincia's payment row); both are recorded with ``when=None``.
* A Mastercard document's detail header omits the ``DÓLARES`` token
  entirely (rather than printing it over an empty column) -- when it does,
  :data:`_dolares_edge` is ``None`` and any amount is refused as
  :attr:`Currency.USD` (``_classify_column``).
* The one recognized charges-section heading is a line containing the word
  ``impuestos``; any other heading text is unrecognized and refuses.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ..numbers import AmountFormat, AmountParseError, parse_amount
from .pdf import PositionedRow, PositionedToken

__all__ = [
    "CHECK_NAMES",
    "CardBrand",
    "CardLiquidacion",
    "CardLiquidacionParseError",
    "ChargeClass",
    "CheckResult",
    "Currency",
    "Movement",
    "MovementKind",
    "ReconciliationError",
    "fold",
    "is_bbva_card_liquidacion",
    "parse_card_liquidacion",
]

#: The checks, in the fixed order they are reported. See the module
#: docstring, "Reconciliation".
CHECK_NAMES: tuple[str, ...] = (
    "section-sums-ars",
    "section-sums-usd",
    "balance-ars",
    "balance-usd",
    "summary-detail-agreement",
    "comprobante-unique",
    "installment-valid",
)

#: The y-classification tolerance (points) around a derived column edge. The
#: reconnaissance measured PESOS/DÓLARES edges roughly 72pt apart, so a
#: two-digit tolerance still cannot straddle both.
_COLUMN_TOLERANCE = 10.0

_AMOUNT_TOKEN_RE = re.compile(r"^-?\$?\d{1,3}(?:\.\d{3})*,\d{2}-?$")
_COMPROBANTE_RE = re.compile(r"^\d{6}$")
_INSTALLMENT_RE = re.compile(r"^C\.(\d{1,2})/(\d{1,2})$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(?P<day>\d{1,2})-(?P<month>[^\s\d-]+)-(?P<year>\d{2})$")
_DB_RG_RE = re.compile(r"^db\.rg\s+\d+")
_IVA_RG_RE = re.compile(r"^iva rg\s+\d+")

_PAYMENT_PESOS_PREFIX = ("su", "pago", "en", "pesos")
_PAYMENT_USD_PREFIX = ("su", "pago", "en", "usd")

_MONTHS: dict[str, int] = {
    "enero": 1,
    "ene": 1,
    "febrero": 2,
    "feb": 2,
    "marzo": 3,
    "mar": 3,
    "abril": 4,
    "abr": 4,
    "mayo": 5,
    "may": 5,
    "junio": 6,
    "jun": 6,
    "julio": 7,
    "jul": 7,
    "agosto": 8,
    "ago": 8,
    "septiembre": 9,
    "setiembre": 9,
    "sep": 9,
    "set": 9,
    "octubre": 10,
    "oct": 10,
    "noviembre": 11,
    "nov": 11,
    "diciembre": 12,
    "dic": 12,
}


def fold(text: str) -> str:
    """Casefold and strip accents: ``DÓLARES`` and ``dolares`` compare equal."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def is_bbva_card_liquidacion(text: str) -> bool:
    """Whether ``text`` is a BBVA Visa/Mastercard card ``Liquidación``.

    Structural, over folded lines: a ``CIERRE ACTUAL`` line, a ``SALDO
    ANTERIOR`` line, and a detail header line carrying both ``FECHA`` and
    ``PESOS`` as separate tokens. Every one of the three is required: BBVA's
    own ``Extracto consolidado`` (:func:`expensuchis.importers.bbva.is_bbva_extracto`)
    carries ``SALDO ANTERIOR`` too but never ``CIERRE ACTUAL`` or a
    ``PESOS``-carrying header, and Banco Provincia's card liquidación
    (:mod:`expensuchis.importers.provincia_visa`) carries ``CIERRE`` but
    never ``CIERRE ACTUAL`` as two adjacent words.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    has_cierre_actual = any("cierre actual" in fold(line) for line in lines)
    has_saldo_anterior = any("saldo anterior" in fold(line) for line in lines)
    has_detail_header = any({"fecha", "pesos"} <= set(fold(line).split()) for line in lines)
    return has_cierre_actual and has_saldo_anterior and has_detail_header


class CardBrand(Enum):
    """The closed vocabulary of card brands this parser recognizes."""

    VISA = "visa"
    MASTERCARD = "mastercard"


class Currency(Enum):
    """The closed vocabulary of movement currencies: which column an amount sat in."""

    ARS = "ars"
    USD = "usd"


class MovementKind(Enum):
    """The closed concept vocabulary of one row: what kind of movement it is."""

    PURCHASE = "purchase"
    PAYMENT = "payment"
    CHARGE = "charge"


class ChargeClass(Enum):
    """The closed vocabulary of a charge row's shape (owner decision, 2026-09-27)."""

    PERCEPCION = "percepcion"
    IVA = "iva"


@dataclass(frozen=True)
class Movement:
    """One reconciled movement, with the position it came from for diagnostics.

    ``when`` is ``None`` for :attr:`MovementKind.PAYMENT` and
    :attr:`MovementKind.CHARGE` rows (neither is observed to carry its own
    date -- see the module docstring's open assumptions). ``comprobante`` is
    the empty string for those two kinds; ``installment_number``/``_total``
    are ``None`` unless the row carried a ``C.NN/NN`` marker.
    ``is_additional_holder`` is ``True`` only for a movement inside the
    **second** consumption section -- the additional cardholder's -- and is
    always ``False`` for a payment or a charge. The cardholder's own name is
    never read, let alone stored here.
    """

    kind: MovementKind
    charge_class: ChargeClass | None
    when: dt.date | None
    currency: Currency
    amount: Decimal
    installment_number: int | None
    installment_total: int | None
    comprobante: str
    description: str
    is_additional_holder: bool
    page: int
    row: int


@dataclass(frozen=True)
class CheckResult:
    """One reconciliation check and its **masked** detail."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class CardLiquidacion:
    """A fully reconciled card statement. Only :func:`parse_card_liquidacion` builds one."""

    brand: CardBrand
    close_date: dt.date
    opening_ars: Decimal
    opening_usd: Decimal | None
    summary_saldo_actual_ars: Decimal
    summary_saldo_actual_usd: Decimal | None
    detail_saldo_actual_ars: Decimal
    detail_saldo_actual_usd: Decimal | None
    movements: tuple[Movement, ...]
    checks: tuple[CheckResult, ...]


class CardLiquidacionParseError(ValueError):
    """The rows are not a parsable BBVA card ``Liquidación``.

    Structural failure, distinct from :class:`ReconciliationError`: the brand,
    the ``CIERRE ACTUAL`` row, the detail header (and so the column edges),
    ``SALDO ANTERIOR``, a consumption section's ``TOTAL CONSUMOS`` line or the
    detail ``SALDO ACTUAL`` line is missing or malformed; a row inside a
    section matches no recognized shape; an amount is malformed; or an
    amount's right edge lands near neither derived column edge (or near
    both). The message never echoes statement text.
    """


class ReconciliationError(RuntimeError):
    """A parsed document failed a reconciliation check.

    Carries **every** check in :attr:`checks` (so one run tells the whole
    story) and the failed subset in :attr:`failures`. The message names the
    failed checks only; the details are masked and live on :attr:`checks`.
    """

    def __init__(self, checks: tuple[CheckResult, ...]) -> None:
        self.checks = checks
        self.failures = tuple(check for check in checks if not check.ok)
        names = ", ".join(check.name for check in self.failures)
        super().__init__(
            f"BBVA card reconciliation failed: {names}. "
            f"Refusing the document; a partially trusted parse is never returned."
        )


# --------------------------------------------------------------------- token helpers


def _is_amount_token(text: str) -> bool:
    return _AMOUNT_TOKEN_RE.fullmatch(text) is not None


def _parse_amount(raw: str, page: int, row: int, field: str) -> Decimal:
    try:
        return parse_amount(raw, AmountFormat.ARS)
    except AmountParseError:
        # ``from None`` on purpose: the AmountParseError message echoes the raw
        # number, and a diagnostic must never carry statement text.
        raise CardLiquidacionParseError(
            f"page {page}, row {row}: the {field} is not a well-formed "
            f"comma-decimal amount (length {len(raw)})"
        ) from None


def _folded_tokens(row: PositionedRow) -> list[str]:
    return [fold(token.text) for token in row.tokens]


def _folded_prefix(row: PositionedRow, length: int) -> tuple[str, ...]:
    folded = _folded_tokens(row)
    if len(folded) < length:
        return ()
    return tuple(folded[:length])


def _parse_date_token(text: str, page: int, row: int) -> dt.date:
    match = _DATE_RE.match(text)
    if match is None:
        raise CardLiquidacionParseError(f"page {page}, row {row}: the date is not dd-mmm-yy")
    month = _MONTHS.get(fold(match.group("month")))
    if month is None:
        raise CardLiquidacionParseError(
            f"page {page}, row {row}: the date's month is not recognized"
        )
    try:
        return dt.date(2000 + int(match.group("year")), month, int(match.group("day")))
    except ValueError:
        raise CardLiquidacionParseError(
            f"page {page}, row {row}: the date is not a real calendar date"
        ) from None


# --------------------------------------------------------------------- structural markers


def _is_detail_header(row: PositionedRow) -> bool:
    folded = set(_folded_tokens(row))
    return "fecha" in folded and "pesos" in folded


def _is_saldo_anterior(row: PositionedRow) -> bool:
    return _folded_prefix(row, 2) == ("saldo", "anterior")


def _is_saldo_actual(row: PositionedRow) -> bool:
    return _folded_prefix(row, 2) == ("saldo", "actual")


def _is_cierre_actual(row: PositionedRow) -> bool:
    return _folded_prefix(row, 2) == ("cierre", "actual")


def _is_charges_heading(row: PositionedRow) -> bool:
    return any("impuestos" in folded for folded in _folded_tokens(row))


def _parse_cierre_actual(row: PositionedRow) -> dt.date:
    """Parse the ``CIERRE ACTUAL <dd-mmm-yy>`` row into its close date.

    The date is the first token within the row's next three tokens (after
    ``CIERRE ACTUAL``) that matches the ``dd-mmm-yy`` shape -- tolerant of one
    stray punctuation mark in between, without trusting an exact offset.
    """
    date_token = next(
        (token for token in row.tokens[2:5] if _DATE_RE.match(token.text) is not None), None
    )
    if date_token is None:
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: the CIERRE ACTUAL row carries no dd-mmm-yy date"
        )
    return _parse_date_token(date_token.text, row.page, row.row)


def _find_row(
    rows: Sequence[PositionedRow], predicate: Callable[[PositionedRow], bool], start: int = 0
) -> int | None:
    for index in range(start, len(rows)):
        if predicate(rows[index]):
            return index
    return None


def _find_brand(rows: Sequence[PositionedRow]) -> CardBrand:
    for row in rows:
        for folded in _folded_tokens(row):
            if folded == "mastercard":
                return CardBrand.MASTERCARD
            if folded == "visa":
                return CardBrand.VISA
    raise CardLiquidacionParseError("no VISA/MASTERCARD brand marker was found")


def _find_column_edges(rows: Sequence[PositionedRow]) -> tuple[float, float | None]:
    for row in rows:
        if not _is_detail_header(row):
            continue
        pesos_edge: float | None = None
        dolares_edge: float | None = None
        for token in row.tokens:
            folded = fold(token.text)
            if folded == "pesos":
                pesos_edge = token.x1
            elif folded == "dolares":
                dolares_edge = token.x1
        if pesos_edge is not None:
            return pesos_edge, dolares_edge
    raise CardLiquidacionParseError(
        "no detail header row (FECHA ... PESOS [DOLARES]) was found; the column "
        "edges cannot be derived"
    )


def _classify_column(
    token: PositionedToken, pesos_edge: float, dolares_edge: float | None, page: int, row: int
) -> Currency:
    near_pesos = abs(token.x1 - pesos_edge) <= _COLUMN_TOLERANCE
    near_dolares = dolares_edge is not None and abs(token.x1 - dolares_edge) <= _COLUMN_TOLERANCE
    if near_pesos and not near_dolares:
        return Currency.ARS
    if near_dolares and not near_pesos:
        return Currency.USD
    raise CardLiquidacionParseError(
        f"page {page}, row {row}: an amount's right edge lands near neither derived "
        f"column edge (or near both); refusing rather than guessing"
    )


def _row_two_column_amounts(
    row: PositionedRow, pesos_edge: float, dolares_edge: float | None, field: str
) -> tuple[Decimal, Decimal | None]:
    """Extract the (ARS, USD-or-None) amounts of a ``SALDO``/``TOTAL`` row.

    Scans **every** token for an amount shape; non-amount tokens (labels, a
    cardholder's name of unknown length) are silently skipped, never read.
    This is deliberate, not a shortcut: it is what lets ``TOTAL CONSUMOS DE
    <name>`` be parsed without ever inspecting the name (see the module
    docstring).
    """
    ars: Decimal | None = None
    usd: Decimal | None = None
    found_any = False
    for token in row.tokens:
        if not _is_amount_token(token.text):
            continue
        found_any = True
        currency = _classify_column(token, pesos_edge, dolares_edge, row.page, row.row)
        value = _parse_amount(token.text, row.page, row.row, field)
        if currency is Currency.ARS:
            if ars is not None:
                raise CardLiquidacionParseError(
                    f"page {row.page}, row {row.row}: the {field} row carries two ARS amounts"
                )
            ars = value
        else:
            if usd is not None:
                raise CardLiquidacionParseError(
                    f"page {row.page}, row {row.row}: the {field} row carries two USD amounts"
                )
            usd = value
    if not found_any or ars is None:
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: the {field} row carries no ARS amount"
        )
    return ars, usd


# --------------------------------------------------------------------- row parsers


def _parse_purchase_row(
    row: PositionedRow,
    pesos_edge: float,
    dolares_edge: float | None,
    is_additional_holder: bool,
    comprobante_seen: dict[str, tuple[int, int]],
    comprobante_repeats: list[tuple[int, int]],
    installment_failures: list[tuple[int, int]],
) -> Movement:
    tokens = list(row.tokens)
    when = _parse_date_token(tokens[0].text, row.page, row.row)
    rest = tokens[1:]
    if not rest:
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: the row has no fields after its date"
        )
    amount_token = rest[-1]
    if not _is_amount_token(amount_token.text):
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: the row has no trailing amount"
        )
    body = rest[:-1]
    if body and body[-1].text == "$":
        body = body[:-1]
    if not body:
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: the row has no comprobante before its amount"
        )
    comprobante_token = body[-1]
    if _COMPROBANTE_RE.fullmatch(comprobante_token.text) is None:
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: the row's comprobante is not six digits"
        )
    comprobante = comprobante_token.text
    head = body[:-1]

    installment_number: int | None = None
    installment_total: int | None = None
    if head:
        match = _INSTALLMENT_RE.fullmatch(head[-1].text)
        if match is not None:
            installment_number = int(match.group(1))
            installment_total = int(match.group(2))
            if not (1 <= installment_number <= installment_total and installment_total >= 2):
                installment_failures.append((row.page, row.row))
            head = head[:-1]

    if not head:
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: the row has no description"
        )
    description = " ".join(token.text for token in head)

    currency = _classify_column(amount_token, pesos_edge, dolares_edge, row.page, row.row)
    amount = _parse_amount(amount_token.text, row.page, row.row, "amount")

    if comprobante in comprobante_seen:
        comprobante_repeats.append((row.page, row.row))
    else:
        comprobante_seen[comprobante] = (row.page, row.row)

    return Movement(
        kind=MovementKind.PURCHASE,
        charge_class=None,
        when=when,
        currency=currency,
        amount=amount,
        installment_number=installment_number,
        installment_total=installment_total,
        comprobante=comprobante,
        description=description,
        is_additional_holder=is_additional_holder,
        page=row.page,
        row=row.row,
    )


def _parse_payment_row(
    row: PositionedRow, pesos_edge: float, dolares_edge: float | None
) -> Movement | None:
    folded = tuple(_folded_tokens(row)[:4])
    if folded != _PAYMENT_PESOS_PREFIX and folded != _PAYMENT_USD_PREFIX:
        return None
    rest = list(row.tokens)[4:]
    if not rest:
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: a payment row carries no amount"
        )
    amount_token = rest[-1]
    if not _is_amount_token(amount_token.text):
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: a payment row's trailing field is not an amount"
        )
    if len(rest) > 1:
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: a payment row carries unexpected extra fields"
        )
    currency = _classify_column(amount_token, pesos_edge, dolares_edge, row.page, row.row)
    amount = _parse_amount(amount_token.text, row.page, row.row, "amount")
    return Movement(
        kind=MovementKind.PAYMENT,
        charge_class=None,
        when=None,
        currency=currency,
        amount=amount,
        installment_number=None,
        installment_total=None,
        comprobante="",
        description="",
        is_additional_holder=False,
        page=row.page,
        row=row.row,
    )


def _parse_payments_block(
    rows: Sequence[PositionedRow], index: int, pesos_edge: float, dolares_edge: float | None
) -> tuple[list[Movement], int]:
    movements: list[Movement] = []
    while index < len(rows):
        movement = _parse_payment_row(rows[index], pesos_edge, dolares_edge)
        if movement is None:
            break
        movements.append(movement)
        index += 1
    return movements, index


def _classify_charge_row(
    row: PositionedRow, pesos_edge: float, dolares_edge: float | None
) -> Movement | None:
    """Classify a post-total charge row **by shape**; ``None`` means "not a charge row"."""
    tokens = list(row.tokens)
    if not tokens:
        return None
    amount_token = tokens[-1]
    if not _is_amount_token(amount_token.text):
        return None
    head = tokens[:-1]
    if head and head[-1].text == "$":
        head = head[:-1]
    if not head:
        return None
    label = " ".join(token.text for token in head)
    folded_label = fold(label)
    if folded_label.startswith("iibb percep-") or _DB_RG_RE.match(folded_label) is not None:
        charge_class = ChargeClass.PERCEPCION
    elif _IVA_RG_RE.match(folded_label) is not None:
        charge_class = ChargeClass.IVA
    else:
        return None
    currency = _classify_column(amount_token, pesos_edge, dolares_edge, row.page, row.row)
    amount = _parse_amount(amount_token.text, row.page, row.row, "amount")
    return Movement(
        kind=MovementKind.CHARGE,
        charge_class=charge_class,
        when=None,
        currency=currency,
        amount=amount,
        installment_number=None,
        installment_total=None,
        comprobante="",
        description=label,
        is_additional_holder=False,
        page=row.page,
        row=row.row,
    )


def _parse_consumption_section(
    rows: Sequence[PositionedRow],
    index: int,
    pesos_edge: float,
    dolares_edge: float | None,
    is_additional_holder: bool,
    comprobante_seen: dict[str, tuple[int, int]],
    comprobante_repeats: list[tuple[int, int]],
    installment_failures: list[tuple[int, int]],
) -> tuple[list[Movement], tuple[Decimal, Decimal | None], int]:
    movements: list[Movement] = []
    while index < len(rows):
        row = rows[index]
        if _folded_prefix(row, 2) == ("total", "consumos"):
            printed = _row_two_column_amounts(row, pesos_edge, dolares_edge, "TOTAL CONSUMOS")
            return movements, printed, index + 1
        if not row.tokens or _DATE_RE.match(row.tokens[0].text) is None:
            raise CardLiquidacionParseError(
                f"page {row.page}, row {row.row}: an unrecognized line appears inside a "
                f"consumption section"
            )
        movements.append(
            _parse_purchase_row(
                row,
                pesos_edge,
                dolares_edge,
                is_additional_holder,
                comprobante_seen,
                comprobante_repeats,
                installment_failures,
            )
        )
        index += 1
    raise CardLiquidacionParseError("a consumption section never reaches its TOTAL CONSUMOS line")


def _parse_charges_section(
    rows: Sequence[PositionedRow], index: int, pesos_edge: float, dolares_edge: float | None
) -> tuple[list[Movement], int]:
    if index < len(rows) and _is_charges_heading(rows[index]):
        index += 1
    movements: list[Movement] = []
    while index < len(rows):
        row = rows[index]
        if _is_saldo_actual(row):
            return movements, index
        parsed = _classify_charge_row(row, pesos_edge, dolares_edge)
        if parsed is None:
            raise CardLiquidacionParseError(
                f"page {row.page}, row {row.row}: an unrecognized line appears inside the "
                f"charges section"
            )
        movements.append(parsed)
        index += 1
    raise CardLiquidacionParseError("the document ends before the detail SALDO ACTUAL line")


# --------------------------------------------------------------------- reconciliation


def _reconcile(
    movements: Sequence[Movement],
    section_totals: Sequence[tuple[Decimal, Decimal | None, Decimal, Decimal]],
    opening_ars: Decimal,
    opening_usd: Decimal | None,
    summary_ars: Decimal,
    summary_usd: Decimal | None,
    detail_ars: Decimal,
    detail_usd: Decimal | None,
    comprobante_repeats: Sequence[tuple[int, int]],
    installment_failures: Sequence[tuple[int, int]],
) -> tuple[CheckResult, ...]:
    checks: list[CheckResult] = []

    ars_ok = all(
        printed_ars == computed_ars for printed_ars, _pu, computed_ars, _cu in section_totals
    )
    checks.append(
        CheckResult(
            "section-sums-ars",
            ars_ok,
            f"the ARS sum of every consumption section's movements matches its printed "
            f"TOTAL CONSUMOS ({len(section_totals)} section(s))"
            if ars_ok
            else f"at least one of {len(section_totals)} consumption section(s) has an ARS "
            f"sum that does not match its printed TOTAL CONSUMOS",
        )
    )

    usd_ok = all(
        (printed_usd if printed_usd is not None else Decimal(0)) == computed_usd
        for _pa, printed_usd, _ca, computed_usd in section_totals
    )
    checks.append(
        CheckResult(
            "section-sums-usd",
            usd_ok,
            f"the USD sum of every consumption section's movements matches its printed "
            f"TOTAL CONSUMOS ({len(section_totals)} section(s))"
            if usd_ok
            else f"at least one of {len(section_totals)} consumption section(s) has a USD "
            f"sum that does not match its printed TOTAL CONSUMOS",
        )
    )

    ars_sum = sum((m.amount for m in movements if m.currency is Currency.ARS), Decimal(0))
    balance_ars_ok = opening_ars + ars_sum == detail_ars
    checks.append(
        CheckResult(
            "balance-ars",
            balance_ars_ok,
            "opening plus every ARS movement equals the detail SALDO ACTUAL"
            if balance_ars_ok
            else "opening plus every ARS movement does not equal the detail SALDO ACTUAL",
        )
    )

    usd_sum = sum((m.amount for m in movements if m.currency is Currency.USD), Decimal(0))
    expected_detail_usd = detail_usd if detail_usd is not None else Decimal(0)
    balance_usd_ok = (
        opening_usd if opening_usd is not None else Decimal(0)
    ) + usd_sum == expected_detail_usd
    checks.append(
        CheckResult(
            "balance-usd",
            balance_usd_ok,
            "opening plus every USD movement equals the detail SALDO ACTUAL"
            if balance_usd_ok
            else "opening plus every USD movement does not equal the detail SALDO ACTUAL",
        )
    )

    agreement_ok = summary_ars == detail_ars and (
        (summary_usd if summary_usd is not None else Decimal(0))
        == (detail_usd if detail_usd is not None else Decimal(0))
    )
    checks.append(
        CheckResult(
            "summary-detail-agreement",
            agreement_ok,
            "the summary box's SALDO ACTUAL agrees with the detail's"
            if agreement_ok
            else "the summary box's SALDO ACTUAL disagrees with the detail's",
        )
    )

    checks.append(
        CheckResult(
            "comprobante-unique",
            not comprobante_repeats,
            f"{len(movements)} movement(s) carry unique comprobantes"
            if not comprobante_repeats
            else f"{len(comprobante_repeats)} repeated comprobante(s); first repeat at page "
            f"{comprobante_repeats[0][0]} row {comprobante_repeats[0][1]}",
        )
    )

    checks.append(
        CheckResult(
            "installment-valid",
            not installment_failures,
            "every installment marker satisfies 1 <= NN <= TOT and TOT >= 2"
            if not installment_failures
            else f"{len(installment_failures)} installment marker(s) fall outside 1..TOT or "
            f"carry TOT < 2; first at page {installment_failures[0][0]} row "
            f"{installment_failures[0][1]}",
        )
    )

    return tuple(checks)


def parse_card_liquidacion(rows: Sequence[PositionedRow]) -> CardLiquidacion:
    """Parse and reconcile one extracted BBVA card ``Liquidación`` (Visa or Mastercard).

    Raises:
        CardLiquidacionParseError: the rows are not a parsable card
            liquidación -- see the class docstring. The message is masked.
        ReconciliationError: the document parsed but a check failed. The
            error carries every check and the failed subset; nothing partial
            is returned.
    """
    rows = tuple(rows)
    if not rows:
        raise CardLiquidacionParseError("the document is empty")

    brand = _find_brand(rows)
    pesos_edge, dolares_edge = _find_column_edges(rows)

    cierre_index = _find_row(rows, _is_cierre_actual)
    if cierre_index is None:
        raise CardLiquidacionParseError("no CIERRE ACTUAL row was found")
    close_date = _parse_cierre_actual(rows[cierre_index])

    summary_index = _find_row(rows, _is_saldo_actual, start=cierre_index + 1)
    if summary_index is None:
        raise CardLiquidacionParseError("no summary SALDO ACTUAL row was found")
    summary_ars, summary_usd = _row_two_column_amounts(
        rows[summary_index], pesos_edge, dolares_edge, "summary SALDO ACTUAL"
    )

    anterior_index = _find_row(rows, _is_saldo_anterior, start=summary_index + 1)
    if anterior_index is None:
        raise CardLiquidacionParseError("no SALDO ANTERIOR row was found")
    opening_ars, opening_usd = _row_two_column_amounts(
        rows[anterior_index], pesos_edge, dolares_edge, "SALDO ANTERIOR"
    )

    movements: list[Movement] = []
    payments, index = _parse_payments_block(rows, anterior_index + 1, pesos_edge, dolares_edge)
    movements.extend(payments)

    comprobante_seen: dict[str, tuple[int, int]] = {}
    comprobante_repeats: list[tuple[int, int]] = []
    installment_failures: list[tuple[int, int]] = []
    section_totals: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = []

    section_index = 0
    detail_ars: Decimal | None = None
    detail_usd: Decimal | None = None
    while True:
        if index >= len(rows):
            raise CardLiquidacionParseError(
                "the document ends before a detail SALDO ACTUAL row is found"
            )
        row = rows[index]
        if _is_detail_header(row):
            index += 1
            section_movements, printed_total, index = _parse_consumption_section(
                rows,
                index,
                pesos_edge,
                dolares_edge,
                section_index >= 1,
                comprobante_seen,
                comprobante_repeats,
                installment_failures,
            )
            movements.extend(section_movements)
            computed_ars = sum(
                (m.amount for m in section_movements if m.currency is Currency.ARS), Decimal(0)
            )
            computed_usd = sum(
                (m.amount for m in section_movements if m.currency is Currency.USD), Decimal(0)
            )
            section_totals.append((printed_total[0], printed_total[1], computed_ars, computed_usd))
            section_index += 1
            continue
        if _is_saldo_actual(row):
            detail_ars, detail_usd = _row_two_column_amounts(
                row, pesos_edge, dolares_edge, "detail SALDO ACTUAL"
            )
            break
        if section_index == 0:
            raise CardLiquidacionParseError("no consumption section (detail header) was found")
        charge_movements, index = _parse_charges_section(rows, index, pesos_edge, dolares_edge)
        movements.extend(charge_movements)

    checks = _reconcile(
        movements=movements,
        section_totals=section_totals,
        opening_ars=opening_ars,
        opening_usd=opening_usd,
        summary_ars=summary_ars,
        summary_usd=summary_usd,
        detail_ars=detail_ars,
        detail_usd=detail_usd,
        comprobante_repeats=comprobante_repeats,
        installment_failures=installment_failures,
    )
    if any(not check.ok for check in checks):
        raise ReconciliationError(checks)

    return CardLiquidacion(
        brand=brand,
        close_date=close_date,
        opening_ars=opening_ars,
        opening_usd=opening_usd,
        summary_saldo_actual_ars=summary_ars,
        summary_saldo_actual_usd=summary_usd,
        detail_saldo_actual_ars=detail_ars,
        detail_saldo_actual_usd=detail_usd,
        movements=tuple(movements),
        checks=checks,
    )
