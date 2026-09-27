"""Parser for BBVA Argentina's ``Extracto consolidado`` account statement.

Pure over extracted text: no PDF library, no filesystem, no clock and no
environment beyond the caller-supplied ``anchor`` date. The caller (the
importer's ``extract``, a later task) owns text extraction; this module owns
meaning.

The parser is a **gate**, not a reader. :func:`parse_extracto_consolidado`
reconciles the document against itself and raises
:class:`ReconciliationError` whenever any check fails; it never returns a
partially trusted result. There is no ``strict=False``: the caller cannot opt
out of a check it does not like.

What the document looks like
-----------------------------

Page 1 opens with the debit-card detail section (``TARJETAS DE DEBITO``): a
short header block naming the card's last four digits and the linked debit
account, followed by zero or more purchase-detail rows, each carrying a
full ``dd/mm/yyyy`` date, a merchant, a six-digit id and a signed amount.
This section is the merchant source for every ``PAGO CON VISA DEBITO``
movement row in the tables below it (owner decision: the movement table is
the source of truth, joined to this section strictly by ``(date, amount)``).

Then one or more account-movement tables follow, one per sub-account
(``CC``/``CA``, currency ``$``/``U$S``/``EUR``). Each table is a self-
contained block: an opening ``SALDO ANTERIOR``, zero or more movement rows
(or a single ``SIN MOVIMIENTOS`` row), a closing ``SALDO AL <dd> DE <MES>``
and a ``TOTAL MOVIMIENTOS`` line. Only a ``$`` block ever carries movements;
a ``U$S``/``EUR`` block is accepted only when quiescent (owner decision,
mirroring Brubank's USD block) and contributes nothing to the ledger.

Finally, a sent-transfers detail section lists every outgoing transfer by
recipient CUIT, account and amount; each ``TRANSFERENCIA`` movement row (the
plain, non-``INMEDIATA`` variant) must match exactly one row here by
``(date, absolute amount)`` (owner decision), which carries the recipient
CUIT onto the movement as key material.

The document never prints the statement's year. :func:`parse_extracto_consolidado`
takes an ``anchor`` date (the caller's clock proxy, e.g. the PDF's
CreationDate) and an optional ``year`` override (owner decision, "Year"):
without an override, the close is the latest ``(day, MES)`` on or before
``anchor``, and the parse refuses when ``anchor`` is more than 60 days after
the close; with an override, the close's year is ``year`` and the 60-day
ceiling does not apply. Every ``dd/mm`` row is placed backwards from the
close: a row whose month is later than the close month belongs to the
previous year (December -> January).

Table regions
-------------

Each account-movement block has a **table region**: it opens right after the
block's own repeated table header (``FECHA ORIGEN CONCEPTO DEBITO CREDITO
SALDO``) and closes at the ``SALDO AL`` line. Inside that region every line
must be exactly one of the opening ``SALDO ANTERIOR`` row (once), a movement
row, the single ``SIN MOVIMIENTOS`` row, or blank -- never silently skipped.
This is the same "region as gate" model Brubank uses: a table split across
pages, a stray footer line or an unclassifiable fragment landing inside an
open region is a document the parser failed to understand, never furniture
by default (the recurring CRITICAL class from T-06b). Outside a region --
the debit-card detail section, the furniture between blocks (page-number
and IVA/CUIT footer lines, legal boilerplate, the sent-transfers section) --
an unrecognized line stays unremarkable furniture, because those sections
are parsed by their own dedicated, bounded scans rather than this shared
region model.

Concept classes
----------------

The concept vocabulary is closed (folded, ASCII, matched structurally) and
each concept class carries a fixed sign; an unknown concept or a concept
whose row sign contradicts its class both refuse immediately, as a
structural failure, the same way Brubank refuses a row with a value in both
or neither of Débito/Crédito. See :data:`_CONCEPT_MATCHERS`.

Masking
-------

The real statements are household data and the models that run this
workflow are remote. **No diagnostic may echo a raw statement line.** Every
:class:`CheckResult` detail and every error message in this module reports
line numbers, movement indexes, counts and field names only -- never a date,
a merchant, a CUIT, an account number, an amount or a balance. The 14-digit
card-account number and every account number are sensitive: they are kept
only as :class:`Movement` fields, never echoed in a message.

The natural key
----------------

:func:`movement_keys` builds each movement's identity:
``<account>:<date>:<amount>:<balance>`` (owner decision 2, "Dedup key for
every row"). The section-A detail row's six-digit id is metadata only and
plays no part in the key.

Sub-account blocks
-------------------

:attr:`ExtractoConsolidado.blocks` exposes every parsed sub-account block's
``kind`` (``cc``/``ca``), ``currency`` and ``account`` number, independently
of whether the block carries any movement. A :class:`Movement`'s own
``account`` field is the raw sub-account number; the block's ``kind`` and
``currency`` are not repeated onto every movement, so the caller (T-07b's
importer, which maps kind + currency to a ledger account) reads them from
here instead.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum

from ..numbers import AmountFormat, AmountParseError, parse_amount

__all__ = [
    "CHECK_NAMES",
    "AccountBlock",
    "CheckResult",
    "ExtractoConsolidado",
    "ExtractoConsolidadoParseError",
    "Movement",
    "MovementKind",
    "ReconciliationError",
    "fold",
    "is_bbva_extracto",
    "movement_keys",
    "parse_extracto_consolidado",
]


class MovementKind(Enum):
    """The closed concept vocabulary of a BBVA account-movement row."""

    DEBIT_CARD_PURCHASE = "debit_card_purchase"
    VISA_SETTLEMENT = "visa_settlement"
    MASTERCARD_SETTLEMENT = "mastercard_settlement"
    CASH_WITHDRAWAL = "cash_withdrawal"
    SALARY = "salary"
    INTEREST = "interest"
    TRANSFER_OUT = "transfer_out"
    TRANSFER_IN = "transfer_in"


#: The checks, in the fixed order they are reported. See the module
#: docstring for what each block-local check reconciles; ``keys-unique``,
#: ``saldo-al-agreement``, ``debit-card-details-matched`` and
#: ``transfers-matched`` are whole-document checks.
CHECK_NAMES: tuple[str, ...] = (
    "running-balance-chain",
    "closing-balance",
    "total-movements",
    "ascending-dates",
    "dates-in-window",
    "saldo-al-agreement",
    "keys-unique",
    "debit-card-details-matched",
    "transfers-matched",
)

_ARS_AMOUNT = r"\d{1,3}(?:\.\d{3})*,\d{2}"

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

_TABLE_HEADER_TOKENS = ("fecha", "origen", "concepto", "debito", "credito", "saldo")

_VISA_DEBITO_RE = re.compile(r"^\S+\s+visa debito\s+(?P<last4>\d{4})$")
_CUENTA_DEBITO_RE = re.compile(r"^cuenta debito\s+\S+\s+\$\s+(?P<account>[\d./-]+)$")
_DETAIL_ROW_RE = re.compile(
    rf"^(?P<dd>\d{{2}})/(?P<mm>\d{{2}})/(?P<yyyy>\d{{4}})\s+(?P<body>.+)\s+"
    rf"(?P<id>\d{{6}})\s+\$\s+-(?P<amount>{_ARS_AMOUNT})$"
)

_BLOCK_HEADER_RE = re.compile(
    r"^(?P<kind>cc|ca)\s+(?P<currency>\S+)\s+(?P<account>\S+)\s+.+\bfinal$"
)
_KNOWN_CURRENCIES = ("$", "u$s", "eur")
_ROW_RE = re.compile(
    rf"^(?P<dd>\d{{2}})/(?P<mm>\d{{2}})\s+(?:(?P<origin>\d{{3}})\s+)?(?P<concept>.+?)\s+"
    rf"(?P<movement>-?{_ARS_AMOUNT})\s+(?P<balance>{_ARS_AMOUNT})$"
)
_SALDO_ANTERIOR_RE = re.compile(rf"^saldo anterior\s+(?P<amount>{_ARS_AMOUNT})$")
_SIN_MOVIMIENTOS_RE = re.compile(
    rf"^(?P<dd>\d{{2}})/(?P<mm>\d{{2}})\s+sin movimientos\s+(?P<balance>{_ARS_AMOUNT})$"
)
_SALDO_AL_RE = re.compile(
    rf"^saldo al\s+(?P<dd>\d{{1,2}})\s+de\s+(?P<month>\S+)\s+(?P<amount>{_ARS_AMOUNT})$"
)
_TOTAL_RE = re.compile(
    rf"^total movimientos\s+-(?P<debits>{_ARS_AMOUNT})\s+(?P<credits>{_ARS_AMOUNT})$"
)
_LEGAL_LINE_TOKENS = ("movimientos", "de iva", "credito")

_TRANSFER_HEADER_RE = re.compile(r"^fecha\s+\S+.*\bnro\b.*\bcuenta\s+origen$")
_TRANSFER_ROW_RE = re.compile(
    rf"^(?P<dd>\d{{2}})/(?P<mm>\d{{2}})\s+(?P<cuit>\d{{11}})\s+(?:\S+\s+)?"
    rf"(?P<account1>\d{{10}})\s+.+?\s+\$\s+(?P<amount>{_ARS_AMOUNT})\s+\S+\s+\$\s+\d{{10}}$"
)

_DETALLE_LINE = "detalle"
_MOVIMIENTOS_EN_RE = re.compile(r"^movimientos en\s+\S+$")


def fold(text: str) -> str:
    """Casefold and strip accents: ``CRÉDITO`` and ``credito`` become ``credito``."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def is_bbva_extracto(text: str) -> bool:
    """Whether ``text`` is a BBVA ``Extracto consolidado``.

    Structural: a line whose folded tokens are exactly the table header
    (``FECHA ORIGEN CONCEPTO DEBITO CREDITO SALDO``) and, anywhere in the
    document, a line carrying the word ``CONSOLIDADO``. Neither alone is
    enough: Brubank's own table header shares four of six tokens
    (``Fecha ... Débito Crédito Saldo``) but never the word ``CONSOLIDADO``,
    and a BBVA page carries ``RESUMEN``/``MOVIMIENTOS`` on different lines,
    never Brubank's exact ``resumen``+``movimientos`` title line.
    """
    lines = _normalize(text).split("\n")
    has_header = any(
        tuple(fold(line.strip()).split()) == _TABLE_HEADER_TOKENS for line in lines
    )
    has_consolidado = any("consolidado" in fold(line).split() for line in lines)
    return has_header and has_consolidado


@dataclass(frozen=True)
class Movement:
    """One reconciled movement, with the position it came from for diagnostics.

    ``account`` is the sub-account number the movement belongs to (sensitive:
    kept only as data). ``merchant`` is non-empty only for
    :attr:`MovementKind.DEBIT_CARD_PURCHASE` (joined from the debit-card
    detail section). ``recipient_cuit`` is non-empty only for
    :attr:`MovementKind.TRANSFER_OUT` (joined from the sent-transfers
    section). ``origin`` is the row's optional 3-digit origin code, or empty.
    """

    date: dt.date
    account: str
    origin: str
    concept: str
    amount: Decimal
    balance: Decimal
    page: int
    line: int
    kind: MovementKind
    merchant: str
    recipient_cuit: str


@dataclass(frozen=True)
class CheckResult:
    """One reconciliation check and its **masked** detail."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class AccountBlock:
    """One parsed sub-account block's identity: kind, currency and account number.

    ``kind`` is ``"cc"`` or ``"ca"`` and ``currency`` is one of
    :data:`_KNOWN_CURRENCIES`, both folded. Exposed independently of whether
    the block carries any movement -- see the module docstring, "Sub-account
    blocks".
    """

    kind: str
    currency: str
    account: str


@dataclass(frozen=True)
class ExtractoConsolidado:
    """A fully reconciled statement. Only :func:`parse_extracto_consolidado` builds one."""

    close_date: dt.date
    movements: tuple[Movement, ...]
    checks: tuple[CheckResult, ...]
    blocks: tuple[AccountBlock, ...]


class ExtractoConsolidadoParseError(ValueError):
    """The text is not a parsable ``Extracto consolidado``.

    Structural failure, distinct from :class:`ReconciliationError`: a
    debit-card detail row or account-movement row has no recognized shape,
    a block's currency marker is unknown, a non-``$`` block carries
    movement, a concept is outside the closed vocabulary or its sign
    contradicts its class, an amount is malformed, or the close date cannot
    be resolved from ``anchor``/``year``. The message never echoes statement
    text.
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
            f"BBVA reconciliation failed: {names}. "
            f"Refusing the document; a partially trusted parse is never returned."
        )


def movement_keys(movements: Sequence[Movement]) -> tuple[str, ...]:
    """Return the natural ``key`` of every movement, in document order.

    ``<account>:<date>:<amount>:<balance>`` -- see the module docstring, "The
    natural key".
    """
    return tuple(
        f"{movement.account}:{movement.date.isoformat()}:"
        f"{movement.amount:.2f}:{movement.balance:.2f}"
        for movement in movements
    )


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _layout(text: str) -> list[tuple[int, int, str]]:
    """Return ``(page, global_line, raw_line)`` for every line of the text."""
    normalized = _normalize(text)
    rows: list[tuple[int, int, str]] = []
    global_line = 0
    for page_index, page_text in enumerate(normalized.split("\f")):
        for raw in page_text.split("\n"):
            global_line += 1
            rows.append((page_index + 1, global_line, raw))
    return rows


def _amount(raw: str, line: int, field: str) -> Decimal:
    try:
        return parse_amount(raw, AmountFormat.ARS)
    except AmountParseError:
        raise ExtractoConsolidadoParseError(
            f"line {line}: the {field} is not a well-formed Argentine amount (length {len(raw)})"
        ) from None


# --------------------------------------------------------------------- section A


@dataclass(frozen=True)
class _DetailRow:
    date: dt.date
    merchant: str
    amount: Decimal
    page: int
    line: int


def _parse_debit_card_details(
    rows: list[tuple[int, int, str]],
) -> tuple[_DetailRow, ...]:
    """Parse the debit-card detail section: the merchant source for purchase rows."""
    last4: str | None = None
    account_index: int | None = None
    for index, (_page, _line, raw) in enumerate(rows):
        stripped = fold(raw.strip())
        match = _VISA_DEBITO_RE.match(stripped)
        if match is not None:
            last4 = match.group("last4")
            continue
        if last4 is not None and _CUENTA_DEBITO_RE.match(stripped) is not None:
            account_index = index
            break
    if last4 is None:
        # Section (A) is optional: a month without debit-card purchases (or a
        # loose 'CUENTA DEBITO'/'TARJETAS DE CREDITO'/'VISA <word>' header
        # area with no 'VISA DEBITO <last4>' anchor) carries no detail rows.
        # A PAGO CON VISA DEBITO row still refuses through the existing
        # strict join (zero candidates), never through a structural error
        # raised here.
        return ()
    if account_index is None:
        raise ExtractoConsolidadoParseError(
            "the debit-card 'VISA DEBITO <last4>' header is present but never followed "
            "by its 'CUENTA DEBITO ... $ <account>' line"
        )

    details: list[_DetailRow] = []
    for page, line, raw in rows[account_index + 1 :]:
        stripped = raw.strip()
        match = _DETAIL_ROW_RE.match(stripped)
        if match is None:
            break
        body_tokens = match.group("body").split()
        occurrences = [i for i, token in enumerate(body_tokens) if token == last4]
        if len(occurrences) != 1:
            raise ExtractoConsolidadoParseError(
                f"line {line}: the debit-card detail row's merchant split on the card's "
                f"last 4 digits is ambiguous ({len(occurrences)} occurrence(s))"
            )
        split_index = occurrences[0]
        if split_index == len(body_tokens) - 1:
            raise ExtractoConsolidadoParseError(
                f"line {line}: the debit-card detail row has no words after the card's "
                f"last 4 digits"
            )
        merchant = " ".join(body_tokens[:split_index])
        try:
            row_date = dt.date(
                int(match.group("yyyy")), int(match.group("mm")), int(match.group("dd"))
            )
        except ValueError:
            raise ExtractoConsolidadoParseError(
                f"line {line}: the debit-card detail row's date is not a real date"
            ) from None
        details.append(
            _DetailRow(
                date=row_date,
                merchant=merchant,
                amount=_amount(match.group("amount"), line, "amount"),
                page=page,
                line=line,
            )
        )
    return tuple(details)


# --------------------------------------------------------------------- concept classification

_CARD_ACCOUNT_RE = re.compile(r"^cuenta visa\s+(?P<account>\d{14})$")
_MASTERCARD_ACCOUNT_RE = re.compile(r"^cuenta mastercard\s+(?P<account>\d{14})$")
_TRANSFER_OUT_RE = re.compile(
    r"^transferencia(?:\s+(?P<word>\S+)(?:\s+(?P<digits6>\d{6})\s+\d)?)?$"
)


def _classify_concept(
    concept: str, is_debit: bool, has_origin: bool, line: int
) -> MovementKind:
    """Classify a folded concept into its :class:`MovementKind`, refusing on mismatch."""
    if concept == "pago con visa debito":
        kind, expected_debit, forbids_origin = MovementKind.DEBIT_CARD_PURCHASE, True, False
    elif _CARD_ACCOUNT_RE.match(concept) is not None:
        kind, expected_debit, forbids_origin = MovementKind.VISA_SETTLEMENT, True, True
    elif _MASTERCARD_ACCOUNT_RE.match(concept) is not None:
        kind, expected_debit, forbids_origin = MovementKind.MASTERCARD_SETTLEMENT, True, True
    elif concept == "extraccion elec+cash":
        kind, expected_debit, forbids_origin = MovementKind.CASH_WITHDRAWAL, True, False
    elif concept == "pago haberes":
        kind, expected_debit, forbids_origin = MovementKind.SALARY, False, False
    elif concept == "intereses ganados":
        kind, expected_debit, forbids_origin = MovementKind.INTEREST, False, True
    elif concept.startswith("transferencia inmediata"):
        kind, expected_debit, forbids_origin = MovementKind.TRANSFER_IN, False, False
    elif _TRANSFER_OUT_RE.match(concept) is not None:
        kind, expected_debit, forbids_origin = MovementKind.TRANSFER_OUT, True, False
    else:
        raise ExtractoConsolidadoParseError(
            f"line {line}: the movement row's concept is outside the known vocabulary"
        )

    if forbids_origin and has_origin:
        raise ExtractoConsolidadoParseError(
            f"line {line}: concept class {kind.value} never carries an origin code"
        )
    if is_debit != expected_debit:
        raise ExtractoConsolidadoParseError(
            f"line {line}: concept class {kind.value} has the wrong sign for its class"
        )
    return kind


# --------------------------------------------------------------------- section B (blocks)


@dataclass(frozen=True)
class _RawRow:
    dd: int
    mm: int
    origin: str
    concept: str
    kind: MovementKind
    amount: Decimal
    balance: Decimal
    page: int
    line: int


@dataclass(frozen=True)
class _Block:
    kind: str
    currency: str
    account: str
    opening: Decimal
    rows: tuple[_RawRow, ...]
    quiescent: bool
    close_day: int
    close_month: int
    close_amount: Decimal
    total_debits: Decimal
    total_credits: Decimal
    header_page: int
    header_line: int


def _is_furniture(stripped: str) -> bool:
    """Furniture lines that repeat between/around blocks and are always skippable."""
    if stripped == "":
        return True
    if stripped == _DETALLE_LINE:
        return True
    if _MOVIMIENTOS_EN_RE.match(stripped) is not None:
        return True
    if all(token in stripped for token in _LEGAL_LINE_TOKENS):
        return True
    if re.search(r"\d+\s+de\s+\d+\s+\S+\s+pagina\s+\d+\s+de\s+\d+\s*$", stripped) is not None:
        return True
    return re.search(r"\biva\b.*\bcuit\b\s+\S+\s+\d{11}\s*$", stripped) is not None


def _find_block_header_indices(rows: list[tuple[int, int, str]]) -> list[int]:
    return [
        index
        for index, (_page, _line, raw) in enumerate(rows)
        if _BLOCK_HEADER_RE.match(fold(raw.strip())) is not None
    ]


def _parse_block(rows: list[tuple[int, int, str]], start: int, end: int) -> tuple[_Block, int]:
    header_page, header_line, header_raw = rows[start]
    header_match = _BLOCK_HEADER_RE.match(fold(header_raw.strip()))
    assert header_match is not None  # caller guarantees this
    kind = header_match.group("kind")
    currency = header_match.group("currency")
    if currency not in _KNOWN_CURRENCIES:
        raise ExtractoConsolidadoParseError(
            f"page {header_page}, line {header_line}: the sub-account block carries an "
            f"unknown currency marker"
        )
    account = header_match.group("account")

    index = start + 1
    while index < end and fold(rows[index][2].strip()) == "":
        index += 1
    if index >= end or tuple(fold(rows[index][2].strip()).split()) != _TABLE_HEADER_TOKENS:
        raise ExtractoConsolidadoParseError(
            f"page {header_page}, line {header_line}: the sub-account block has no table "
            f"header line right after it"
        )
    index += 1

    opening: Decimal | None = None
    raw_rows: list[_RawRow] = []
    quiescent = False
    close_day: int | None = None
    close_month: int | None = None
    close_amount: Decimal | None = None

    while index < end:
        page, line, raw = rows[index]
        stripped = fold(raw.strip())
        if stripped == "":
            index += 1
            continue

        saldo_al_match = _SALDO_AL_RE.match(stripped)
        if saldo_al_match is not None:
            month = _MONTHS.get(saldo_al_match.group("month"))
            if month is None:
                raise ExtractoConsolidadoParseError(
                    f"line {line}: the SALDO AL line uses a month name outside the known table"
                )
            close_day = int(saldo_al_match.group("dd"))
            close_month = month
            close_amount = _amount(saldo_al_match.group("amount"), line, "close balance")
            index += 1
            break

        if opening is None:
            saldo_anterior_match = _SALDO_ANTERIOR_RE.match(stripped)
            if saldo_anterior_match is None:
                raise ExtractoConsolidadoParseError(
                    f"page {page}, line {line}: the sub-account block's table region does "
                    f"not open with SALDO ANTERIOR"
                )
            opening = _amount(saldo_anterior_match.group("amount"), line, "opening balance")
            index += 1
            continue

        sin_movimientos_match = _SIN_MOVIMIENTOS_RE.match(stripped)
        if sin_movimientos_match is not None:
            quiescent = True
            index += 1
            continue

        row_match = _ROW_RE.match(stripped)
        if row_match is None:
            raise ExtractoConsolidadoParseError(
                f"page {page}, line {line}: an unrecognized line appears inside the "
                f"movement table"
            )
        origin = row_match.group("origin") or ""
        movement_raw = row_match.group("movement")
        is_debit = movement_raw.startswith("-")
        magnitude = _amount(movement_raw[1:] if is_debit else movement_raw, line, "amount")
        concept_kind = _classify_concept(
            row_match.group("concept"), is_debit, bool(origin), line
        )
        raw_rows.append(
            _RawRow(
                dd=int(row_match.group("dd")),
                mm=int(row_match.group("mm")),
                origin=origin,
                concept=row_match.group("concept"),
                kind=concept_kind,
                amount=-magnitude if is_debit else magnitude,
                balance=_amount(row_match.group("balance"), line, "balance"),
                page=page,
                line=line,
            )
        )
        index += 1

    if opening is None or close_day is None or close_month is None or close_amount is None:
        raise ExtractoConsolidadoParseError(
            f"page {header_page}, line {header_line}: the sub-account block never reaches "
            f"a SALDO AL closing line"
        )

    while index < end and _is_furniture(fold(rows[index][2].strip())):
        index += 1
    if index >= end:
        raise ExtractoConsolidadoParseError(
            f"page {header_page}, line {header_line}: the sub-account block has no "
            f"TOTAL MOVIMIENTOS line"
        )
    total_match = _TOTAL_RE.match(fold(rows[index][2].strip()))
    if total_match is None:
        raise ExtractoConsolidadoParseError(
            f"page {header_page}, line {header_line}: the sub-account block has no "
            f"TOTAL MOVIMIENTOS line"
        )
    total_debits = _amount(total_match.group("debits"), rows[index][1], "total debits")
    total_credits = _amount(total_match.group("credits"), rows[index][1], "total credits")
    index += 1

    if currency != "$":
        row_shaped = bool(raw_rows) or not quiescent
        if row_shaped or opening != close_amount or total_debits != 0 or total_credits != 0:
            raise ExtractoConsolidadoParseError(
                f"page {header_page}, line {header_line}: a non-ARS sub-account block "
                f"carries movement; only a quiescent block is accepted"
            )

    block = _Block(
        kind=kind,
        currency=currency,
        account=account,
        opening=opening,
        rows=tuple(raw_rows),
        quiescent=quiescent,
        close_day=close_day,
        close_month=close_month,
        close_amount=close_amount,
        total_debits=total_debits,
        total_credits=total_credits,
        header_page=header_page,
        header_line=header_line,
    )
    return block, index


def _parse_blocks(rows: list[tuple[int, int, str]]) -> tuple[_Block, ...]:
    header_indices = _find_block_header_indices(rows)
    if not header_indices:
        raise ExtractoConsolidadoParseError("no sub-account movement block was found")
    transfer_header_index = next(
        (
            index
            for index, (_p, _l, raw) in enumerate(rows)
            if _TRANSFER_HEADER_RE.match(fold(raw.strip())) is not None
        ),
        len(rows),
    )
    blocks: list[_Block] = []
    for position, start in enumerate(header_indices):
        end = (
            header_indices[position + 1]
            if position + 1 < len(header_indices)
            else transfer_header_index
        )
        block, _next_index = _parse_block(rows, start, end)
        blocks.append(block)
    return tuple(blocks)


# --------------------------------------------------------------------- section C


@dataclass(frozen=True)
class _TransferRow:
    date: dt.date
    cuit: str
    amount: Decimal
    page: int
    line: int
    matched: bool = False


def _parse_transfer_rows(
    rows: list[tuple[int, int, str]], close_year: int, close_month: int
) -> tuple[_TransferRow, ...]:
    header_index = next(
        (
            index
            for index, (_p, _l, raw) in enumerate(rows)
            if _TRANSFER_HEADER_RE.match(fold(raw.strip())) is not None
        ),
        None,
    )
    if header_index is None:
        return ()

    transfers: list[_TransferRow] = []
    for page, line, raw in rows[header_index + 1 :]:
        stripped = raw.strip()
        match = _TRANSFER_ROW_RE.match(stripped)
        if match is None:
            break
        dd = int(match.group("dd"))
        mm = int(match.group("mm"))
        year = close_year if mm <= close_month else close_year - 1
        try:
            row_date = dt.date(year, mm, dd)
        except ValueError:
            raise ExtractoConsolidadoParseError(
                f"line {line}: a sent-transfer row's date is not a real date"
            ) from None
        transfers.append(
            _TransferRow(
                date=row_date,
                cuit=match.group("cuit"),
                amount=_amount(match.group("amount"), line, "amount"),
                page=page,
                line=line,
            )
        )
    return tuple(transfers)


# --------------------------------------------------------------------- close date resolution


def _resolve_close_date(
    close_day: int, close_month: int, anchor: dt.date, year: int | None
) -> dt.date:
    if year is not None:
        try:
            return dt.date(year, close_month, close_day)
        except ValueError:
            raise ExtractoConsolidadoParseError(
                "the close date built from the given year override is not a real date"
            ) from None

    candidate_year = anchor.year
    try:
        candidate = dt.date(candidate_year, close_month, close_day)
    except ValueError:
        raise ExtractoConsolidadoParseError("the SALDO AL close date is not a real date") from None
    if candidate > anchor:
        candidate_year -= 1
        try:
            candidate = dt.date(candidate_year, close_month, close_day)
        except ValueError:
            raise ExtractoConsolidadoParseError(
                "the SALDO AL close date is not a real date"
            ) from None
    if (anchor - candidate).days > 60:
        raise ExtractoConsolidadoParseError(
            "the anchor date is more than 60 days after the resolved close date"
        )
    return candidate


def _resolve_row_date(dd: int, mm: int, close_year: int, close_month: int) -> dt.date:
    year = close_year if mm <= close_month else close_year - 1
    return dt.date(year, mm, dd)


# --------------------------------------------------------------------- assembly & reconciliation


def _reconcile(
    blocks: Sequence[_Block],
    block_dates: Sequence[tuple[dt.date, ...]],
    block_close_dates: Sequence[dt.date],
    primary_close_date: dt.date,
    movements: Sequence[Movement],
    detail_match_detail: str,
    detail_match_ok: bool,
    transfer_match_detail: str,
    transfer_match_ok: bool,
) -> tuple[CheckResult, ...]:
    checks: list[CheckResult] = []

    chain_ok = True
    chain_detail = f"the running-balance chain holds across {len(blocks)} block(s)"
    for block_index, block in enumerate(blocks):
        previous = block.opening
        for row_index, row in enumerate(block.rows):
            if row.balance != previous + row.amount:
                chain_ok = False
                chain_detail = (
                    f"block {block_index + 1}, row {row_index + 1} (line {row.line}): the "
                    f"saldo does not equal the previous saldo plus the movement"
                )
                break
            previous = row.balance
        if not chain_ok:
            break
    checks.append(CheckResult("running-balance-chain", chain_ok, chain_detail))

    closing_ok = True
    closing_detail = f"opening plus movements equals SALDO AL across {len(blocks)} block(s)"
    for block_index, block in enumerate(blocks):
        total = block.opening + sum((row.amount for row in block.rows), Decimal(0))
        if total != block.close_amount:
            closing_ok = False
            closing_detail = (
                f"block {block_index + 1} (line {block.header_line}): SALDO ANTERIOR plus "
                f"its movements does not equal SALDO AL"
            )
            break
    checks.append(CheckResult("closing-balance", closing_ok, closing_detail))

    total_ok = True
    total_detail = f"TOTAL MOVIMIENTOS matches the summed debits/credits in {len(blocks)} block(s)"
    for block_index, block in enumerate(blocks):
        sum_debits = -sum((row.amount for row in block.rows if row.amount < 0), Decimal(0))
        sum_credits = sum((row.amount for row in block.rows if row.amount > 0), Decimal(0))
        if sum_debits != block.total_debits or sum_credits != block.total_credits:
            total_ok = False
            total_detail = (
                f"block {block_index + 1} (line {block.header_line}): TOTAL MOVIMIENTOS "
                f"does not equal the summed debit/credit rows"
            )
            break
    checks.append(CheckResult("total-movements", total_ok, total_detail))

    ascending_ok = True
    ascending_detail = f"every block's rows are in non-decreasing date order across {len(blocks)} block(s)"
    for block_index, dates in enumerate(block_dates):
        for row_index in range(1, len(dates)):
            if dates[row_index] < dates[row_index - 1]:
                ascending_ok = False
                ascending_detail = (
                    f"block {block_index + 1}, row {row_index + 1}: date order breaks"
                )
                break
        if not ascending_ok:
            break
    checks.append(CheckResult("ascending-dates", ascending_ok, ascending_detail))

    window_ok = True
    window_detail = f"every block's rows fall inside its own window across {len(blocks)} block(s)"
    for block_index, (block, dates) in enumerate(zip(blocks, block_dates, strict=True)):
        if not dates:
            continue
        window_start, window_end = dates[0], block_close_dates[block_index]
        for row_index, date in enumerate(dates):
            if not (window_start <= date <= window_end):
                window_ok = False
                window_detail = (
                    f"block {block_index + 1}, row {row_index + 1}: date falls outside "
                    f"the block's own window"
                )
                break
        if not window_ok:
            break
    checks.append(CheckResult("dates-in-window", window_ok, window_detail))

    agreement_ok = all(date == primary_close_date for date in block_close_dates)
    agreement_detail = (
        f"all {len(blocks)} block(s) agree on the resolved SALDO AL date"
        if agreement_ok
        else f"at least one of {len(blocks)} block(s) disagrees on the resolved SALDO AL date"
    )
    checks.append(CheckResult("saldo-al-agreement", agreement_ok, agreement_detail))

    keys = movement_keys(movements)
    duplicate_count = len(keys) - len(set(keys))
    keys_ok = duplicate_count == 0
    keys_detail = (
        f"{len(keys)} natural key(s) are unique within the batch"
        if keys_ok
        else f"{duplicate_count} natural key(s) repeat within the batch"
    )
    checks.append(CheckResult("keys-unique", keys_ok, keys_detail))

    checks.append(CheckResult("debit-card-details-matched", detail_match_ok, detail_match_detail))
    checks.append(CheckResult("transfers-matched", transfer_match_ok, transfer_match_detail))

    return tuple(checks)


def parse_extracto_consolidado(
    text: str, *, anchor: dt.date, year: int | None = None
) -> ExtractoConsolidado:
    """Parse and reconcile one extracted BBVA ``Extracto consolidado``.

    Args:
        anchor: the caller's clock proxy (e.g. the source PDF's CreationDate).
            Used to resolve the close's year when ``year`` is not given.
        year: an explicit override for the close's year. When given, the
            60-day anchor ceiling does not apply.

    Raises:
        ExtractoConsolidadoParseError: the text is not a parsable
            ``Extracto consolidado``, or the close date cannot be resolved.
            The message is masked.
        ReconciliationError: the document parsed but a check failed. The
            error carries every check and the failed subset; nothing partial
            is returned.
    """
    rows = _layout(text)
    details = _parse_debit_card_details(rows)
    blocks = _parse_blocks(rows)

    primary_close_date = _resolve_close_date(
        blocks[0].close_day, blocks[0].close_month, anchor, year
    )
    close_year = primary_close_date.year

    block_dates: list[tuple[dt.date, ...]] = []
    block_close_dates: list[dt.date] = []
    movements: list[Movement] = []
    for block in blocks:
        block_close_date = _resolve_row_date(
            block.close_day, block.close_month, close_year, primary_close_date.month
        )
        block_close_dates.append(block_close_date)
        dates: list[dt.date] = []
        for row in block.rows:
            row_date = _resolve_row_date(row.dd, row.mm, close_year, primary_close_date.month)
            dates.append(row_date)
            movements.append(
                Movement(
                    date=row_date,
                    account=block.account,
                    origin=row.origin,
                    concept=row.concept,
                    amount=row.amount,
                    balance=row.balance,
                    page=row.page,
                    line=row.line,
                    kind=row.kind,
                    merchant="",
                    recipient_cuit="",
                )
            )
        block_dates.append(tuple(dates))

    transfers = _parse_transfer_rows(rows, close_year, primary_close_date.month)

    # -------------------------------------------------- debit-card detail matching
    ars_movement_dates = [
        date
        for block, dates in zip(blocks, block_dates, strict=True)
        if block.currency == "$"
        for date in dates
    ]
    window_start = min(ars_movement_dates) if ars_movement_dates else primary_close_date
    detail_candidates: dict[tuple[dt.date, Decimal], list[_DetailRow]] = {}
    for detail in details:
        detail_candidates.setdefault((detail.date, detail.amount), []).append(detail)

    detail_match_ok = True
    detail_match_detail = "every debit-card purchase row matches exactly one detail row"
    used_details: set[tuple[dt.date, Decimal, int]] = set()
    final_movements: list[Movement] = []
    for movement in movements:
        if movement.kind is not MovementKind.DEBIT_CARD_PURCHASE:
            final_movements.append(movement)
            continue
        candidates = detail_candidates.get((movement.date, -movement.amount), [])
        if len(candidates) != 1:
            detail_match_ok = False
            detail_match_detail = (
                f"a debit-card purchase row (line {movement.line}) has "
                f"{len(candidates)} detail candidate(s), expected exactly 1"
            )
            final_movements.append(movement)
            continue
        detail = candidates[0]
        used_details.add((detail.date, detail.amount, detail.line))
        final_movements.append(replace(movement, merchant=detail.merchant))
    movements = final_movements

    if detail_match_ok:
        for detail in details:
            if not (window_start <= detail.date <= primary_close_date):
                continue
            if (detail.date, detail.amount, detail.line) not in used_details:
                detail_match_ok = False
                detail_match_detail = (
                    f"a debit-card detail row (line {detail.line}) inside the table window "
                    f"has no matching purchase row"
                )
                break

    # -------------------------------------------------------- sent-transfer matching
    transfer_candidates: dict[tuple[dt.date, Decimal], list[_TransferRow]] = {}
    for transfer in transfers:
        transfer_candidates.setdefault((transfer.date, transfer.amount), []).append(transfer)

    transfer_match_ok = True
    transfer_match_detail = "every TRANSFERENCIA-out row matches exactly one sent-transfer row"
    used_transfers: set[tuple[dt.date, Decimal, int]] = set()
    final_movements = []
    for movement in movements:
        if movement.kind is not MovementKind.TRANSFER_OUT:
            final_movements.append(movement)
            continue
        candidates = transfer_candidates.get((movement.date, -movement.amount), [])
        if len(candidates) != 1:
            transfer_match_ok = False
            transfer_match_detail = (
                f"a TRANSFERENCIA-out row (line {movement.line}) has {len(candidates)} "
                f"sent-transfer candidate(s), expected exactly 1"
            )
            final_movements.append(movement)
            continue
        transfer = candidates[0]
        used_transfers.add((transfer.date, transfer.amount, transfer.line))
        final_movements.append(replace(movement, recipient_cuit=transfer.cuit))
    movements = final_movements

    if transfer_match_ok:
        for transfer in transfers:
            if (transfer.date, transfer.amount, transfer.line) not in used_transfers:
                transfer_match_ok = False
                transfer_match_detail = (
                    f"a sent-transfer row (line {transfer.line}) has no matching "
                    f"TRANSFERENCIA-out row"
                )
                break

    checks = _reconcile(
        blocks,
        block_dates,
        block_close_dates,
        primary_close_date,
        movements,
        detail_match_detail,
        detail_match_ok,
        transfer_match_detail,
        transfer_match_ok,
    )
    if any(not check.ok for check in checks):
        raise ReconciliationError(checks)

    return ExtractoConsolidado(
        close_date=primary_close_date,
        movements=tuple(movements),
        checks=checks,
        blocks=tuple(
            AccountBlock(kind=block.kind, currency=block.currency, account=block.account)
            for block in blocks
        ),
    )
