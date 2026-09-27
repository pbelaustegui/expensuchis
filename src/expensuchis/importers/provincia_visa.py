"""Parser for Banco Provincia's ``Liquidación Visa`` card statement.

Pure over extracted text: no PDF library, no filesystem, no clock and no
environment. The caller (the importer's ``extract``, in a later task) owns text
extraction via :func:`expensuchis.importers.pdf.read_pdf`; this module owns
meaning. Pages arrive joined with :data:`expensuchis.importers.pdf.PAGE_SEPARATOR`
and the layout below splits on it.

The parser is a **gate**, not a reader. :func:`parse_liquidacion` reconciles the
document against itself and raises :class:`ReconciliationError` whenever any check
fails; it never returns a partially trusted result. There is no ``strict=False``:
the caller cannot opt out of a check it does not like.

What the document looks like
----------------------------

A monthly card statement (``Liquidación Visa``) is Argentine comma-decimal
(``1.234.567,89``, ``AmountFormat.ARS``), carries **no running balance**, and its
sign is inverted relative to the account: a purchase grows the card liability, so
amounts are kept as printed (positive purchases), and the *posting* side is the
importer's job, not this module's.

Page 1 opens with a **letterhead block of exactly ten lines**, repeated at the top
of every page. It is stationery, not data: only its ``(NNNN)``-shaped first token
(the bank code in parentheses) identifies it, and the ten lines starting there are
skipped wholesale wherever the block is still open. One of its middle lines carries
the period — ``… CIERRE <dd> <Mes> <yy> VENCIMIENTO <dd> <Mes> <yy>`` — which is the
only thing the letterhead is mined for: the closing date (``CIERRE``) gives the
statement's own year and month, the due date (``VENCIMIENTO``) gives the year and
month the importer derives first due dates from. The letterhead's wording is
otherwise unknown by design, so nothing else inside it is parsed.

After the letterhead, the opening anchor: ``SALDO ANTERIOR <ARS> <USD>`` — two
amounts, the opening balances, not a movement.

Then the **payment block**: rows whose description folds to ``SU PAGO EN PESOS``, in
exactly two shapes. A **payment row** is the credit itself — ``<yy> <Mes> <dd> SU
PAGO EN PESOS <amount>`` with the amount **as printed** (a trailing minus is a
credit, and ``parse_amount`` already keeps it), optionally followed by a literal
``USD`` token and its USD amount; more than one non-USD amount on a payment row
refuses the document. A **balance row** restates the block's balances — ``<dd> SU
PAGO EN PESOS <previous ARS> TC<rate> <resulting ARS-> <resulting USD->`` — and is
identified **structurally** by its ``TC<rate>`` token, never by position: within the
block, a row carrying a ``TC<rate>`` token is the balance row and a row without one
is a payment row. There are zero or more payment rows; **more than one balance row
is an unobserved shape and refuses the document**. The rate token's real shape is
``TC`` + a dot-grouped (or plain) integer + a comma with one to four decimals
(three on the real document), and it is parsed by a small dedicated parser — **not**
:func:`expensuchis.numbers.parse_amount`, which correctly refuses non-two-decimal
money. These rows are parsed, reconciled and kept for the importer, and they are
**never a consumption and never an expense** — the importer must not post them
through the counterparty map. The ``balance-chain`` check proves the payment row
and the balance row belong to one chain and that no row was dropped; it compares
**magnitudes** (the statement's balance signs are not yet understood and nothing in
this block is posted): ``|opening ARS| - |sum(payment ARS)| == |resulting ARS|``,
``|opening USD| - |sum(payment USD)| == |resulting USD|`` and
``|previous ARS| == |opening ARS|``. The block is closed by the first line that is
not a payment or balance row.

A **rule line** (at least forty characters, all non-alphanumeric — the real file
has 112 underscores) separates the payment block from the movements.

Then **N to many charge rows** (consumptions). The month is drawn once per group as
a merged cell — ``<yy> <Mes>`` before the day of the group's first row — and every
following row of the group carries only the day, so the parser carries the current
``(yy, Mes)`` forward and stamps each row with it. The token before the comprobante
is the **day** and the merged cell's leading token is the **year** (two digits, so
``20xx``); swapping them would corrupt every date, so the shapes are pinned by the
tests. A row reads ``[<yy> <Mes>] <day> <comprobante: 6 digits> <description…>
[C.NN/NN] <amount>``: the description may be several words, may carry ``*``
(processor prefixes such as ``PAGOAPP*``) and may be preceded by a lone marker
token or a ``*`` — all of which stays in the verbatim description, because nothing
downstream needs to split it. ``C.NN/NN`` is the installment plan marker. A row ends
with an **ARS amount**; a **USD-only row** ends with the *same value printed
twice* (once in the ``importe`` column and once in the ``dólares`` column) and is
kept as **one USD amount** with ``amount_ars=None``. A row that wraps onto two
lines has never been observed: a day-led, comprobante-carrying line without a
trailing amount refuses the document instead of being dropped or guessed.

The **total row** folds to contain ``TOTAL CONSUMOS`` and carries two amounts, the
ARS and USD figures the charges must sum to (the real file decorates them with
footnote ``*`` characters, which are not amounts).

After the total, more **charge rows** appear (taxes and regimes). They are
classified **by shape, never by trusting a label alone**:

* ``IMPUESTO DE SELLOS`` in ARS (``… $ <amount>``) or in USD (``… USD <amount>``)
  is a :attr:`SurchargeKind.SELLOS`;
* any other charge row carrying a ``%`` rate and a parenthesized base before its
  amount (``IIBB <words> <rate>%( <base>) <amount>``, ``IVA RG 9999 <rate>%
  ( <base> ) <amount>``) is a :attr:`SurchargeKind.PERCEPCION` — the four-digit
  regime number is **optional** (the real IIBB row carries none), and the rate may
  be glued to its opening parenthesis;
* **anything else** fails the ``surcharge-complete`` check and refuses the
  document. A future regime must be added deliberately; a charge row must never
  silently become an expense or be skipped.

The payment coupons later in the document are the reason classification stays
shape-based: they carry six-digit numbers in other columns, eighteen-digit
barcodes and amounts, so neither "the line has a six-digit token" nor "the line
has an amount" identifies a movement. The post-total block is closed by the first
line that matches **no charge marker at all** (prose and the letterhead flow past
it silently); anything that matches a marker and cannot be parsed as a charge —
an unrecognized line ending in an amount-shaped token (a prose line does not end
in an amount; a charge row always does), a perception pattern without its
amount, or a malformed parenthesis pattern (an unmatched ``(`` or ``)``) — fails
``surcharge-complete``; the scanner never re-enters.

Pagination
----------

A long statement may continue the charge rows on a later page, in which case the
ten-line letterhead reappears *inside* the open block. The scanner skips the
repeated block; the block must then resume with a charge row on the first
non-blank line, otherwise the document is refused — a bounded window, not an
open-ended search, so a truncated document cannot trail into prose unnoticed.

Masking
-------

The real statements are household data and the models that run this workflow are
remote. **No diagnostic may echo a raw statement line.** Every :class:`CheckResult`
detail and every error message in this module reports page and line numbers,
counts, indexes and field names only — never a date, a description, a comprobante,
a coupon, a rate, an amount or a filename. ``provincia.py`` records why: a crude
refusal message that echoed a statement description cost T-06a two CRITICAL review
findings.

Invalid days
------------

A row whose day is invalid for its month (``31 Febrero``) cannot be represented as
a ``datetime.date``, so it is **excluded** from the result and the ``day-valid``
check fails; the document is refused either way. Excluding the row can also fail a
sum check, which is honest: the run tells the whole story through
:class:`ReconciliationError` and nothing partial is ever returned.

The year rule
-------------

A merged cell's two-digit token is the **purchase** year, and an installment plan
bought in a previous year legitimately appears in a later statement — a ``C.03/12``
plan bought in Noviembre 25 shows up in a statement closing Agosto 26. Equality
with the letterhead's year was therefore rejected as a check: it would refuse every
such document at the first year boundary. The checks that replaced it are true by
construction and still catch a mis-read year: ``periods-ordered`` requires every
group cell — the charge groups' and the post-total rows' — to be **non-decreasing
in (year, month) in document order**, because consumos are listed chronologically
by purchase month; ``periods-window`` bounds every group cell **below** by the
statement's own closing month shifted back by ``max(3, the largest installment
number in the document)`` months, and **above** by the closing month. The
rationale: the current billing month and the purchase month are related by the
installment **number**, not the plan's total — a row at ``C.NN/TOT`` is the NN-th
billing, so its purchase month is at most ``NN - 1`` months before the closing
month; the floor of 3 keeps a plan-less statement windowed at a normal cycle and
keeps a ``C.01/99`` row from widening the window to 99 months. The bound comes
from the statement's own data, never from a clock, and the real document's groups
sit inside it with the deepest group exactly at ``NN - 1`` months back — an
off-by-one in the bound would refuse the real document. And
``date-not-after-closing`` requires every charge, payment and surcharge date to
be **on or before the statement's CIERRE date** — the closing date is the bound at
day granularity, so a row inside the closing month is fine while a mis-read year
(2027 for 2026) or a cell past the closing month refuses.

The window still cannot be perfectly tight, and the case that stays inside it is a
one-year misread of a plan whose installment number is large enough that the
misread year remains within the window — a twelve-plus-installment plan windows
over a year, so a 2024 cell on a 2026 statement can pass when it should have been
2025. That residual is documented here, not hidden: tightening it further would
need a bound the document does not carry, and a false refusal of a legitimate old
plan is worse than a bounded blind spot.

The natural key
---------------

:func:`charge_keys` builds each charge's identity, used as the entry's ``key``
metadata that the pipeline deduplicates on: ``<date>:<comprobante>:<amount>``,
where the amount is the ARS amount, or the USD amount on a USD-only row. The
comprobante is unique across the document (enforced by the ``comprobante-unique``
check), so — unlike ``provincia.movement_keys`` — no occurrence suffix is needed:
on a reconciled document the three parts are already unique.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ..numbers import AmountFormat, AmountParseError, parse_amount
from .pdf import PAGE_SEPARATOR

__all__ = [
    "CHECK_NAMES",
    "Balance",
    "Charge",
    "CheckResult",
    "Liquidacion",
    "LiquidacionParseError",
    "Payment",
    "ReconciliationError",
    "Surcharge",
    "SurchargeKind",
    "charge_keys",
    "parse_liquidacion",
]

#: The checks, in the fixed order they are reported. Stable strings: a caller may
#: branch on them and a reader may grep for them. The sums are two independent
#: checks (ARS and USD) because the document declares both totals and each can be
#: wrong alone; together with ``block-complete`` and ``surcharge-complete`` they
#: make a dropped, duplicated or mis-shaped row impossible to import silently.
CHECK_NAMES: tuple[str, ...] = (
    "block-complete",
    "charge-sums-ars",
    "charge-sums-usd",
    "comprobante-unique",
    "periods-ordered",
    "periods-window",
    "date-not-after-closing",
    "balance-chain",
    "day-valid",
    "surcharge-complete",
    "installment-valid",
)

#: The letterhead block is the ten lines starting at the ``(NNNN)`` anchor. It is
#: skipped by count, never by content, because its wording is unknown by design.
_LETTERHEAD_LINES = 10

_ANCHOR_RE = re.compile(r"^\(\d{4}\)(?:\s|$)")
#: The letterhead's period line. The two ``Cierre … Fec.:`` lines carry no
#: ``VENCIMIENTO`` and cannot match, so the first match in the block is the period.
_PERIOD_RE = re.compile(
    r"CIERRE\s+(?P<cday>\d{1,2})\s+(?P<cmonth>\S+)\s+(?P<cyear>\d{1,2})\s+"
    r"VENCIMIENTO\s+(?P<vday>\d{1,2})\s+(?P<vmonth>\S+)\s+(?P<vyear>\d{1,2})",
    re.IGNORECASE,
)
#: A strict Argentine amount token: optional ``$``, dot-grouped integer, exactly
#: two comma decimals, optional trailing minus. Strictness is what keeps a
#: four-digit regime number or an eighteen-digit barcode from reading as an amount.
_AMOUNT_TOKEN_RE = re.compile(r"^-?\$?\d{1,3}(?:\.\d{3})*,\d{2}-?$")
_YEAR_TOKEN_RE = re.compile(r"^\d{2}$")
_DAY_TOKEN_RE = re.compile(r"^\d{1,2}$")
_COMPROBANTE_RE = re.compile(r"^\d{6}$")
_INSTALLMENT_RE = re.compile(r"^C\.(\d{1,2})/(\d{1,2})$", re.IGNORECASE)
#: The balance row's rate token: ``TC`` + a dot-grouped (or plain) integer + a comma
#: with one to four decimals (three on the real document). Parsed by :func:`_rate`,
#: a dedicated parser — ``numbers.parse_amount`` correctly refuses non-two-decimal
#: money, and the rate is not money.
_TC_RATE_RE = re.compile(r"^TC(?P<rate>\d{1,3}(?:\.\d{3})*|\d+),(?P<decimals>\d{1,4})$", re.IGNORECASE)
#: A perception row's rate token; the opening parenthesis may be glued to it
#: (``9,99%(``), which both real perception shapes show.
_RATE_RE = re.compile(r"^\d{1,2}(?:[.,]\d+)?%\(?$")

#: The month names of the merged cells and the letterhead's period line: full
#: Spanish names **and** the three-letter abbreviations the real period line uses
#: (``Jul``, ``Ago``, ``Set``, ``Oct`` are the four it shows), plus the Argentine
#: spelling ``setiembre``. Folded, so ``MARZO``, ``Mar`` and ``mar`` resolve alike;
#: the table is shared, so a merged cell and the period line both accept both forms.
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

_SELLOS_FOLDED = "impuesto de sellos"
_PAYMENT_FOLDED = "su pago en pesos"
_TOTAL_FOLDED = "total consumos"


def fold(text: str) -> str:
    """Casefold and strip accents: ``IMPUESTO DE SELLOS`` and ``impuesto`` compare equal."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


class SurchargeKind(Enum):
    """The closed vocabulary of post-total charge rows.

    Classification is by shape, never by trusting a label alone: an unknown shape
    refuses the document (``surcharge-complete``), so a future regime is added
    deliberately instead of silently becoming an expense.
    """

    SELLOS = "sellos"
    PERCEPCION = "percepcion"


@dataclass(frozen=True)
class Charge:
    """One consumption row, stamped with its group's year and month.

    ``description`` is the row's description text with whitespace normalized —
    verbatim words, including any processor prefix and ``*``. A USD-only row (the
    document prints the same value twice on it: the ``importe`` column and the
    ``dólares`` column) keeps **one** ``amount_usd`` and ``amount_ars=None``.
    ``page`` is 1-based, ``line`` is 1-based within the page, for masked
    diagnostics only.
    """

    when: dt.date
    comprobante: str
    description: str
    installment_number: int | None
    installment_total: int | None
    amount_ars: Decimal | None
    amount_usd: Decimal | None
    page: int
    line: int


@dataclass(frozen=True)
class Payment:
    """One ``SU PAGO EN PESOS`` **credit** row — the card payment, **never posted**.

    The amount is as printed: a trailing minus is a credit and is kept, so the
    chain arithmetic in the ``balance-chain`` check works in magnitudes. A payment
    row optionally carries a literal ``USD`` token and its USD amount; more than
    one non-USD amount on the row refuses the document.
    """

    when: dt.date
    amount_ars: Decimal
    amount_usd: Decimal | None
    page: int
    line: int


@dataclass(frozen=True)
class Balance:
    """The payment block's **balance row** — a restatement of the balances, never posted.

    Identified structurally by its ``TC<rate>`` token. It carries the previous ARS
    balance, the peso/dólar rate used, and the resulting ARS and USD balances as
    printed (trailing minuses kept). ``previous_ars`` equals the opening balance in
    magnitude, and the resulting balances are what the payment left — the relations
    the ``balance-chain`` check proves, in magnitudes (the statement's balance signs
    are not yet understood and nothing in this block is posted).
    """

    when: dt.date
    previous_ars: Decimal
    rate: Decimal | None
    resulting_ars: Decimal
    resulting_usd: Decimal
    page: int
    line: int


@dataclass(frozen=True)
class Surcharge:
    """A post-total charge row: a stamp tax or a regime perception.

    ``label`` is the row's leading words, verbatim — narration material for the
    importer. ``when`` is the carried group's year and month with the row's own
    day, when the row carries one; ``None`` otherwise. The parenthesized base of a
    perception is structure (it classifies the row) and is not kept: the posted
    amount is the row's own.
    """

    kind: SurchargeKind
    label: str
    when: dt.date | None
    amount_ars: Decimal | None
    amount_usd: Decimal | None
    page: int
    line: int


@dataclass(frozen=True)
class CheckResult:
    """One reconciliation check and its **masked** detail."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Liquidacion:
    """A fully reconciled card statement. Only :func:`parse_liquidacion` builds one.

    ``period_year`` is the statement's own year (the ``CIERRE`` date's year). The
    ``VENCIMIENTO`` date supplies ``due_year``/``due_month`` — the importer derives
    ``first_due`` as ``due_month`` minus ``(installment_number - 1)`` months — and
    may fall in the next calendar year, so ``closing_year``/``closing_month`` (the
    ``CIERRE`` date) are kept separately.
    """

    period_year: int
    due_year: int
    due_month: int
    closing_year: int
    closing_month: int
    opening_ars: Decimal
    opening_usd: Decimal
    payments: tuple[Payment, ...]
    balance: Balance | None
    charges: tuple[Charge, ...]
    total_ars: Decimal
    total_usd: Decimal
    surcharges: tuple[Surcharge, ...]
    checks: tuple[CheckResult, ...]


class LiquidacionParseError(ValueError):
    """The text is not a parsable ``Liquidación Visa``.

    Structural failure, distinct from :class:`ReconciliationError`: the letterhead
    anchor is missing, the opening balance row or the total row is missing or
    misplaced, the letterhead's CIERRE date is not a real date, a charge row has no
    trailing amount (a wrapped row has never been observed), or an amount is not a
    comma-decimal number. The message never echoes statement text.
    """


class ReconciliationError(RuntimeError):
    """A parsed document failed a reconciliation check.

    Carries **every** check in :attr:`checks` (so one run tells the whole story)
    and the failed subset in :attr:`failures`. The message names the failed checks
    only; the details are masked and live on :attr:`checks`.
    """

    def __init__(self, checks: tuple[CheckResult, ...]) -> None:
        self.checks = checks
        self.failures = tuple(check for check in checks if not check.ok)
        names = ", ".join(check.name for check in self.failures)
        super().__init__(
            f"Banco Provincia card reconciliation failed: {names}. "
            f"Refusing the document; a partially trusted parse is never returned."
        )


def _layout(text: str) -> list[tuple[int, int, str]]:
    """Return ``(page, line, raw_line)`` for every line; ``line`` restarts per page.

    Rows carry their 1-based position within their own page because that is what a
    masked diagnostic can point a human at without leaking content.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    rows: list[tuple[int, int, str]] = []
    for page_index, page_text in enumerate(normalized.split(PAGE_SEPARATOR)):
        for line_index, raw in enumerate(page_text.split("\n")):
            rows.append((page_index + 1, line_index + 1, raw))
    return rows


def _skip_blanks(rows: list[tuple[int, int, str]], index: int) -> int:
    """Return ``index`` advanced past blank lines. Blank lines are never data."""
    while index < len(rows) and not rows[index][2].strip():
        index += 1
    return index


def _amount(raw: str, page: int, line: int, field: str) -> Decimal:
    try:
        return parse_amount(raw, AmountFormat.ARS)
    except AmountParseError:
        # ``from None`` on purpose: the AmountParseError message echoes the raw
        # number, and a diagnostic must never carry statement text.
        raise LiquidacionParseError(
            f"page {page} line {line}: the {field} is not a well-formed "
            f"comma-decimal amount (length {len(raw)})"
        ) from None


def _trailing_amounts(tokens: list[str]) -> tuple[list[str], int]:
    """Return the trailing amount tokens (document order) and how many tokens they span.

    A ``$`` immediately before an amount attaches to it. Scanning stops at the
    first token that is neither an amount nor an attached ``$`` — which is what
    keeps a four-digit regime number or an eighteen-digit barcode from reading as
    an amount.
    """
    raws: list[str] = []
    index = len(tokens)
    while index > 0 and _AMOUNT_TOKEN_RE.fullmatch(tokens[index - 1]):
        raws.insert(0, tokens[index - 1])
        index -= 1
        if index > 0 and tokens[index - 1] == "$":
            index -= 1
    return raws, len(tokens) - index


def _total_amounts(tokens: list[str]) -> list[str]:
    """Return the total row's amounts, skipping the footnote ``*`` characters.

    The ``*`` in the real total row are footnotes, not amounts; the strict amount
    shape keeps the row's four-digit sequence number from being collected.
    """
    raws: list[str] = []
    index = len(tokens)
    while index > 0 and (tokens[index - 1] == "*" or _AMOUNT_TOKEN_RE.fullmatch(tokens[index - 1])):
        if tokens[index - 1] != "*":
            raws.insert(0, tokens[index - 1])
        index -= 1
    return raws


def _is_rule(raw: str) -> bool:
    """Whether the line is the rule separating the payment block from the movements.

    At least forty characters, none alphanumeric — length is what separates the
    rule from a stray dash or a footnote separator.
    """
    stripped = raw.strip()
    return len(stripped) >= 40 and not any(character.isalnum() for character in stripped)


@dataclass(frozen=True)
class _Head:
    """The ``[<yy> <Mes>] <day>`` head of a dated row, with the rest of the tokens."""

    group: tuple[int, int] | None
    day: int
    rest: list[str]


def _split_head(tokens: list[str]) -> _Head | None:
    """Split the optional merged cell and the day off the front of a dated row.

    The merged cell's first token is the **year** (two digits, so ``20xx``) and the
    day is the token before the comprobante — the shapes are disjoint (a two-digit
    year followed by a month word, a bare one/two-digit day followed by anything
    else), so they cannot be swapped. ``None`` when the line is not day-led at all.
    """
    group: tuple[int, int] | None = None
    offset = 0
    if len(tokens) >= 2 and _YEAR_TOKEN_RE.fullmatch(tokens[0]) and fold(tokens[1]) in _MONTHS:
        group = (2000 + int(tokens[0]), _MONTHS[fold(tokens[1])])
        offset = 2
    if offset >= len(tokens) or not _DAY_TOKEN_RE.fullmatch(tokens[offset]):
        return None
    return _Head(group, int(tokens[offset]), tokens[offset + 1 :])


def _skip_letterhead(rows: list[tuple[int, int, str]], index: int) -> int:
    """Skip the ten-line letterhead block starting at the anchor ``rows[index]``."""
    page, line, _raw = rows[index]
    end = index + _LETTERHEAD_LINES
    if end > len(rows):
        raise LiquidacionParseError(
            f"the letterhead block starting at page {page} line {line} is truncated "
            f"({len(rows) - index} line(s) left)"
        )
    return end


def _letterhead_period(
    rows: list[tuple[int, int, str]], index: int
) -> tuple[int, int, int, int, int]:
    """Return ``(closing_year, closing_month, closing_day, due_year, due_month)``.

    The block is stationery; the only data mined from it is the period line — the
    one line that carries both ``CIERRE`` and ``VENCIMIENTO`` dates. The two
    ``Cierre … Fec.:`` lines carry no ``VENCIMIENTO`` and cannot be mistaken for it.
    The CIERRE day is kept: it is the bound the ``date-not-after-closing`` check
    compares every row date against.
    """
    block = rows[index : index + _LETTERHEAD_LINES]
    for _page, line, raw in block:
        match = _PERIOD_RE.search(raw)
        if match is None:
            continue
        closing_month = _MONTHS.get(fold(match.group("cmonth")))
        due_month = _MONTHS.get(fold(match.group("vmonth")))
        if closing_month is None or due_month is None:
            continue
        return (
            2000 + int(match.group("cyear")),
            closing_month,
            int(match.group("cday")),
            2000 + int(match.group("vyear")),
            due_month,
        )
    raise LiquidacionParseError(
        f"the letterhead block starting at page {rows[index][0]} line {rows[index][1]} "
        f"carries no CIERRE/VENCIMIENTO period line"
    )


def _opening_row(raw: str, page: int, line: int) -> tuple[Decimal, Decimal]:
    """Parse the ``SALDO ANTERIOR <ARS> <USD>`` opening anchor."""
    tokens = raw.strip().split()
    if len(tokens) < 2 or fold(" ".join(tokens[:2])) != "saldo anterior":
        raise LiquidacionParseError(
            f"page {page} line {line}: the opening balance row (SALDO ANTERIOR) does not "
            f"follow the letterhead"
        )
    amounts, _consumed = _trailing_amounts(tokens[2:])
    if len(amounts) != 2:
        raise LiquidacionParseError(
            f"page {page} line {line}: the opening balance row does not carry exactly two amounts"
        )
    return _amount(amounts[0], page, line, "opening balance"), _amount(
        amounts[1], page, line, "opening balance"
    )


def _rate(raw: str, page: int, line: int) -> Decimal:
    """Parse a ``TC<rate>`` token: dot-grouped (or plain) integer, 1..4 comma decimals.

    A dedicated parser on purpose: the rate is a price, not money, and
    :func:`expensuchis.numbers.parse_amount` correctly refuses non-two-decimal
    figures — a rate with three decimals must not be forced through it.
    """
    match = _TC_RATE_RE.fullmatch(raw)
    if match is None:
        raise LiquidacionParseError(
            f"page {page} line {line}: the rate token is not a well-formed TC rate "
            f"(length {len(raw)})"
        )
    integer = match.group("rate").replace(".", "")
    return Decimal(f"{integer}.{match.group('decimals')}")


def _total_row(raw: str, page: int, line: int) -> tuple[Decimal, Decimal]:
    """Parse the ``TOTAL CONSUMOS`` row into ``(ARS, USD)``."""
    amounts = _total_amounts(raw.strip().split())
    if len(amounts) != 2:
        raise LiquidacionParseError(
            f"page {page} line {line}: the total row does not carry exactly two amounts"
        )
    return _amount(amounts[0], page, line, "total"), _amount(amounts[1], page, line, "total")


def _payment_amounts(trailing: list[str], page: int, line: int) -> tuple[Decimal, Decimal | None]:
    """Parse a payment row's trailing amounts: one ARS amount plus an optional ``USD`` one.

    The USD amount, when present, is introduced by a literal ``USD`` token; more
    than one non-USD amount on the row is an unobserved shape and refuses the
    document. The amounts are kept **as printed** — a trailing minus is a credit.
    """
    amount_ars: Decimal | None = None
    amount_usd: Decimal | None = None
    index = 0
    while index < len(trailing):
        token = trailing[index]
        if fold(token) == "usd":
            if (
                amount_usd is not None
                or index + 1 >= len(trailing)
                or not _AMOUNT_TOKEN_RE.fullmatch(trailing[index + 1])
            ):
                raise LiquidacionParseError(
                    f"page {page} line {line}: the payment row carries an unexpected "
                    f"USD field"
                )
            amount_usd = _amount(trailing[index + 1], page, line, "amount")
            index += 2
        elif _AMOUNT_TOKEN_RE.fullmatch(token):
            if amount_ars is not None:
                raise LiquidacionParseError(
                    f"page {page} line {line}: a payment row carries more than one "
                    f"non-USD amount"
                )
            amount_ars = _amount(token, page, line, "amount")
            index += 1
        else:
            raise LiquidacionParseError(
                f"page {page} line {line}: the payment row carries an unexpected field"
            )
    if amount_ars is None:
        raise LiquidacionParseError(
            f"page {page} line {line}: a payment row carries no ARS amount"
        )
    return amount_ars, amount_usd


def _balance_row(
    trailing: list[str],
    group: tuple[int, int],
    day: int,
    page: int,
    line: int,
    day_failures: list[tuple[int, int]],
    closing_date: dt.date,
    closing_failures: list[tuple[int, int]],
) -> Balance | None:
    """Parse the balance row: ``<previous ARS> TC<rate> <resulting ARS-> <resulting USD->``.

    The shape is positional and strict: four tokens with the ``TC`` rate second.
    Anything else is an unobserved shape and refuses the document. Amounts are kept
    as printed; the row's day runs through the same day-valid and closing-date
    checks as every other dated row. A row whose day is invalid cannot be
    represented (``when`` is not optional), so it is dropped and the ``day-valid``
    check fails — the document is refused either way.
    """
    if len(trailing) != 4 or not _AMOUNT_TOKEN_RE.fullmatch(trailing[0]):
        raise LiquidacionParseError(
            f"page {page} line {line}: the balance row does not carry the expected "
            f"previous, rate and resulting fields"
        )
    if _TC_RATE_RE.fullmatch(trailing[1]) is None:
        raise LiquidacionParseError(
            f"page {page} line {line}: the balance row's second field is not a TC rate"
        )
    rate = _rate(trailing[1], page, line)
    if not (_AMOUNT_TOKEN_RE.fullmatch(trailing[2]) and _AMOUNT_TOKEN_RE.fullmatch(trailing[3])):
        raise LiquidacionParseError(
            f"page {page} line {line}: the balance row's resulting fields are not amounts"
        )
    when = _date_or_fail(group, day, page, line, day_failures, closing_date, closing_failures)
    if when is None:
        return None
    return Balance(
        when=when,
        previous_ars=_amount(trailing[0], page, line, "previous balance"),
        rate=rate,
        resulting_ars=_amount(trailing[2], page, line, "resulting balance"),
        resulting_usd=_amount(trailing[3], page, line, "resulting balance"),
        page=page,
        line=line,
    )


def _date_or_fail(
    group: tuple[int, int],
    day: int,
    page: int,
    line: int,
    day_failures: list[tuple[int, int]],
    closing_date: dt.date,
    closing_failures: list[tuple[int, int]],
) -> dt.date | None:
    """Build the row's date, or record a ``day-valid`` failure and return ``None``.

    A row with an invalid day cannot be represented as a date, so it is excluded
    from the result (see the module docstring); the document is refused either way.
    A row that *does* build a date is checked against the statement's closing date
    (``date-not-after-closing``): the closing date is the bound, so a row inside
    the closing month is fine and anything after it is recorded.
    """
    try:
        when = dt.date(group[0], group[1], day)
    except ValueError:
        day_failures.append((page, line))
        return None
    if when > closing_date:
        closing_failures.append((page, line))
    return when


def _record_group_cell(
    group: tuple[int, int],
    last_group: tuple[int, int] | None,
    page: int,
    line: int,
    periods_failures: list[tuple[int, int]],
    cell_positions: list[tuple[tuple[int, int], tuple[int, int]]],
) -> tuple[int, int]:
    """Fold a parsed group cell into the ordering state (``periods-ordered``).

    Consumos are listed chronologically by purchase month, so every group cell —
    a charge group's or a post-total row's — must be non-decreasing in (year,
    month) against the previous cell in document order. The comparison is between
    **cells**, never rows: a row without a cell says nothing about ordering. The
    cell and its position are also recorded for the ``periods-window`` check.
    """
    if last_group is not None and group < last_group:
        periods_failures.append((page, line))
    cell_positions.append((group, (page, line)))
    return group


def _shift_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Shift ``(year, month)`` by ``delta`` months, crossing years correctly."""
    absolute = year * 12 + (month - 1) + delta
    return absolute // 12, absolute % 12 + 1


def _is_money(token: str) -> bool:
    """Whether the token is an Argentine amount with decimals, per the project's own
    authority: :func:`expensuchis.numbers.parse_amount` with ``AmountFormat.ARS``.

    One layered clause — the token must carry a comma — because ``parse_amount``
    accepts a bare integer, while ``(2026)`` or ``(art. 760)`` is a year or an
    article number, never money. One question, one answer; the comma keeps prose
    unclaimed.
    """
    try:
        parse_amount(token, AmountFormat.ARS)
    except AmountParseError:
        return False
    return "," in token


class _Parens(Enum):
    """The head's parenthesis outcome: ``BASE``/``PROSE`` well-formed, ``MALFORMED`` unmatched."""

    NONE = "none"
    BASE = "base"
    PROSE = "prose"
    MALFORMED = "malformed"


def _parenthesis_base(head: list[str], rate_index: int | None) -> _Parens:
    """Three-way parenthesis analysis of the head — never a bare boolean.

    ``BASE``: a well-formed pair enclosing an amount-shaped token (the perception
    classification). ``PROSE``: a well-formed pair holding no money — ``(art.
    760)`` — which cannot match and keeps legal prose unclaimed. ``MALFORMED``:
    an unmatched parenthesis (an opening with no closing one, a closing with no
    opening, or a nested one) — the row claims to be a perception, so it is
    refused, never dropped and never a crash. Glued fragments are stripped (a
    rate may carry the opening one, a base the closing one); every scan is
    bounded by ``len(head)``, so no index arithmetic raises.
    """
    open_index = next((i for i, token in enumerate(head) if "(" in token), None)
    close_index = next((i for i, token in enumerate(head) if ")" in token), None)
    if open_index is None:
        return _Parens.MALFORMED if close_index is not None else _Parens.NONE
    if close_index is None or close_index < open_index:
        return _Parens.MALFORMED
    if any("(" in token for token in head[open_index + 1 : close_index]):
        return _Parens.MALFORMED
    has_base = any(
        _is_money(token.replace("(", "").replace(")", ""))
        for offset, token in enumerate(head[open_index : close_index + 1])
        if open_index + offset != rate_index
    )
    return _Parens.BASE if has_base else _Parens.PROSE


def _surcharge_row(
    tokens: list[str], page: int, line: int
) -> tuple[tuple[SurchargeKind, str, Decimal | None, Decimal | None] | None, bool]:
    """Classify a post-total charge row **by shape**.

    Returns ``((kind, label, amount_ars, amount_usd), claimed)``. ``claimed`` marks
    a row that matches a charge marker but cannot be parsed as one (a SELLOS row
    with the wrong number of amounts, a perception pattern without a usable
    amount); a ``None`` kind with ``claimed=False`` matches no marker at all —
    only such a line ends the block.
    """
    amounts, consumed = _trailing_amounts(tokens)
    head = tokens[: len(tokens) - consumed]
    if _SELLOS_FOLDED in fold(" ".join(tokens)):
        if len(amounts) != 1:
            return None, True
        if head and fold(head[-1]) == "usd":
            return (
                SurchargeKind.SELLOS,
                " ".join(head[:-1]),
                None,
                _amount(amounts[0], page, line, "amount"),
            ), False
        return (SurchargeKind.SELLOS, " ".join(head), _amount(amounts[0], page, line, "amount"), None), False
    rate_index = next((i for i, token in enumerate(head) if _RATE_RE.fullmatch(token)), None)
    parens = _parenthesis_base(head, rate_index)
    if parens is _Parens.MALFORMED:
        # A malformed parenthesis pattern claims to be a perception: the row is
        # a charge row that cannot be parsed, and failing beats dropping.
        return None, True
    if rate_index is not None and parens is _Parens.BASE:
        # A perception pattern without its usable amount is still a claim: the
        # row is a charge row that cannot be parsed, and failing beats dropping.
        if not amounts:
            return None, True
        if len(amounts) == 1:
            amount_ars: Decimal | None = _amount(amounts[0], page, line, "amount")
            amount_usd: Decimal | None = None
        elif len(amounts) == 2:
            first = _amount(amounts[0], page, line, "amount")
            second = _amount(amounts[1], page, line, "amount")
            if first != second:
                return None, True
            amount_ars, amount_usd = None, first
        else:
            return None, True
        return (SurchargeKind.PERCEPCION, " ".join(tokens[:rate_index]), amount_ars, amount_usd), False
    return None, False


def _reconcile(
    payments: list[Payment],
    balance: Balance | None,
    charges: list[Charge],
    surcharges: list[Surcharge],
    total: tuple[Decimal, Decimal],
    opening_ars: Decimal,
    opening_usd: Decimal,
    comprobante_repeats: list[tuple[int, int]],
    periods_failures: list[tuple[int, int]],
    closing_failures: list[tuple[int, int]],
    day_failures: list[tuple[int, int]],
    surcharge_failures: list[tuple[int, int]],
    installment_failures: list[tuple[int, int]],
    unclassified: list[tuple[int, int]],
    cell_positions: list[tuple[tuple[int, int], tuple[int, int]]],
    closing: tuple[int, int],
) -> tuple[CheckResult, ...]:
    """Run all checks and return them in :data:`CHECK_NAMES` order.

    Every check is computed even after an earlier one fails, so one run tells the
    whole story. Details are masked: counts, indexes and page/line numbers only.
    """
    checks: list[CheckResult] = []

    if unclassified:
        page, line = unclassified[0]
        block_detail = (
            f"{len(unclassified)} line(s) between the opening anchor and the total row "
            f"match no known shape; first at page {page} line {line}"
        )
    else:
        block_detail = (
            f"every line between the opening anchor and the total row is classified "
            f"({len(payments)} payment row(s), {len(charges)} charge row(s))"
        )
    checks.append(CheckResult("block-complete", not unclassified, block_detail))

    ars_sum = sum((charge.amount_ars for charge in charges if charge.amount_ars is not None), Decimal(0))
    if ars_sum != total[0]:
        ars_detail = (
            f"the ARS sum of {len(charges)} charge row(s) does not equal the declared "
            f"ARS total"
        )
    else:
        ars_detail = f"the ARS amounts of {len(charges)} charge row(s) sum to the declared ARS total"
    checks.append(CheckResult("charge-sums-ars", ars_sum == total[0], ars_detail))

    usd_sum = sum((charge.amount_usd for charge in charges if charge.amount_usd is not None), Decimal(0))
    if usd_sum != total[1]:
        usd_detail = (
            f"the USD sum of {sum(1 for charge in charges if charge.amount_usd is not None)} "
            f"USD-only charge row(s) does not equal the declared USD total"
        )
    else:
        usd_detail = (
            f"the USD amounts of {sum(1 for charge in charges if charge.amount_usd is not None)} "
            f"USD-only charge row(s) sum to the declared USD total"
        )
    checks.append(CheckResult("charge-sums-usd", usd_sum == total[1], usd_detail))

    if comprobante_repeats:
        page, line = comprobante_repeats[0]
        unique_detail = (
            f"{len(comprobante_repeats)} repeated comprobante(s); first repeat at "
            f"page {page} line {line}"
        )
    else:
        unique_detail = f"{len(charges)} comprobante(s) are unique across the document"
    checks.append(CheckResult("comprobante-unique", not comprobante_repeats, unique_detail))

    if periods_failures:
        page, line = periods_failures[0]
        periods_detail = (
            f"{len(periods_failures)} group cell(s) decrease in (year, month) in document "
            f"order; first at page {page} line {line}"
        )
    else:
        periods_detail = (
            f"all {len(cell_positions)} group cell(s) are non-decreasing in (year, month) "
            f"in document order"
        )
    checks.append(CheckResult("periods-ordered", not periods_failures, periods_detail))

    # The window's bound comes from the statement's own data, never from a clock:
    # the current billing month and the purchase month are related by the
    # installment NUMBER, not by the plan's total — a row at ``C.NN/TOT`` is the
    # NN-th billing, so its purchase month is at most ``NN - 1`` months before the
    # closing month, and the document's bound is ``max(3, the largest installment
    # number)`` months back. The floor of 3 keeps a plan-less statement (no
    # markers, or only ``C.01/...``) windowed at three months — a normal cycle —
    # and keeps a ``C.01/99`` row from widening the window to 99 months. The upper
    # bound is the closing month; it is redundant with ``date-not-after-closing``
    # yet survives that check's mutant, so it is pinned by its own test. On a
    # document with no charges the check examines zero cells (the detail says so).
    max_number = max(
        (charge.installment_number for charge in charges if charge.installment_number is not None),
        default=0,
    )
    lower, _lower_month = _shift_months(closing[0], closing[1], -max(3, max_number))
    lower_bound = (lower, _lower_month)
    outside = [position for cell, position in cell_positions if not lower_bound <= cell <= closing]
    if outside:
        page, line = outside[0]
        window_detail = (
            f"{len(outside)} group cell(s) fall outside the installment window; "
            f"first at page {page} line {line}"
        )
    else:
        window_detail = f"all {len(cell_positions)} group cell(s) are inside the installment window"
    checks.append(CheckResult("periods-window", not outside, window_detail))

    dated_rows = len(charges) + len(payments) + sum(1 for s in surcharges if s.when is not None)
    if closing_failures:
        page, line = closing_failures[0]
        closing_detail = (
            f"{len(closing_failures)} row(s) carry a date after the closing date; "
            f"first at page {page} line {line}"
        )
    else:
        closing_detail = f"all {dated_rows} dated row(s) are on or before the closing date"
    checks.append(CheckResult("date-not-after-closing", not closing_failures, closing_detail))

    # The balance chain, in magnitudes: the statement's balance signs are not yet
    # understood and nothing in this block is posted — the check's job is to prove
    # the payment row and the balance row belong to one chain and that no row was
    # dropped. Run only when a balance row is present; without one it is vacuous.
    chain_ok = True
    if balance is None:
        chain_detail = "no balance row; the balance-chain check is vacuous"
    else:
        payment_ars = sum((payment.amount_ars for payment in payments), Decimal(0))
        payment_usd = sum(
            (payment.amount_usd for payment in payments if payment.amount_usd is not None),
            Decimal(0),
        )
        relations = (
            abs(opening_ars) - abs(payment_ars) == abs(balance.resulting_ars),
            abs(opening_usd) - abs(payment_usd) == abs(balance.resulting_usd),
            abs(balance.previous_ars) == abs(opening_ars),
        )
        broken = [
            name
            for name, holds in (
                ("the resulting-ARS relation", relations[0]),
                ("the resulting-USD relation", relations[1]),
                ("the previous-balance relation", relations[2]),
            )
            if not holds
        ]
        chain_ok = not broken
        if broken:
            chain_detail = (
                f"the balance chain breaks in magnitudes: {len(broken)} of 3 relation(s) "
                f"fail ({', '.join(broken)}); balance row at page {balance.page} line "
                f"{balance.line}"
            )
        else:
            chain_detail = (
                f"the balance chain closes in magnitudes across {len(payments)} payment "
                f"row(s) and the balance row"
            )
    checks.append(CheckResult("balance-chain", chain_ok, chain_detail))

    if day_failures:
        page, line = day_failures[0]
        day_detail = (
            f"{len(day_failures)} row(s) carry a day invalid for their month and year; "
            f"first at page {page} line {line}"
        )
    else:
        day_detail = (
            "every charge, payment and surcharge day is valid for its month and year"
        )
    checks.append(CheckResult("day-valid", not day_failures, day_detail))

    if surcharge_failures:
        page, line = surcharge_failures[0]
        surcharge_detail = (
            f"{len(surcharge_failures)} post-total charge row(s) match no known surcharge "
            f"shape; first at page {page} line {line}"
        )
    else:
        surcharge_detail = f"all {len(surcharges)} post-total charge row(s) are classified"
    checks.append(CheckResult("surcharge-complete", not surcharge_failures, surcharge_detail))

    if installment_failures:
        page, line = installment_failures[0]
        installment_detail = (
            f"{len(installment_failures)} installment marker(s) fall outside 1..TOT or "
            f"carry TOT < 2; first at page {page} line {line}"
        )
    else:
        installment_detail = (
            f"every installment marker satisfies 1 <= NN <= TOT and TOT >= 2 "
            f"({sum(1 for charge in charges if charge.installment_total is not None)} marked row(s))"
        )
    checks.append(CheckResult("installment-valid", not installment_failures, installment_detail))

    return tuple(checks)


def parse_liquidacion(text: str) -> Liquidacion:
    """Parse and reconcile one extracted card statement.

    Raises:
        LiquidacionParseError: the text is not a ``Liquidación Visa``: the
            letterhead anchor, the opening balance row or the total row is missing
            or misplaced, a charge row has no trailing amount, or an amount is
            malformed. The message is masked.
        ReconciliationError: the document parsed but a check failed. The error
            carries every check and the failed subset; nothing partial is returned.
    """
    rows = _layout(text)
    index = _skip_blanks(rows, 0)
    if index >= len(rows) or not _ANCHOR_RE.match(rows[index][2].strip()):
        raise LiquidacionParseError(
            "the text does not start with a Banco Provincia card letterhead "
            "(no '(NNNN)' anchor line)"
        )
    closing_year, closing_month, closing_day, due_year, due_month = _letterhead_period(rows, index)
    try:
        closing_date = dt.date(closing_year, closing_month, closing_day)
    except ValueError:
        raise LiquidacionParseError("the letterhead's CIERRE date is not a real date") from None
    index = _skip_blanks(rows, index + _LETTERHEAD_LINES)
    if index >= len(rows):
        raise LiquidacionParseError("the opening balance row (SALDO ANTERIOR) is missing")
    page, line, raw = rows[index]
    opening_ars, opening_usd = _opening_row(raw, page, line)
    index = _skip_blanks(rows, index + 1)

    payments: list[Payment] = []
    charges: list[Charge] = []
    surcharges: list[Surcharge] = []
    unclassified: list[tuple[int, int]] = []
    comprobante_first: dict[str, tuple[int, int]] = {}
    comprobante_repeats: list[tuple[int, int]] = []
    periods_failures: list[tuple[int, int]] = []
    closing_failures: list[tuple[int, int]] = []
    day_failures: list[tuple[int, int]] = []
    surcharge_failures: list[tuple[int, int]] = []
    installment_failures: list[tuple[int, int]] = []
    group: tuple[int, int] | None = None
    last_group: tuple[int, int] | None = None
    cell_positions: list[tuple[tuple[int, int], tuple[int, int]]] = []
    payment_group: tuple[int, int] | None = None
    total: tuple[Decimal, Decimal] | None = None

    # The payment block: zero or more credit rows and at most one balance row, all
    # folding to SU PAGO EN PESOS. The TC<rate> token is the structural
    # discriminator — a row carrying one is the balance row; a second one is an
    # unobserved shape and refuses. Never posted, never an expense.
    balance: Balance | None = None
    while index < len(rows):
        page, line, raw = rows[index]
        tokens = raw.strip().split()
        head = _split_head(tokens)
        if head is None or len(head.rest) < 4 or fold(" ".join(head.rest[:4])) != _PAYMENT_FOLDED:
            break
        if head.group is not None:
            # A payment cell carries the purchase month of the row itself; it
            # is not a charge group cell and does not join the ordering sequence.
            payment_group = head.group
        if payment_group is None:
            raise LiquidacionParseError(
                f"page {page} line {line}: a payment row carries no month group"
            )
        trailing = head.rest[4:]
        if any(_TC_RATE_RE.fullmatch(token) for token in trailing):
            if balance is not None:
                raise LiquidacionParseError(
                    f"page {page} line {line}: a second balance row refuses the "
                    f"document (an unobserved shape)"
                )
            balance = _balance_row(
                trailing,
                payment_group,
                head.day,
                page,
                line,
                day_failures,
                closing_date,
                closing_failures,
            )
        else:
            amount_ars, amount_usd = _payment_amounts(trailing, page, line)
            when = _date_or_fail(
                payment_group, head.day, page, line, day_failures, closing_date, closing_failures
            )
            if when is not None:
                payments.append(
                    Payment(
                        when=when,
                        amount_ars=amount_ars,
                        amount_usd=amount_usd,
                        page=page,
                        line=line,
                    )
                )
        index = _skip_blanks(rows, index + 1)

    index = _skip_blanks(rows, index)
    if index < len(rows) and _is_rule(rows[index][2]):
        index = _skip_blanks(rows, index + 1)

    # The charge rows, up to the total. A repeated letterhead inside the block is
    # pagination: skip it and require the block to resume with a charge row.
    while index < len(rows):
        page, line, raw = rows[index]
        stripped = raw.strip()
        if not stripped:
            index += 1
            continue
        if _ANCHOR_RE.match(stripped):
            index = _skip_blanks(rows, _skip_letterhead(rows, index))
            if index >= len(rows) or not _resume_is_charge(rows[index][2]):
                raise LiquidacionParseError(
                    "the statement block does not resume with a charge row after the "
                    "repeated letterhead"
                )
            continue
        if _TOTAL_FOLDED in fold(stripped):
            total = _total_row(stripped, page, line)
            index += 1
            break
        if _is_rule(stripped):
            index += 1
            continue
        tokens = stripped.split()
        head = _split_head(tokens)
        if head is not None and head.rest and _COMPROBANTE_RE.fullmatch(head.rest[0]):
            comprobante = head.rest[0]
            if head.group is not None:
                group = head.group
                last_group = _record_group_cell(
                    head.group, last_group, page, line, periods_failures, cell_positions
                )
            if group is None:
                raise LiquidacionParseError(
                    f"page {page} line {line}: a charge row appears before any month group"
                )
            body = head.rest[1:]
            amounts, consumed = _trailing_amounts(body)
            tail = body[: len(body) - consumed]
            installment_number: int | None = None
            installment_total: int | None = None
            if tail and _INSTALLMENT_RE.fullmatch(tail[-1]):
                match = _INSTALLMENT_RE.fullmatch(tail[-1])
                installment_number = int(match.group(1))
                installment_total = int(match.group(2))
                if not (1 <= installment_number <= installment_total and installment_total >= 2):
                    installment_failures.append((page, line))
                tail = tail[:-1]
            description = " ".join(tail)
            if not amounts:
                raise LiquidacionParseError(
                    f"page {page} line {line}: a charge row has no trailing amount; a row "
                    f"wrapping onto two lines has never been observed and is refused"
                )
            if len(amounts) == 1:
                amount_ars: Decimal | None = _amount(amounts[0], page, line, "amount")
                amount_usd: Decimal | None = None
            elif len(amounts) == 2:
                first = _amount(amounts[0], page, line, "amount")
                second = _amount(amounts[1], page, line, "amount")
                if first != second:
                    raise LiquidacionParseError(
                        f"page {page} line {line}: a charge row ends with two different amounts"
                    )
                # A USD-only row prints the same value twice (importe and dólares
                # columns); it is one USD amount, never an ARS amount.
                amount_ars, amount_usd = None, first
            else:
                raise LiquidacionParseError(
                    f"page {page} line {line}: a charge row ends with an unexpected "
                    f"number of amounts"
                )
            when = _date_or_fail(
                group, head.day, page, line, day_failures, closing_date, closing_failures
            )
            if when is not None:
                if comprobante in comprobante_first:
                    comprobante_repeats.append((page, line))
                else:
                    comprobante_first[comprobante] = (page, line)
                charges.append(
                    Charge(
                        when=when,
                        comprobante=comprobante,
                        description=description,
                        installment_number=installment_number,
                        installment_total=installment_total,
                        amount_ars=amount_ars,
                        amount_usd=amount_usd,
                        page=page,
                        line=line,
                    )
                )
            index += 1
            continue
        unclassified.append((page, line))
        index += 1

    if total is None:
        raise LiquidacionParseError("the total row (TOTAL CONSUMOS) is missing")

    # The post-total charge rows, classified by shape. The block is closed by the
    # first line that carries no trailing amount; everything after it is furniture
    # the scanner never re-enters.
    index = _skip_blanks(rows, index)
    while index < len(rows):
        page, line, raw = rows[index]
        stripped = raw.strip()
        if not stripped:
            index += 1
            continue
        if _ANCHOR_RE.match(stripped):
            index = _skip_blanks(rows, _skip_letterhead(rows, index))
            continue
        tokens = stripped.split()
        head = _split_head(tokens)
        rest = head.rest if head is not None else tokens
        row_group = head.group if head is not None else None
        day = head.day if head is not None else None
        parsed, claimed = _surcharge_row(rest, page, line)
        if claimed:
            surcharge_failures.append((page, line))
            index += 1
            continue
        if parsed is not None:
            kind, surcharge_label, amount_ars, amount_usd = parsed
            when: dt.date | None = None
            if row_group is not None:
                group = row_group
                last_group = _record_group_cell(
                    row_group, last_group, page, line, periods_failures, cell_positions
                )
            if day is not None:
                if group is None:
                    raise LiquidacionParseError(
                        f"page {page} line {line}: a dated surcharge row appears before "
                        f"any month group"
                    )
                when = _date_or_fail(
                    group, day, page, line, day_failures, closing_date, closing_failures
                )
            if when is not None or day is None:
                surcharges.append(
                    Surcharge(
                        kind=kind,
                        label=surcharge_label,
                        when=when,
                        amount_ars=amount_ars,
                        amount_usd=amount_usd,
                        page=page,
                        line=line,
                    )
                )
            index = _skip_blanks(rows, index + 1)
            continue
        if _trailing_amounts(rest)[0]:
            # An amount-tailed row that is neither SELLOS nor PERCEPCION is a
            # charge row the parser refuses to guess about — day-led or not (the
            # module docstring records why the day token must not gate this).
            surcharge_failures.append((page, line))
            index += 1
            continue
        # No trailing amount: furniture; the block ends and is never re-entered.
        break

    checks = _reconcile(
        payments=payments,
        balance=balance,
        charges=charges,
        surcharges=surcharges,
        total=total,
        opening_ars=opening_ars,
        opening_usd=opening_usd,
        comprobante_repeats=comprobante_repeats,
        periods_failures=periods_failures,
        closing_failures=closing_failures,
        day_failures=day_failures,
        surcharge_failures=surcharge_failures,
        installment_failures=installment_failures,
        unclassified=unclassified,
        cell_positions=cell_positions,
        closing=(closing_year, closing_month),
    )
    if any(not check.ok for check in checks):
        raise ReconciliationError(checks)
    return Liquidacion(
        period_year=closing_year,
        due_year=due_year,
        due_month=due_month,
        closing_year=closing_year,
        closing_month=closing_month,
        opening_ars=opening_ars,
        opening_usd=opening_usd,
        payments=tuple(payments),
        balance=balance,
        charges=tuple(charges),
        total_ars=total[0],
        total_usd=total[1],
        surcharges=tuple(surcharges),
        checks=checks,
    )


def _resume_is_charge(raw: str) -> bool:
    """Whether the line after a repeated letterhead resumes the block with a charge row.

    The window is bounded by construction: exactly the next non-blank line is
    examined, so a truncated document cannot trail into prose unnoticed.
    """
    tokens = raw.strip().split()
    head = _split_head(tokens)
    if head is None or not head.rest or not _COMPROBANTE_RE.fullmatch(head.rest[0]):
        return False
    return bool(_trailing_amounts(head.rest[1:])[0])


def charge_keys(charges: Sequence[Charge]) -> tuple[str, ...]:
    """Return the natural ``key`` of every charge, in document order.

    ``<date>:<comprobante>:<amount>`` — the comprobante is unique across the
    document (enforced by the ``comprobante-unique`` check), so, unlike
    ``provincia.movement_keys``, no occurrence suffix is needed. The amount is the
    ARS amount, or the USD amount on a USD-only row. Keys may contain ``:`` and
    spaces inside the identity part — the key is treated as an opaque string
    everywhere downstream.
    """
    keys: list[str] = []
    for charge in charges:
        amount = charge.amount_ars if charge.amount_ars is not None else charge.amount_usd
        keys.append(f"{charge.when.isoformat()}:{charge.comprobante}:{amount:.2f}")
    return tuple(keys)
