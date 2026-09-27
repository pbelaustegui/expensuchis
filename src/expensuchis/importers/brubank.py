"""Parser for Brubank's ``Resumen de Movimientos`` account statement.

Pure over extracted text: no PDF library, no filesystem, no clock and no
environment. The caller (the importer's ``extract``, a later task, or the
acceptance probe) owns text extraction; this module owns meaning.

The parser is a **gate**, not a reader. :func:`parse_resumen` reconciles the
document against itself and raises :class:`ReconciliationError` whenever any
check fails; it never returns a partially trusted result. There is no
``strict=False``: the caller cannot opt out of a check it does not like.

What the document looks like
----------------------------

Reconnaissance against one real file (masked, shape-only; see
``odd/tasks/family-ledger.md``, "T-08 reconnaissance") found: a header block
naming ``Saldo Inicial``, ``Saldo Final``, ``Créditos``, ``Débitos`` and an
``Imp. Trans. Financieras`` total, opening the document; a movement table whose
columns are **``Fecha | #Ref | Descripción | Débito | Crédito | Saldo``**; and a
footer period line, ``dd Mon yyyy al dd Mon yyyy`` with Spanish month
abbreviations. The exact surrounding text of the header and footer lines (labels
on their own line versus inline with a leading currency symbol, extra metadata
lines such as ``CUIT`` or ``CBU``) was not confirmed by the reconnaissance, which
mapped column geometry, not line text; this module's header/footer/period
line shapes are therefore this unit's best-supported reading of the recon, not a
transcription of the file, and are recorded as a residual risk.

The header block is not a one-time preamble: it **repeats verbatim as a recap at
the end of the last movement page** (reconnaissance: "the balance header block
... opens page 1 and repeats as a recap at the end of page 3"). Both copies are
parsed, and a mismatch between them refuses the document — picking one over the
other would silently trust an inconsistent statement.

A movement is always exactly one line — the real file carries no wrapped
descriptions (reconnaissance: "no wrapped descriptions in this file") — of the
shape ``dd-mm-aa #Ref Descripción Débito Crédito Saldo``. The date's two-digit
year is read as ``20aa`` (owner decision). Direction is carried **only** by
which of Débito/Crédito holds a value; the unused column reads ``-`` and there is
**no minus sign anywhere** in the document (reconnaissance). A row carrying a
value in both columns, in neither, or a minus sign in an amount position, is not
guessed at: it is refused, because the running balance is the only thing that
could catch a misread column here (reconnaissance) and this module never invents
a reading strictness cannot support. That is also why an unrecognized row is
always a refusal rather than a silent drop — the recurring CRITICAL class of
T-06b (see ``odd/tasks/family-ledger.md``): a document line the parser cannot
place is a movement it failed to understand, never furniture by default.

The table header (``Fecha #Ref Descripción Débito Crédito Saldo``) and the
footer's period line repeat on every page. Each page has a **table region**: it
starts right after a table-header line and ends at the recap block's first
field, the footer/period line, or the end of the page, whichever comes first.
Inside that region every line must be exactly one of a movement row, a repeated
table header, the footer/period line, or blank — never silently skipped just
because it does not start with a date. The region closes only on a line that
*is* one of those closing shapes, not merely a line that resembles one: a
header/recap field line closes it only when its label is one of the five known
labels, and the footer/period line closes it only when the whole line matches
that shape, not a period-shaped substring inside a longer line. Only *outside*
the region (the header block that opens the document, and a page that never
carries a table header at all, such as the trailing legal prose) does an
unrecognized line stay unremarkable furniture — except a movement-row-shaped
line, which is never furniture and always refuses. See :func:`_parse_movements`.

Special rows
------------

* ``Intereses pagados`` is a normal credit row (reconnaissance: one occurrence,
  a credit); it is tagged :attr:`MovementKind.INTEREST` so the importer (a later
  unit) can post it to ``Income:<person>:Intereses``, as Provincia does.
* ``De una cuenta tuya - <bank>`` is an internal transfer between the
  statement owner's own accounts (2026-09-24 clearing-account decision, forced
  by the real ``De una cuenta tuya - BBVA`` row appearing in both the Brubank
  and the BBVA statements). It is tagged :attr:`MovementKind.INTERNAL_TRANSFER`
  and carries the counterpart bank word in :attr:`Movement.transfer_bank`, so the
  importer can post the counterpart to the shared clearing account.
* ``Imp. Trans. Financieras`` is a **header-only total** (owner decision): the
  real file never carries it as a row. A row whose description is exactly that
  phrase refuses the parse rather than being posted or silently dropped.

Every other row is :attr:`MovementKind.ORDINARY`: the description names a
counterparty (a person or a service provider), not a movement type, so
categorization here is name matching, never verb parsing (unlike Provincia).

Reconciliation
--------------

Every listed condition is computed and reported even after an earlier one
fails, so one run tells the whole story:

* ``header-balance-equation`` — the header's own opening balance, plus its
  declared credits, minus its declared debits, equals its declared closing
  balance;
* ``declared-credits`` / ``declared-debits`` — the sum of the credit (resp.
  debit) rows equals the header's declared total;
* ``running-balance-chain`` — each row's saldo equals the previous saldo (the
  opening balance for the first row) plus that row's signed amount;
* ``ascending-dates`` — rows are in non-decreasing date order;
* ``dates-in-period`` — every row's date falls inside the declared period;
* ``refs-unique`` — every ``#Ref`` is distinct within the file.

Masking
-------

The real statements are household data and the models that run this workflow
are remote. **No diagnostic may echo a raw statement line.** Every
:class:`CheckResult` detail and every error message in this module reports line
numbers, movement indexes, counts and field names only — never a date, a
description, a reference, an amount or a balance.

The natural key
----------------

:func:`movement_keys` builds each movement's identity: ``<date>:<#Ref>:<amount>``.
The owner decided ``#Ref`` is the identity part (periods are user-chosen and can
overlap between statements, so the date and amount alone are not enough), its
global uniqueness across files to be re-checked when a second, overlapping
statement arrives. Unlike Provincia's ``movement_keys``, no occurrence suffix is
needed: the ``refs-unique`` check already guarantees every ``#Ref`` is distinct
within one file, and a shared ``#Ref`` across two overlapping files is exactly
the dedup signal the pipeline wants — it means the same transaction, not two
different ones that happen to collide.
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

__all__ = [
    "CHECK_NAMES",
    "CheckResult",
    "Movement",
    "MovementKind",
    "ReconciliationError",
    "Resumen",
    "ResumenParseError",
    "fold",
    "is_resumen_movimientos",
    "movement_keys",
    "parse_resumen",
]


class MovementKind(Enum):
    """The closed movement classification of the Brubank statement.

    Unlike Provincia, this is not a verb vocabulary: the description names a
    counterparty, not a movement type, so almost every row is
    :attr:`ORDINARY` and its posting is name matching (a later unit's job).
    Only two shapes are structurally distinguished here because a later unit
    needs to post them differently: :attr:`INTEREST` (interest paid, a credit)
    and :attr:`INTERNAL_TRANSFER` (money moving between the owner's own
    accounts, the clearing-account case).
    """

    ORDINARY = "ordinary"
    INTEREST = "intereses"
    INTERNAL_TRANSFER = "internal_transfer"


#: The checks, in the fixed order they are reported. See the module docstring,
#: "Reconciliation", for what each one means.
CHECK_NAMES: tuple[str, ...] = (
    "header-balance-equation",
    "declared-credits",
    "declared-debits",
    "running-balance-chain",
    "ascending-dates",
    "dates-in-period",
    "refs-unique",
)

#: An Argentine amount without sign or currency symbol: dot-grouped integer,
#: exactly two comma decimals. There is no minus-sign variant on purpose — the
#: document carries none (reconnaissance), so a negative-looking token simply
#: fails to match and the row is refused as unrecognized, never parsed as a
#: signed amount.
_ARS_AMOUNT = r"\d{1,3}(?:\.\d{3})*,\d{2}"

_DATE_PREFIX_RE = re.compile(r"^\d{2}-\d{2}-\d{2}\s")
_ROW_RE = re.compile(
    rf"^(?P<day>\d{{2}})-(?P<month>\d{{2}})-(?P<year>\d{{2}})\s+"
    rf"(?P<ref>\d{{10}})\s+"
    rf"(?P<description>.+?)\s+"
    rf"(?P<debito>-|{_ARS_AMOUNT})\s+"
    rf"(?P<credito>-|{_ARS_AMOUNT})\s+"
    rf"(?P<saldo>{_ARS_AMOUNT})\s*$"
)
#: A header/recap field line: a label (any run of non-digit characters) then an
#: optional ``$`` and an Argentine amount. Matched structurally so the label's
#: exact accenting or a stray ``$`` never matters; :data:`_HEADER_LABELS` maps
#: the folded label to the field it names.
_HEADER_FIELD_RE = re.compile(rf"^(?P<label>[^\d\n]+?)\s*\$?\s*(?P<amount>{_ARS_AMOUNT})\s*$")
_HEADER_LABELS: dict[str, str] = {
    "saldo inicial": "opening",
    "saldo final": "closing",
    "creditos": "declared_credits",
    "debitos": "declared_debits",
    "imp. trans. financieras": "financial_transactions_tax",
}
#: The footer's period line: ``dd Mon yyyy al dd Mon yyyy``. Not anchored on its
#: own, so :func:`_period` may find it sitting inside a longer footer line
#: (e.g. a ``Período: ...`` label) without this module needing to know the
#: exact surrounding wording.
_PERIOD_RE = re.compile(
    r"(?P<d1>\d{1,2})\s+(?P<m1>\S+)\s+(?P<y1>\d{4})\s+al\s+"
    r"(?P<d2>\d{1,2})\s+(?P<m2>\S+)\s+(?P<y2>\d{4})",
    re.IGNORECASE,
)
#: The *whole* footer line: an optional non-digit label (as loose as
#: :data:`_HEADER_FIELD_RE`'s label, so a ``Período:`` prefix still matches)
#: followed by the period shape and nothing else. Unlike :data:`_PERIOD_RE`,
#: this is matched with ``fullmatch`` — used only to decide whether a line
#: *is* the footer line (closing the table region), never to extract the
#: period itself, which stays :func:`_period`'s job via ``search``.
_FOOTER_LINE_RE = re.compile(
    rf"^(?:[^\d\n]+\s)?{_PERIOD_RE.pattern}\s*$",
    re.IGNORECASE,
)
#: The repeated table header, folded into tokens so a stray extra space or a
#: rendering difference in the accents never matters. See :func:`_is_table_header`.
_TABLE_HEADER_TOKENS = ("fecha", "#ref", "descripcion", "debito", "credito", "saldo")

#: The special descriptions, folded (casefolded, accents stripped) for matching.
_FORBIDDEN_DESCRIPTION = "imp. trans. financieras"
_INTEREST_DESCRIPTION = "intereses pagados"
_INTERNAL_TRANSFER_RE = re.compile(r"^de una cuenta tuya\s*-\s*(?P<bank>.+)$")
_INTERNAL_TRANSFER_PREFIX_RE = re.compile(r"^de una cuenta tuya\s*-\s*", re.IGNORECASE)

#: Spanish month names and their three-letter abbreviations, folded, for the
#: footer's period line. Kept local to this module (Provincia's card-liquidación
#: parser, ``provincia_visa.py``, keeps its own equivalent table too): each
#: parser is self-contained and pure over text.
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
    """Casefold and strip accents: ``CRÉDITOS`` and ``creditos`` become ``creditos``."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def is_resumen_movimientos(text: str) -> bool:
    """Whether ``text`` is a Brubank ``Resumen de Movimientos``, by its title line.

    Structural, not a substring search over the whole document: a single line
    whose folded tokens include both ``resumen`` and ``movimientos``. Disjoint
    from Provincia's ``Extracto de Cuenta`` marker and from the card
    liquidación's vocabulary (``liquidacion``, ``consumos``, ...). Two words
    that happen to land on different lines are not the title.
    """
    for line in _normalize(text).split("\n"):
        tokens = set(fold(line).split())
        if "resumen" in tokens and "movimientos" in tokens:
            return True
    return False


@dataclass(frozen=True)
class Movement:
    """One reconciled movement, with the position it came from for diagnostics.

    ``amount`` is signed (positive for a credit, negative for a debit) despite
    the document itself carrying no minus sign — the sign is reconstructed from
    which column (Débito or Crédito) held the value, so the running-balance
    chain can be checked the same way as every other importer in this project.
    ``transfer_bank`` is non-empty only when ``kind`` is
    :attr:`MovementKind.INTERNAL_TRANSFER`; it is empty otherwise.
    """

    date: dt.date
    reference: str
    description: str
    amount: Decimal
    balance: Decimal
    page: int
    line: int
    kind: MovementKind
    transfer_bank: str


@dataclass(frozen=True)
class CheckResult:
    """One reconciliation check and its **masked** detail."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Resumen:
    """A fully reconciled statement. Only :func:`parse_resumen` builds one.

    ``opening``, ``closing``, ``declared_credits`` and ``declared_debits`` are
    the header's own figures (confirmed equal to their end-of-document recap by
    :func:`parse_resumen`). ``financial_transactions_tax`` is the header's
    ``Imp. Trans. Financieras`` total, kept as data: it is never posted (owner
    decision) and never appears as a movement.
    """

    period_start: dt.date
    period_end: dt.date
    opening: Decimal
    closing: Decimal
    declared_credits: Decimal
    declared_debits: Decimal
    financial_transactions_tax: Decimal
    movements: tuple[Movement, ...]
    checks: tuple[CheckResult, ...]


class ResumenParseError(ValueError):
    """The text is not a parsable ``Resumen de Movimientos``.

    Structural failure, distinct from :class:`ReconciliationError`: a header or
    recap field is missing, duplicated beyond the expected opening+recap pair,
    or the two copies disagree; the period line is missing or its repeated
    copies disagree; a movement row has no recognized shape, carries a value in
    both or neither of Débito/Crédito, carries an amount this module cannot
    parse (including one that would need a minus sign), or its description is
    the forbidden header-only ``Imp. Trans. Financieras`` total. The message
    never echoes statement text.
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
            f"Brubank reconciliation failed: {names}. "
            f"Refusing the document; a partially trusted parse is never returned."
        )


def movement_keys(movements: Sequence[Movement]) -> tuple[str, ...]:
    """Return the natural ``key`` of every movement, in document order.

    ``<date>:<#Ref>:<amount>`` — see the module docstring, "The natural key".
    """
    return tuple(
        f"{movement.date.isoformat()}:{movement.reference}:{movement.amount:.2f}"
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
        # ``from None`` on purpose: the AmountParseError message echoes the raw
        # number, and a diagnostic must never carry statement text.
        raise ResumenParseError(
            f"line {line}: the {field} is not a well-formed Argentine amount (length {len(raw)})"
        ) from None


def _header_field(stripped: str) -> tuple[str, str] | None:
    """Return ``(field, raw_amount)`` when ``stripped`` is a known header/recap line.

    Matched structurally by :data:`_HEADER_FIELD_RE` first (any non-digit label
    then an amount), then the folded label is checked against
    :data:`_HEADER_LABELS`: a coincidental label-shaped line — any run of
    non-digit text immediately followed by an amount, which a stray note or a
    wrapped fragment inside the movement table can also look like — never
    counts as a header field unless it names one of the five known ones.
    Shared by :func:`_header_block` and :func:`_parse_movements`'s table-region
    close, so the two can never disagree on what a header field is.
    """
    match = _HEADER_FIELD_RE.match(stripped)
    if match is None:
        return None
    label = fold(match.group("label")).strip()
    field = _HEADER_LABELS.get(label)
    if field is None:
        return None
    return field, match.group("amount")


def _header_block(rows: list[tuple[int, int, str]]) -> dict[str, Decimal]:
    """Return the five header fields, requiring each to appear exactly twice and agree.

    The header block opens the document and repeats verbatim as a recap at the
    end of the last movement page (reconnaissance). Both copies are required —
    not "at least one" — because a document that dropped its recap, or grew a
    third copy, is not a shape this module has confirmed; refusing is safer
    than picking a copy.
    """
    occurrences: dict[str, list[tuple[int, Decimal]]] = {
        field: [] for field in _HEADER_LABELS.values()
    }
    for _page, line, raw in rows:
        header_field = _header_field(raw.strip())
        if header_field is None:
            continue
        field, amount_raw = header_field
        occurrences[field].append((line, _amount(amount_raw, line, field)))

    result: dict[str, Decimal] = {}
    for field, entries in occurrences.items():
        if len(entries) != 2:
            raise ResumenParseError(
                f"the '{field}' header field appears {len(entries)} time(s); expected exactly 2 "
                f"(the opening block and the last page's recap)"
            )
        (opening_line, opening_value), (recap_line, recap_value) = entries
        if opening_value != recap_value:
            raise ResumenParseError(
                f"the '{field}' header field's recap (line {recap_line}) does not equal its "
                f"opening block value (line {opening_line}); refusing rather than picking one"
            )
        result[field] = opening_value
    return result


def _period_date(
    match: re.Match[str], day_group: str, month_group: str, year_group: str, line: int
) -> dt.date:
    month = _MONTHS.get(fold(match.group(month_group)))
    if month is None:
        raise ResumenParseError(
            f"line {line}: the period line uses a month name outside the known table"
        )
    try:
        return dt.date(int(match.group(year_group)), month, int(match.group(day_group)))
    except ValueError:
        raise ResumenParseError(
            f"line {line}: the period line carries a date that does not exist"
        ) from None


def _period(rows: list[tuple[int, int, str]]) -> tuple[dt.date, dt.date]:
    """Return ``(period_start, period_end)``, requiring every occurrence to agree.

    The footer's period line repeats on every movement page (reconnaissance).
    Disagreement between copies is refused rather than resolved by picking one.
    """
    found: list[tuple[int, dt.date, dt.date]] = []
    for _page, line, raw in rows:
        match = _PERIOD_RE.search(raw)
        if match is None:
            continue
        start = _period_date(match, "d1", "m1", "y1", line)
        end = _period_date(match, "d2", "m2", "y2", line)
        found.append((line, start, end))

    if not found:
        raise ResumenParseError(
            "missing the statement period: no 'dd Mon yyyy al dd Mon yyyy' line was found"
        )
    first_line, first_start, first_end = found[0]
    for line, start, end in found[1:]:
        if (start, end) != (first_start, first_end):
            raise ResumenParseError(
                f"the statement period at line {line} does not agree with the period declared "
                f"at line {first_line}; refusing rather than picking one"
            )
    return first_start, first_end


def _is_table_header(line: str) -> bool:
    """Whether ``line`` is the repeated table header, matched by exact folded shape."""
    return tuple(fold(line).split()) == _TABLE_HEADER_TOKENS


def _classify(description: str) -> tuple[MovementKind, str]:
    folded = fold(description)
    if folded == _INTEREST_DESCRIPTION:
        return MovementKind.INTEREST, ""
    if _INTERNAL_TRANSFER_RE.match(folded) is not None:
        bank = _INTERNAL_TRANSFER_PREFIX_RE.sub("", description, count=1).strip()
        return MovementKind.INTERNAL_TRANSFER, bank
    return MovementKind.ORDINARY, ""


def _movement_from_line(raw: str, page: int, line: int) -> Movement:
    match = _ROW_RE.match(raw)
    if match is None:
        raise ResumenParseError(f"line {line}: a movement row has an unrecognized shape")

    debito_raw = match.group("debito")
    credito_raw = match.group("credito")
    is_debit = debito_raw != "-"
    is_credit = credito_raw != "-"
    if is_debit == is_credit:
        raise ResumenParseError(
            f"line {line}: a movement row must carry exactly one of Débito/Crédito, "
            f"never both or neither"
        )

    try:
        movement_date = dt.date(
            2000 + int(match.group("year")), int(match.group("month")), int(match.group("day"))
        )
    except ValueError:
        raise ResumenParseError(f"line {line}: the leading date is not a real date") from None

    description = " ".join(match.group("description").split())
    if fold(description) == _FORBIDDEN_DESCRIPTION:
        raise ResumenParseError(
            f"line {line}: 'Imp. Trans. Financieras' is a header-only total and must never "
            f"appear as a movement row"
        )

    magnitude = _amount(debito_raw if is_debit else credito_raw, line, "amount")
    balance = _amount(match.group("saldo"), line, "balance")
    kind, transfer_bank = _classify(description)

    return Movement(
        date=movement_date,
        reference=match.group("ref"),
        description=description,
        amount=-magnitude if is_debit else magnitude,
        balance=balance,
        page=page,
        line=line,
        kind=kind,
        transfer_bank=transfer_bank,
    )


def _parse_movements(rows: list[tuple[int, int, str]]) -> list[Movement]:
    """Parse every movement row, refusing any unrecognized line inside the table region.

    A page's **table region** starts right after a table-header line and ends at
    the recap block's first field, the footer/period line, or the end of the
    page — whichever comes first. The region closes **only** on a line that is
    structurally one of those two things, checked the same way the rest of the
    module checks them: a header/recap field line closes it only when its
    folded label is exactly one of the five known labels
    (:func:`_header_field`, shared with :func:`_header_block` so the two can
    never disagree), and the footer/period line closes it only when the *whole*
    line matches the footer shape (:data:`_FOOTER_LINE_RE`, ``fullmatch``, not
    a substring search). A line that merely resembles either shape — an
    unknown label followed by an amount, or a line that happens to carry a
    period-shaped substring among other text — is not a close: it falls
    through to the final, unconditional refusal below, exactly like any other
    unrecognized line.

    Outside the region (the header block that opens the document, and a page
    such as the trailing legal prose that never carries a table header at all)
    a line that matches nothing stays unremarkable furniture, as it always
    has — *except* a movement-row-shaped line (a date prefix), which is never
    furniture: a movement row that appears once the region has closed (a stray
    row after the recap, for instance) refuses rather than being silently
    dropped, the same way a movement row inside a now-broken region would.

    Inside the region, every line must be exactly one of: a movement row, a
    repeated table header, the footer/period line, or blank. Anything else —
    a stray fragment, a wrapped continuation, an unexplained line — refuses
    rather than being silently dropped: a dropped non-monetary line would pass
    every reconciliation check while still truncating a movement, the same
    silent-drop class the card-liquidación parser's reconnaissance (T-06b)
    flagged as its recurring CRITICAL finding.
    """
    movements: list[Movement] = []
    in_region = False
    current_page: int | None = None
    for page, line, raw in rows:
        if page != current_page:
            current_page = page
            in_region = False

        stripped = raw.strip()

        if not in_region:
            if _is_table_header(stripped):
                in_region = True
                continue
            if _DATE_PREFIX_RE.match(stripped) is not None:
                raise ResumenParseError(
                    f"page {page}, line {line}: a movement-row-shaped line appears outside "
                    f"any movement table"
                )
            continue

        if stripped == "":
            continue
        if _is_table_header(stripped):
            continue
        if _DATE_PREFIX_RE.match(stripped) is not None:
            movements.append(_movement_from_line(stripped, page, line))
            continue
        if _FOOTER_LINE_RE.fullmatch(stripped) is not None:
            in_region = False
            continue
        if _header_field(stripped) is not None:
            in_region = False
            continue

        raise ResumenParseError(
            f"page {page}, line {line}: an unrecognized line appears inside the movement table"
        )
    return movements


def _reconcile(
    opening: Decimal,
    closing: Decimal,
    declared_credits: Decimal,
    declared_debits: Decimal,
    movements: list[Movement],
    period_start: dt.date,
    period_end: dt.date,
) -> tuple[CheckResult, ...]:
    """Run all checks and return them in :data:`CHECK_NAMES` order.

    Every check is computed even after an earlier one fails, so one run tells
    the whole story. Details are masked: counts, movement indexes and line
    numbers only.
    """
    checks: list[CheckResult] = []

    equation_ok = opening + declared_credits - declared_debits == closing
    checks.append(
        CheckResult(
            "header-balance-equation",
            equation_ok,
            "the header's opening balance plus its declared credits minus its declared debits "
            + ("equals" if equation_ok else "does not equal")
            + " its declared closing balance",
        )
    )

    credit_movements = [movement for movement in movements if movement.amount > 0]
    sum_credits = sum((movement.amount for movement in credit_movements), Decimal(0))
    credits_ok = sum_credits == declared_credits
    checks.append(
        CheckResult(
            "declared-credits",
            credits_ok,
            f"the sum of the {len(credit_movements)} credit row(s) "
            + ("equals" if credits_ok else "does not equal")
            + " the header's declared Créditos total",
        )
    )

    debit_movements = [movement for movement in movements if movement.amount < 0]
    sum_debits = -sum((movement.amount for movement in debit_movements), Decimal(0))
    debits_ok = sum_debits == declared_debits
    checks.append(
        CheckResult(
            "declared-debits",
            debits_ok,
            f"the sum of the {len(debit_movements)} debit row(s) "
            + ("equals" if debits_ok else "does not equal")
            + " the header's declared Débitos total",
        )
    )

    chain_ok = True
    previous = opening
    chain_detail = (
        f"balance chain holds from the opening balance across {len(movements)} movement(s)"
    )
    for index, movement in enumerate(movements):
        if movement.balance != previous + movement.amount:
            chain_ok = False
            chain_detail = (
                f"balance chain breaks at movement {index + 1} (line {movement.line}): the saldo "
                f"does not equal the previous saldo plus the movement's signed amount"
            )
            break
        previous = movement.balance
    checks.append(CheckResult("running-balance-chain", chain_ok, chain_detail))

    ascending_ok = True
    ascending_detail = f"all {len(movements)} movement(s) are in non-decreasing date order"
    for index in range(1, len(movements)):
        if movements[index].date < movements[index - 1].date:
            ascending_ok = False
            ascending_detail = (
                f"date order breaks at movement {index + 1} (line {movements[index].line}): its "
                f"date precedes the previous movement's date"
            )
            break
    checks.append(CheckResult("ascending-dates", ascending_ok, ascending_detail))

    out_of_period = [
        movement for movement in movements if not (period_start <= movement.date <= period_end)
    ]
    period_ok = not out_of_period
    if period_ok:
        period_detail = f"all {len(movements)} movement(s) fall inside the declared period"
    else:
        first = out_of_period[0]
        period_detail = (
            f"movement {movements.index(first) + 1} (line {first.line}) falls outside the "
            f"declared period; {len(out_of_period)} total"
        )
    checks.append(CheckResult("dates-in-period", period_ok, period_detail))

    references = [movement.reference for movement in movements]
    duplicate_count = len(references) - len(set(references))
    refs_ok = duplicate_count == 0
    refs_detail = (
        f"{len(references)} #Ref value(s) are unique within the file"
        if refs_ok
        else f"{duplicate_count} #Ref value(s) repeat within the file"
    )
    checks.append(CheckResult("refs-unique", refs_ok, refs_detail))

    return tuple(checks)


def parse_resumen(text: str) -> Resumen:
    """Parse and reconcile one extracted statement.

    Raises:
        ResumenParseError: the text is not a ``Resumen de Movimientos``: a
            header/recap field is missing, duplicated beyond the opening+recap
            pair, or the two copies disagree; the period line is missing or its
            copies disagree; or a movement row has no recognized shape, carries
            a value in both or neither of Débito/Crédito, carries a malformed
            amount, or is the forbidden ``Imp. Trans. Financieras`` row. The
            message is masked.
        ReconciliationError: the document parsed but a check failed. The error
            carries every check and the failed subset; nothing partial is
            returned.
    """
    rows = _layout(text)
    header = _header_block(rows)
    period_start, period_end = _period(rows)
    movements = _parse_movements(rows)
    checks = _reconcile(
        header["opening"],
        header["closing"],
        header["declared_credits"],
        header["declared_debits"],
        movements,
        period_start,
        period_end,
    )
    if any(not check.ok for check in checks):
        raise ReconciliationError(checks)
    return Resumen(
        period_start=period_start,
        period_end=period_end,
        opening=header["opening"],
        closing=header["closing"],
        declared_credits=header["declared_credits"],
        declared_debits=header["declared_debits"],
        financial_transactions_tax=header["financial_transactions_tax"],
        movements=tuple(movements),
        checks=checks,
    )
