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

Column geometry is used **only** where it is the sole signal available: a
single-amount movement row (a purchase, a payment or a charge), where nothing
else on the row says which currency it is. A "totals" row that always prints
its ARS figure before its USD figure (``SALDO ANTERIOR``, a section's
``TOTAL CONSUMOS`` and the closing ``SALDO ACTUAL``) is read by **reading
order** instead (:func:`_row_ordered_amounts`): the first amount-shaped token
is ARS, the second -- if present -- is USD. This is deliberately *not*
column-based, because the T-07d real-file probe found that the **summary
box**'s own totals rows sit at different x positions than the detail's (the
edges are still derived from the detail header only, never from the summary
box), so trusting reading order for these two-amount rows is both simpler and
safer than trusting geometry where geometry is not needed.

An amount printed to the **left** of the columns -- an inline original
foreign-currency amount or a perception's base, both observed in the real
document -- is never mistaken for the movement: only a single-amount row's
**last** token is ever read as the movement amount (see "Row grammar" below),
so an earlier amount-shaped token is always kept as plain description text.

Document order
---------------

The real document (T-07d real-file probe, 2026-09-27) has a **summary box**
(page 2 in the real files) whose only load-bearing rows are the close date,
the opening balance and its own closing balance; everything else in it is
furniture this parser skips without reading, followed by a **detail**
section that carries every actual, dated movement::

    (brand marker: VISA or MASTERCARD, anywhere)
    CIERRE ACTUAL
    <dd-mmm-yy>                                  -- the close date (next row)
    ... summary-box furniture: VENCIMIENTO ACTUAL date, a *label-only*
        "SALDO ACTUAL $"/"SALDO ACTUAL U$S" pair whose values sit on the
        FOLLOWING row each, PAGO MÍNIMO, limits ...
    Pesos Dólares                                -- a mini header (no FECHA)
    SALDO ANTERIOR <ARS> [<USD>]                 -- opening balances (one row)
    ... more summary-box furniture: an undated restatement of the payments,
        each section's TOTAL CONSUMOS and (Visa) the charge rows, none of
        which this parser reads ...
    SALDO ACTUAL <ARS> [<USD>]                   -- the summary figure (one
                                                     row, both amounts on it --
                                                     what distinguishes it from
                                                     the earlier label-only pair)
    ... more furniture: rate rows and a handful of other closing/due-date
        labels, each followed by its own date on the next row ...
    DETALLE
    ... a furniture line ...
    FECHA ... NRO. ... PESOS [DÓLARES]           -- detail header (payments)
    <dd-mmm-yy> SU PAGO EN PESOS <amount>        -- dated; zero or more
    <dd-mmm-yy> SU PAGO EN USD <amount>          --   payment rows
    Consumos <name>                              -- section title (skipped)
    FECHA ... NRO. ... PESOS [DÓLARES]           -- detail header: opens a
      <consumption rows>                            consumption section
    TOTAL CONSUMOS DE <name> <ARS> [<USD>]       -- closes it
    [ Consumos <name>                            -- a second section title
      FECHA ... NRO. ... PESOS [DÓLARES]            (the additional
      <consumption rows>                              cardholder's)
      TOTAL CONSUMOS DE <name> <ARS> [<USD>] ]
    [ Impuestos, cargos e intereses              -- charges heading
      FECHA ... PESOS [DÓLARES]                     (no NRO. on this one)
      <dd-mmm-yy> <charge row> ]                  -- dated; both optional
    SALDO ACTUAL <ARS> [<USD>]                   -- the detail figure
    ... prose, "cuotas a vencer" ...             -- ignored

Everything before the first detail header, and between a section's
``TOTAL CONSUMOS`` and the next boundary, is scanned leniently: an
unrecognized row there is furniture (mirrors ``bbva.py``'s "outside a
region" leniency) -- this is how the summary box's own undated restatement of
the payments/totals/charges is skipped without ever being parsed: this
parser never looks at it beyond its close date, its opening balance and its
own closing ``SALDO ACTUAL``. A **section title** row (``Consumos <name>``)
is also furniture: its folded first token is ``consumos``, and the name that
follows -- of unknown, variable length -- is never read. **Inside** a
consumption or charges section every row must be a recognized shape, its
section's closing marker, or -- for the charges zone only -- the one
recognized heading line plus its own detail header; anything else refuses
with a masked diagnostic (the Brubank/T-08 lesson: never skip silently). This
is also how the reconnaissance's documented "known unknown" is handled: the
real Visa holder section carries one undated line with a column amount, very
likely a wrapped description. This parser does **not** guess a merge rule for
it -- an undated row inside a consumption section refuses, by the same
general rule.

There are between one and two consumption sections. The **second** one, when
present, is the additional cardholder's -- :attr:`Movement.is_additional_holder`
marks every movement inside it. The holder's own name and the additional
cardholder's are never read: :func:`_row_ordered_amounts` only ever extracts
amount-shaped tokens from a ``TOTAL CONSUMOS DE <name>`` row (and a section
title is skipped by its own leading token only), so the name is never
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

A **charge row** (post-total, **dated**, no comprobante): ``<dd-mmm-yy date>
<description words...> <amount>``, classified **by shape**, never by
trusting a label alone (owner decision, 2026-09-27): ``IIBB PERCEP-*`` or
``DB.RG NNNN`` is a :attr:`ChargeClass.PERCEPCION`; ``IVA RG NNNN`` is a
:attr:`ChargeClass.IVA`; any other shape refuses the document.

A **payment row** (**dated**, in the detail): ``<dd-mmm-yy date> SU PAGO EN
PESOS <amount>`` or ``<dd-mmm-yy date> SU PAGO EN USD <amount>``, kept as a
:attr:`MovementKind.PAYMENT` movement -- **never dropped by this parser**
(the importer drops it, mirroring the Provincia precedent: the extracto owns
the cash movement, so reconciliation here still needs the payment counted).
Amounts are kept **as printed**: a trailing minus is a credit, exactly as
:mod:`expensuchis.importers.provincia_visa` keeps its own payment rows.

Reconciliation
---------------

Every check is computed and reported, even after an earlier one fails, so one
run tells the whole story (see :data:`CHECK_NAMES`): each consumption
section's movement sum, per currency, must equal its own printed
``TOTAL CONSUMOS``; per currency, opening balance plus every movement
(payments, consumption, charges, all signed as printed) must equal the
**detail** ``SALDO ACTUAL``; and the **summary** box's ``SALDO ACTUAL`` must
agree with the **detail**'s.

Token merging
-------------

A real ``pypdfium2`` extraction does not reliably split words apart:
whenever the true gap between two words is under the tokenizer's own
tolerance (or there is genuinely no space character at all -- a name run
straight into the next label, a rate and a base run straight into their
label), the whole run becomes **one token**, and the very same label may
appear merged on one row and split on another within the same document
(T-07d, second real-file correction -- ``CIERREACTUAL``, ``SUPAGOENPESOS``
and ``TOTALCONSUMOSDE<name>`` are each observed as a single token, while
``SALDO``/``ANTERIOR`` and ``Impuestos,``/``cargos``/``e``/``intereses``
stay split). Every label and charge shape in this module is therefore
matched on the row's **despaced** text (:func:`_row_despaced`,
:func:`_row_starts_with`) -- fold, then drop every space -- never on exact
token equality or a fixed token count: a label match never depends on how
many tokens it happened to span. The one place that still trusts an exact,
isolated token is the detail header's ``PESOS``/``DÓLARES`` marker used to
read the column edges (:func:`_find_column_edges`) -- every real occurrence
observed keeps that token clean, so reading its own ``x1`` stays exact
rather than an approximation from a merged run.

Because a label's token boundary is no longer trusted, a payment row's
grammar is slightly more permissive than before: anything between the
matched ``SU PAGO EN *`` label and the row's trailing amount is silently
absorbed rather than raising on "unexpected extra fields" (no such content
has been observed, and the amount is still unambiguously the row's last
token either way).

Page furniture
---------------

Every real page carries two more kinds of furniture the reconnaissance had
not sampled until the fourth pass:

* a **vertical right-margin run**, roughly 64 single-character tokens per
  page starting at x0≈579 -- well past the DÓLARES edge (x1≈574) that no
  real amount ever reaches. :func:`_strip_margin_tokens` drops every token
  whose ``x0`` is at or past the document's own derived DÓLARES (or PESOS,
  when there is no DÓLARES column) edge plus :data:`_MARGIN_TOLERANCE`, once,
  right after the column edges are derived and before any other row is
  read. A row left with no tokens after the drop is dropped whole. This
  runs at the parser level, deliberately: :func:`expensuchis.importers.pdf.read_pdf_rows`
  stays a generic primitive with no notion of what any statement's margin
  looks like.
* a **page/of counter** row (``_is_page_counter``, :data:`_PAGE_COUNTER_RE`),
  repeated at the top of every page and never confined to a boundary
  between sections -- it can just as well land in the middle of a
  consumption section (a page break mid-table) as between two of them, so it
  is skipped everywhere a page-counter check would otherwise have to raise:
  the top-level section loop, inside a consumption section, and inside the
  charges section. It is matched by **shape**, exactly like a charge row,
  never merely by position or by appearing "between" two other rows -- a
  row that merely *looks* like it might be page furniture but does not
  match the counter's shape is still an unrecognized line and still
  refuses.

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

The document order above is now confirmed against both real files (T-07d,
2026-09-27); the assumptions still open are narrower:

* ``CIERRE ACTUAL`` is followed on the **next row** by a ``dd-mmm-yy`` date
  (confirmed); this parser does not tolerate the date sharing the label's
  own row, since that shape has not been observed.
* Reading order (not geometry) is trusted for ``SALDO ANTERIOR``, a
  section's ``TOTAL CONSUMOS`` and both ``SALDO ACTUAL`` rows (ARS token
  first, USD token second when present) -- confirmed for the amounts
  themselves, but the exact x position of the **summary box**'s own
  totals rows relative to the detail-derived column edges is not: if a
  future statement prints a single-amount movement row inside the summary
  box (none is currently read there), this parser would need to re-derive
  edges for that zone rather than reusing the detail's.
  This risk does not affect the fixtures or checks above, because the
  summary box's own movement-shaped rows (payments, charges) are never
  parsed by this module -- they are furniture, skipped in full.
* The real Visa file's ``SU PAGO EN USD`` row was observed, in **flat**
  text, glued to the following page's footer; positioned rows (grouped by
  measured y, not by line breaks) are expected not to reproduce that
  artifact, but this parser does not defend against a positioned row that
  is unexpectedly split mid-row by a coincidental y collision -- such a
  split would surface as an ordinary structural refusal (a payment row
  missing its trailing amount), never a silent merge guess.
* The one recognized charges-section heading is a line containing the word
  ``impuestos``; any other heading text is unrecognized and refuses. The
  charges section's own detail header (required right after the heading)
  never carries a ``NRO.`` token, unlike every other detail header in the
  document; this parser does not require its absence, only tolerates it.
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

#: The margin past the detail header's own DÓLARES (or PESOS, if there is no
#: DÓLARES column) right edge, beyond which a token is page furniture, never
#: real data. Confirmed against the real files (T-07d, fourth pass): every
#: page carries a vertical run of right-margin text starting at x0≈579, well
#: past the DÓLARES edge (x1≈574), and no real data token's own x0 ever
#: reaches that far -- see :func:`_strip_margin_tokens`.
_MARGIN_TOLERANCE = 2.0

_AMOUNT_TOKEN_RE = re.compile(r"^-?\$?\d{1,3}(?:\.\d{3})*,\d{2}-?$")
_COMPROBANTE_RE = re.compile(r"^\d{6}$")
_INSTALLMENT_RE = re.compile(r"^C\.(\d{1,2})/(\d{1,2})$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(?P<day>\d{1,2})-(?P<month>[^\s\d-]+)-(?P<year>\d{2})$")
#: Charge-shape prefixes, matched on **despaced** text (see ``_row_despaced``):
#: a real ``pypdfium2`` extraction glues a charge row's label, rate and base
#: into one token with no space at all (T-07d second real-file correction),
#: so these never require a ``\s+`` between the label and its digits.
_DB_RG_RE = re.compile(r"^db\.rg\d")
_IVA_RG_RE = re.compile(r"^ivarg\d")
#: A page/of counter, repeated at the top of every page: a document-word
#: label, a parenthesized run of digits, a ``NdeM`` page count, a slash, and
#: a second label with its own ``NdeM`` count -- one merged token, on its
#: own row, wherever a page break falls (T-07d, fourth pass). Matched on
#: despaced, folded text, so accented letters are already stripped by
#: :func:`fold` before this ever runs.
_PAGE_COUNTER_RE = re.compile(r"^[a-z]+\(\d{5,}\)\d+de\d+/[a-z]+\d+de\d+$")

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
    ``PESOS``. Every one of the three is required: BBVA's own ``Extracto
    consolidado`` (:func:`expensuchis.importers.bbva.is_bbva_extracto`)
    carries ``SALDO ANTERIOR`` too but never ``CIERRE ACTUAL`` or a
    ``PESOS``-carrying header, and Banco Provincia's card liquidación
    (:mod:`expensuchis.importers.provincia_visa`) carries ``CIERRE`` but
    never ``CIERRE ACTUAL`` as adjacent words.

    The ``CIERRE ACTUAL``/``SALDO ANTERIOR`` checks are **whitespace-
    insensitive** (space stripped from the folded line before comparing):
    ``pypdfium2``'s flat-text extraction, like its positioned rows, can glue
    two words together with no space between them whenever the real
    character gap is too small (T-07d, second real-file correction), so a
    literal ``"cierre actual"`` substring is not reliable.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    has_cierre_actual = any("cierreactual" in fold(line).replace(" ", "") for line in lines)
    has_saldo_anterior = any("saldoanterior" in fold(line).replace(" ", "") for line in lines)
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

    ``when`` is populated for every kind: a purchase's date comes from its
    own row, and -- confirmed against the real files (T-07d) -- so do a
    payment's and a charge's. ``comprobante`` is the empty string for
    :attr:`MovementKind.PAYMENT` and :attr:`MovementKind.CHARGE` (neither
    carries one); ``installment_number``/``_total`` are ``None`` unless the
    row carried a ``C.NN/NN`` marker. ``is_additional_holder`` is ``True``
    only for a movement inside the **second** consumption section -- the
    additional cardholder's -- and is always ``False`` for a payment or a
    charge. The cardholder's own name is never read, let alone stored here.
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
    the ``CIERRE ACTUAL`` row (and the date on its following row),
    ``SALDO ANTERIOR``, the summary or detail ``SALDO ACTUAL`` line, the
    detail header (and so the column edges), a consumption section's
    ``TOTAL CONSUMOS`` line, or the charges section's own header is missing
    or malformed; a row inside a section matches no recognized shape; an
    amount is malformed; or an amount's right edge lands near neither derived
    column edge (or near both). The message never echoes statement text.
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


def _row_despaced(row: PositionedRow) -> str:
    """Return the row's tokens, folded and concatenated with **no separator**.

    The real ``pypdfium2`` extraction sometimes glues two or more words into
    one token (no space character, and a gap under the token-splitting
    tolerance) and sometimes keeps them apart -- both were observed for
    different labels in the same document (T-07d, second real-file
    correction). Matching a label against this despaced concatenation, never
    against exact per-token equality, is what makes the match tolerant to
    either shape.
    """
    return "".join(fold(token.text) for token in row.tokens)


def _row_starts_with(row: PositionedRow, label: str) -> bool:
    """Whether the row's despaced text starts with ``label`` (itself despaced, folded).

    Deliberately a prefix check, not equality: a label may run straight into
    trailing content within the very same token with no separator at all --
    ``TOTAL CONSUMOS DE <name>`` is one token in the real file, name included
    -- so this never requires knowing where the label "ends".
    """
    return _row_despaced(row).startswith(fold(label).replace(" ", ""))


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
    """Whether ``row`` carries both a ``FECHA`` and a ``PESOS`` marker.

    A substring check on the despaced row, not a per-token lookup: ``NRO.``
    is sometimes glued to the word right after it (``NRO.aaaaa`` one token),
    so a set-of-exact-tokens check would miss it if ``FECHA``/``PESOS`` ever
    merged the same way. Neither is observed to merge in the real files, but
    the check costs nothing extra and stays consistent with every other
    marker in this module.
    """
    despaced = _row_despaced(row)
    return "fecha" in despaced and "pesos" in despaced


def _is_saldo_anterior(row: PositionedRow) -> bool:
    return _row_starts_with(row, "SALDO ANTERIOR")


def _is_saldo_actual(row: PositionedRow) -> bool:
    """Whether ``row`` is a ``SALDO ACTUAL`` row that carries its own amount.

    The summary box prints ``SALDO ACTUAL $``/``SALDO ACTUAL U$S`` as two
    **label-only** rows (their values sit on the row right after each), then
    later a **combined** ``SALDO ACTUAL <ars> [<usd>]`` row with both amounts
    on itself -- that combined row is the one this parser reads (for the
    summary figure, and again for the detail's own closing row); requiring an
    amount on the row is what tells the two shapes apart without needing to
    know which one comes first.
    """
    return _row_starts_with(row, "SALDO ACTUAL") and any(
        _is_amount_token(token.text) for token in row.tokens
    )


def _is_cierre_actual(row: PositionedRow) -> bool:
    return _row_starts_with(row, "CIERRE ACTUAL")


def _is_charges_heading(row: PositionedRow) -> bool:
    return "impuestos" in _row_despaced(row)


def _is_consumos_title(row: PositionedRow) -> bool:
    """A ``Consumos <name>`` section title -- furniture; the name is never read."""
    return _row_starts_with(row, "Consumos")


def _is_page_counter(row: PositionedRow) -> bool:
    """Whether ``row`` is the repeated page/of counter row -- furniture, everywhere.

    Recognized by shape (:data:`_PAGE_COUNTER_RE`), never skipped by position:
    it recurs at the top of every page, including in the middle of a
    consumption section or between two sections, wherever a page break falls
    (T-07d, fourth pass).
    """
    return _PAGE_COUNTER_RE.match(_row_despaced(row)) is not None


def _strip_margin_tokens(
    rows: Sequence[PositionedRow],
    pesos_edge: float,
    dolares_edge: float | None,
    left_edge: float,
) -> tuple[PositionedRow, ...]:
    """Drop every token outside the detail header's own edges, plus a small margin.

    Right: past the DÓLARES (or PESOS) right edge. Left: ending before the
    detail header's ``FECHA`` token starts -- the real cards also carry a
    vertical left-margin run at x≈29-56 on some pages (T-07d, fifth pass),
    while every real row starts at the ``FECHA`` column (x0≈62) or right of it.

    Every real page carries a vertical run of right-margin text (T-07d,
    fourth pass) that a positioned-row primitive has no way to recognize as
    furniture -- it is not a PDF library's job to know what a BBVA card
    liquidación's own margin looks like, so this runs here, at the parser
    level, over the document's *own* derived edge rather than a hardcoded
    x-position. A row left with no tokens after the drop is dropped whole
    (an empty :class:`PositionedRow` is never produced): this is what removes
    the whole vertical run, since each of its rows carries only margin
    tokens. A row that keeps at least one real token survives with only its
    real content -- this is never observed in the real files (a margin
    token has never shared a row with real data), but nothing here would
    break if it did.
    """
    cutoff = (dolares_edge if dolares_edge is not None else pesos_edge) + _MARGIN_TOLERANCE
    left_cutoff = left_edge - _MARGIN_TOLERANCE
    cleaned: list[PositionedRow] = []
    for row in rows:
        tokens = tuple(
            token for token in row.tokens if token.x0 < cutoff and token.x1 > left_cutoff
        )
        if tokens:
            cleaned.append(PositionedRow(page=row.page, row=row.row, tokens=tokens))
    return tuple(cleaned)


def _parse_cierre_actual_date(rows: Sequence[PositionedRow], cierre_index: int) -> dt.date:
    """Parse the close date from the row right after ``CIERRE ACTUAL``.

    Confirmed against the real files (T-07d): the label and its ``dd-mmm-yy``
    value are on **separate** rows, unlike a consumption row's inline date.
    """
    if cierre_index + 1 >= len(rows):
        raise CardLiquidacionParseError(
            "the CIERRE ACTUAL row has no following row to carry its date"
        )
    date_row = rows[cierre_index + 1]
    if not date_row.tokens or _DATE_RE.match(date_row.tokens[0].text) is None:
        raise CardLiquidacionParseError(
            f"page {date_row.page}, row {date_row.row}: the row after CIERRE ACTUAL carries "
            f"no dd-mmm-yy date"
        )
    return _parse_date_token(date_row.tokens[0].text, date_row.page, date_row.row)


def _find_row(
    rows: Sequence[PositionedRow], predicate: Callable[[PositionedRow], bool], start: int = 0
) -> int | None:
    for index in range(start, len(rows)):
        if predicate(rows[index]):
            return index
    return None


def _find_brand(rows: Sequence[PositionedRow]) -> CardBrand:
    """Find the ``VISA``/``MASTERCARD`` marker, as a despaced substring of any row.

    A substring check, not exact token equality: the brand word can be glued
    to neighbouring text on its own title row (never observed to merge in
    the real files, but the letterhead row was not part of the reconnaissance
    dump either, so this stays defensive rather than assuming it cannot).
    """
    for row in rows:
        despaced = _row_despaced(row)
        if "mastercard" in despaced:
            return CardBrand.MASTERCARD
        if "visa" in despaced:
            return CardBrand.VISA
    raise CardLiquidacionParseError("no VISA/MASTERCARD brand marker was found")


def _find_left_edge(rows: Sequence[PositionedRow]) -> float:
    """Return the ``x0`` of the first detail header's ``FECHA`` token: the left
    edge every real row starts at or right of (see :func:`_strip_margin_tokens`)."""
    for row in rows:
        if not _is_detail_header(row):
            continue
        for token in row.tokens:
            if fold(token.text) == "fecha":
                return token.x0
    raise CardLiquidacionParseError(
        "the detail header row carries no FECHA marker; the left edge cannot be derived"
    )


def _find_column_edges(rows: Sequence[PositionedRow]) -> tuple[float, float | None]:
    """Derive the PESOS/DÓLARES right edges from the document's own detail header.

    The **first** row carrying both a ``FECHA`` and a ``PESOS`` marker is
    always a detail header (the summary box's own ``Pesos Dólares`` mini
    header carries no ``FECHA`` marker, so it never matches -- see the
    module docstring, "Summary-box values sit elsewhere"). The edge itself
    still comes from an **exact** token equal to ``PESOS``/``DÓLARES``: every
    real occurrence observed keeps that token clean and unmerged, unlike the
    labels this module matches by despaced prefix, so reading its own ``x1``
    directly is safe and gives the true edge rather than an approximation.
    """
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


def _row_ordered_amounts(row: PositionedRow, field: str) -> tuple[Decimal, Decimal | None]:
    """Extract the (ARS, USD-or-None) amounts of a ``SALDO``/``TOTAL`` row **by reading order**.

    These "totals" rows always print their ARS figure before their USD
    figure (confirmed by every observed shape, in the summary box and in the
    detail alike), so the first amount-shaped token is ARS and the second,
    when present, is USD -- no column geometry needed, which sidesteps the
    summary box's own amount columns sitting at a different x than the
    detail's (see the module docstring). Non-amount tokens (labels, a
    cardholder's name of unknown length) are silently skipped, never read:
    this is what lets ``TOTAL CONSUMOS DE <name>`` be parsed without ever
    inspecting the name.
    """
    amounts = [token for token in row.tokens if _is_amount_token(token.text)]
    if not amounts or len(amounts) > 2:
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: the {field} row does not carry one or two amounts"
        )
    ars = _parse_amount(amounts[0].text, row.page, row.row, field)
    usd = _parse_amount(amounts[1].text, row.page, row.row, field) if len(amounts) == 2 else None
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
    """Parse a (dated) ``SU PAGO EN PESOS``/``SU PAGO EN USD`` row, or ``None`` if it is neither.

    Confirmed against the real files (T-07d): the detail's payment row is
    dated, unlike the summary box's own undated restatement (which this
    parser never reaches -- see the module docstring). The leading date is
    tolerated but not required, so an undated payment row -- if ever
    observed -- still parses, with ``when=None``.

    The label is matched on the despaced text of everything after the
    optional date (``SUPAGOEN``, common to both currencies -- the real
    document glues the whole label into one token, per the second real-file
    correction). The **currency** still comes from the trailing amount's own
    column, never from which of PESOS/USD the label named: a label glued to
    the amount's own token has never been observed, but trusting geometry
    over text here costs nothing and stays consistent with every other
    movement row.
    """
    tokens = list(row.tokens)
    when: dt.date | None = None
    offset = 0
    if tokens and _DATE_RE.match(tokens[0].text) is not None:
        when = _parse_date_token(tokens[0].text, row.page, row.row)
        offset = 1
    remaining = tokens[offset:]
    if not remaining:
        return None
    despaced = "".join(fold(token.text) for token in remaining)
    if not despaced.startswith("supagoen"):
        return None
    amount_token = remaining[-1]
    if not _is_amount_token(amount_token.text):
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: a payment row carries no trailing amount"
        )
    currency = _classify_column(amount_token, pesos_edge, dolares_edge, row.page, row.row)
    amount = _parse_amount(amount_token.text, row.page, row.row, "amount")
    return Movement(
        kind=MovementKind.PAYMENT,
        charge_class=None,
        when=when,
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


def _parse_charge_row(
    row: PositionedRow, pesos_edge: float, dolares_edge: float | None
) -> Movement | None:
    """Classify a **dated** post-total charge row **by shape**.

    ``None`` means "not a charge row at all" (no leading date, or the shape
    matches no known charge class) -- the caller decides whether that is the
    section's terminator or a genuine refusal.

    The shape is matched on the row's **despaced** head text (every token
    but the trailing amount): the real document glues a charge label, its
    rate and its parenthesized base into one token with no space at all
    (``IIBBPERCEP-CABA9,99%(``, T-07d second real-file correction), so
    ``_DB_RG_RE``/``_IVA_RG_RE`` never require a space before the regime
    number, and the ``IIBB PERCEP-`` prefix is checked despaced too.
    """
    tokens = list(row.tokens)
    if not tokens or _DATE_RE.match(tokens[0].text) is None:
        return None
    when = _parse_date_token(tokens[0].text, row.page, row.row)
    rest = tokens[1:]
    if not rest:
        return None
    amount_token = rest[-1]
    if not _is_amount_token(amount_token.text):
        return None
    head = rest[:-1]
    if head and head[-1].text == "$":
        head = head[:-1]
    if not head:
        return None
    label = " ".join(token.text for token in head)
    despaced_head = "".join(fold(token.text) for token in head)
    if despaced_head.startswith("iibbpercep-") or _DB_RG_RE.match(despaced_head) is not None:
        charge_class = ChargeClass.PERCEPCION
    elif _IVA_RG_RE.match(despaced_head) is not None:
        charge_class = ChargeClass.IVA
    else:
        return None
    currency = _classify_column(amount_token, pesos_edge, dolares_edge, row.page, row.row)
    amount = _parse_amount(amount_token.text, row.page, row.row, "amount")
    return Movement(
        kind=MovementKind.CHARGE,
        charge_class=charge_class,
        when=when,
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
        if _row_starts_with(row, "TOTAL CONSUMOS"):
            printed = _row_ordered_amounts(row, "TOTAL CONSUMOS")
            return movements, printed, index + 1
        if _is_page_counter(row):
            index += 1
            continue
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
    """Parse the charges zone. ``rows[index]`` must already be the recognized heading.

    The heading is followed by the charges section's **own** detail header
    (never carrying ``NRO.``, unlike every other one) before the first dated
    charge row; its absence is a structural refusal, not silent tolerance.
    """
    index += 1  # the heading itself
    if index >= len(rows) or not _is_detail_header(rows[index]):
        raise CardLiquidacionParseError(
            "the charges section heading is not followed by its own detail header"
        )
    index += 1  # the charges section's header
    movements: list[Movement] = []
    while index < len(rows):
        row = rows[index]
        if _is_saldo_actual(row):
            return movements, index
        if _is_page_counter(row):
            index += 1
            continue
        parsed = _parse_charge_row(row, pesos_edge, dolares_edge)
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
    rows = _strip_margin_tokens(rows, pesos_edge, dolares_edge, _find_left_edge(rows))

    cierre_index = _find_row(rows, _is_cierre_actual)
    if cierre_index is None:
        raise CardLiquidacionParseError("no CIERRE ACTUAL row was found")
    close_date = _parse_cierre_actual_date(rows, cierre_index)

    anterior_index = _find_row(rows, _is_saldo_anterior, start=cierre_index + 1)
    if anterior_index is None:
        raise CardLiquidacionParseError("no SALDO ANTERIOR row was found")
    opening_ars, opening_usd = _row_ordered_amounts(rows[anterior_index], "SALDO ANTERIOR")

    summary_index = _find_row(rows, _is_saldo_actual, start=anterior_index + 1)
    if summary_index is None:
        raise CardLiquidacionParseError("no summary SALDO ACTUAL row was found")
    summary_ars, summary_usd = _row_ordered_amounts(rows[summary_index], "summary SALDO ACTUAL")

    detail_header_index = _find_row(rows, _is_detail_header, start=summary_index + 1)
    if detail_header_index is None:
        raise CardLiquidacionParseError(
            "no detail section (FECHA ... PESOS [DOLARES] header) was found after the summary box"
        )
    index = detail_header_index + 1

    movements: list[Movement] = []
    payments, index = _parse_payments_block(rows, index, pesos_edge, dolares_edge)
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
        if _is_consumos_title(row):
            index += 1
            continue
        if _is_page_counter(row):
            index += 1
            continue
        if _is_charges_heading(row):
            charge_movements, index = _parse_charges_section(rows, index, pesos_edge, dolares_edge)
            movements.extend(charge_movements)
            continue
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
            detail_ars, detail_usd = _row_ordered_amounts(row, "detail SALDO ACTUAL")
            break
        raise CardLiquidacionParseError(
            f"page {row.page}, row {row.row}: an unrecognized line appears between "
            f"consumption sections"
        )

    if section_index == 0:
        raise CardLiquidacionParseError("no consumption section (detail header) was found")

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
