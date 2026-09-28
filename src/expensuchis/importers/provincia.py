"""Parser for Banco Provincia's ``Extracto de cuenta`` statement.

Pure over extracted text: no PDF library, no filesystem, no clock and no
environment. The caller (the importer's ``extract``, or the acceptance probe in
``tools/probe_provincia.py``) owns text extraction; this module owns meaning.

The parser is a **gate**, not a reader. :func:`parse_extracto` reconciles the
document against itself and raises :class:`ReconciliationError` whenever any check
fails; it never returns a partially trusted result. There is no ``strict=False``:
the caller cannot opt out of a check it does not like.

What the document looks like
----------------------------

The quarterly account statement is a table whose column header (``Fecha Concepto
Importe Fecha Valor Saldo``) repeats at the top of every page. The first data row
is ``SALDO ANTERIOR <amount>`` — the opening balance, which starts the
running-balance chain; it is not a movement. Every movement row ends with three
trailing fields: ``<importe> <fecha-valor dd-mm> <saldo>``, both amounts
dot-decimal (``12345.67``) and the saldo present on every row.

A movement is a block of one or two lines:

* the *compact* form is a single line, ``dd/mm/yyyy <head> <importe> <dd-mm>
  <saldo>``;
* the *wrapped* form puts the date and description on one line and the three
  trailing fields alone on the continuation line. The continuation is identified
  **structurally** — it is a line that consists only of ``<importe> <dd-mm>
  <saldo>`` — never by assuming one line per row.

The head after the date comes in two shapes:

* a *description* row starts with one of the frozen verbs in :data:`MOVEMENT_VERBS`
  (``compra``/``pago``/``sueldo``/...), as in ``pago DE <name> (<id>)`` or
  ``compra TARJETA dd/dd dd:dd``;
* a *reference* row carries no description at all: ``Nº.XXXXX dd/dd-X.XXXXXX
  X:XXXXXXXXXXX`` — reference numbers only. These are the volume of the document,
  and the ``X:XXXXXXXXXXX`` field is the **recipient's CUIT/CUIL** (user-confirmed
  2026-09-25): the rows are immediate transfers (``pagos por transferencia
  inmediata``), and that field identifies who is being paid.

Everything else is furniture: the repeated column header, the ``SALDO ANTERIOR``
row, the last page's legal boilerplate, and the footer's closing summary — a line of
exactly two ``$``-prefixed figures, the **closing saldo** and the **total debits**.
Furniture is skipped, never parsed as a movement — except that the closing summary is
parsed (structurally, one summary line per document) and fed to the reconciliation
gate. The document's own column labels for those two figures are uncertain from the
geometry, so the match is the two-``$``-amount shape, not a label; the fixtures pin
the semantics (first figure == last row's saldo, second == sum of negative importes).
A line that *starts* with a movement date but is neither a complete row nor followed
by a continuation line is not furniture — it is a movement the parser failed to understand, so it
refuses the document instead of silently dropping it.

The reference-form decision
---------------------------

Reference rows carry no description, so :class:`MovementKind.REFERENCE` is a
**structural** classification: the head matches the reference shape (tokens of
digits, dots, colons, slashes and dashes, with an optional leading ``Nº.``) rather
than any verb. Their posting is a **user-confirmed classification** (2026-09-25): the
rows are immediate transfers (``pagos por transferencia inmediata``) and post to
``Assets:TransferenciaEnTransito`` in the importer (see ``provincia_importer``).
Earlier this was argued from structure alone — the frozen vocabulary has no transfer
verb, yet the real document is known to carry many transfers per quarter, and the
reference row is the only shape left to carry them; the user confirmed the reading.
The clearing account stays loud by design: if a reference row were anything other
than a transfer, the clearing account would not return to zero and the discrepancy
surfaces in review, in the report the human approves. What is refused instead is any
*silent* fallback to an expense category. The recipient CUIT is personal data: it is key material that
reaches the private ledger's staged and report bytes by design, never a module or
probe log line — see Masking.

The verb vocabulary is small, closed and frozen in :data:`MOVEMENT_VERBS`, matched
on the first head token after folding (casefold plus accent stripping), so
``crédito`` and ``credito`` are one verb and ``COMISIÓN`` matches ``comision``.
A first token outside the vocabulary, on a head that is not reference-shaped,
yields :attr:`MovementKind.UNKNOWN`, which the ``vocabulary-known`` check turns
into a refusal. **A new verb in a future statement therefore refuses the import
rather than silently mis-classifying a movement**; the fix is to add the verb to
:data:`MOVEMENT_VERBS` deliberately. Two described rows are split into their own
kinds, matched on the **whole leading token pair** after folding — never a
substring, so ``pago VISALUD`` or ``pago Zxqv Visaplus`` stay ordinary bill
payments on the counterparty map: a ``pago visa`` settlement (the credit-card
payment) and a ``compra tarjeta`` (user-confirmed 2026-09-25: a **debit-card
purchase**, which posts through the counterparty map exactly like any ``compra``;
only ``pago visa`` reaches the card liability).

Masking
-------

The real statements are household data and the models that run this workflow are
remote. **No diagnostic may echo a raw statement line.** Every :class:`CheckResult`
detail and every error message in this module reports line numbers, movement
indexes, counts and field lengths only — never a date, a description, a reference,
a CUIT, an amount or a balance. The check details are printed by the acceptance probe
and read by a human, so this is a property of the code, not of how it is used.

The natural key
---------------

:func:`movement_keys` builds each movement's identity, used as the entry's ``key``
metadata that the pipeline deduplicates on: ``<date>:<identity>:<amount>``, where

* a reference row's identity is the **recipient CUIT** when the head carries the
  ``X:XXXXXXXXXXX`` field (user-confirmed 2026-09-25: that field is the recipient's
  CUIT/CUIL, the stable part of an immediate transfer — two transfers to one person
  must not collapse into one), and the whole **reference string** otherwise. The CUIT
  is personal data: it is key material that reaches the private ledger's staged and
  report bytes by design, never a module or probe log line;
* a description row's identity is its **description** (the head after the date,
  verbatim).

Because a description row has no operation id, two same-day rows with the same
description and amount are indistinguishable in principle; the second and later
occurrences get a deterministic ``#N`` suffix on their identity (first repeat
``#2``, in document order). The suffix is stable for a given statement file, so
re-importing the same file reproduces the same keys and the pipeline's dedup
holds. Keys may contain ``:`` and spaces inside the identity part — the key is
treated as an opaque string everywhere downstream.
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
    "MOVEMENT_VERBS",
    "CheckResult",
    "Extracto",
    "ExtractoParseError",
    "Movement",
    "MovementKind",
    "ReconciliationError",
    "fold",
    "movement_keys",
    "parse_extracto",
]


class MovementKind(Enum):
    """The closed movement vocabulary of the account statement.

    The value is the verb as the statement's vocabulary spells it, folded, so a
    probe can print counts per kind without translating. The two card kinds are
    described rows matched on the whole leading token pair: a ``compra tarjeta``
    is a debit-card purchase that posts through the counterparty map like any
    ``compra`` (user-confirmed 2026-09-25), and a ``pago visa`` is the one kind
    that posts to the card liability. :attr:`REFERENCE` is the structural kind of
    a description-less reference row (an immediate transfer, user-confirmed).
    """

    PURCHASE = "compra"
    CARD_PURCHASE = "compra tarjeta"
    BILL_PAYMENT = "pago"
    CARD_SETTLEMENT = "pago visa"
    SALARY = "sueldo"
    EARNINGS = "haberes"
    DEPOSIT = "deposito"
    CREDIT = "credito"
    INTEREST = "intereses"
    FEES = "comision"
    CHARGES = "cargos"
    DEBIT = "debito"
    REFUND = "devolucion"
    TOPUP = "recarga"
    REFERENCE = "reference"
    UNKNOWN = "unknown"


#: The frozen verb vocabulary, folded, as ``(verb, kind)`` pairs. A verb outside
#: this tuple yields :attr:`MovementKind.UNKNOWN` and the document is refused;
#: adding a verb here is a deliberate act.
MOVEMENT_VERBS: tuple[tuple[str, MovementKind], ...] = (
    ("compra", MovementKind.PURCHASE),
    ("pago", MovementKind.BILL_PAYMENT),
    ("sueldo", MovementKind.SALARY),
    ("intereses", MovementKind.INTEREST),
    ("credito", MovementKind.CREDIT),
    ("deposito", MovementKind.DEPOSIT),
    ("haberes", MovementKind.EARNINGS),
    ("comision", MovementKind.FEES),
    ("comisiones", MovementKind.FEES),
    ("cargos", MovementKind.CHARGES),
    ("devolucion", MovementKind.REFUND),
    ("debito", MovementKind.DEBIT),
    ("recarga", MovementKind.TOPUP),
)
_VERB_KINDS = dict(MOVEMENT_VERBS)

#: The checks, in the fixed order they are reported. Stable strings: a caller may
#: branch on them and a reader may grep for them. The running-balance chain covers
#: every row; the two footer checks reconcile the document against the last page's
#: closing summary (two ``$``-prefixed figures: the closing saldo and the total
#: debits), so a dropped last row, a truncated document or a 0-movement statement is
#: refused; the remaining checks pin the key invariant and the frozen vocabulary.
CHECK_NAMES: tuple[str, ...] = (
    "running-balance-chain",
    "closing-balance",
    "declared-debits",
    "keys-unique",
    "vocabulary-known",
)

_DATE = r"\d{2}/\d{2}/\d{4}"
_VALUE_DATE = r"\d{2}-\d{2}"
_AMOUNT = r"-?\d[\d.]*"
_DATE_PREFIX_RE = re.compile(rf"^({_DATE})(?:\s|$)")
_DATE_BODY_RE = re.compile(rf"^{_DATE}\s+(?P<body>.+)$")
#: The trailing ``<importe> <fecha-valor> <saldo>`` fields of a movement row.
_TAIL_RE = re.compile(
    rf"(?P<value>{_AMOUNT})\s+(?P<value_date>{_VALUE_DATE})\s+(?P<balance>{_AMOUNT})\s*$"
)
#: A line that is *only* the trailing fields: the continuation of a wrapped row.
_CONTINUATION_RE = re.compile(
    rf"^(?P<value>{_AMOUNT})\s+(?P<value_date>{_VALUE_DATE})\s+(?P<balance>{_AMOUNT})$"
)
_OPENING_RE = re.compile(rf"^saldo anterior\s+(?P<value>{_AMOUNT})$", re.IGNORECASE)
_REFERENCE_TOKEN_RE = re.compile(r"(?:n[ºo]?\.?[\d]*)|(?:[\d.:/-]+)")
#: The recipient CUIT/CUIL inside a reference head: ``X:XXXXXXXXXXX`` (user-confirmed
#: shape) — a ``:``-prefixed run of exactly eleven digits. Personal data: key only.
_CUIT_RE = re.compile(r"(?<![\d:])\d{0,3}:(\d{11})(?!\d)")
#: The last page's closing summary: a line of exactly two ``$``-prefixed amounts.
#: The document's own labels for the two columns are uncertain from the geometry, so
#: the match is structural; the fixtures pin the semantics (closing saldo, total debits).
_FOOTER_SUMMARY_RE = re.compile(rf"^\$\s*(?P<closing>{_AMOUNT})\s+\$\s*(?P<debits>{_AMOUNT})\s*$")


def fold(text: str) -> str:
    """Casefold and strip accents: ``CRÉDITO`` and ``credito`` become ``credito``."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _is_reference_body(body: str) -> bool:
    """Whether the head after the date matches the reference-row shape.

    Every token must be a reference token — an optional ``Nº.``-like marker or a
    run of digits with ``.``/``:``/``/``/``-`` separators — and at least one token
    must carry a digit. Any word that is neither marks the head as a description
    (or an unknown verb).
    """
    tokens = fold(body).split()
    if not tokens:
        return False
    if not all(_REFERENCE_TOKEN_RE.fullmatch(token) for token in tokens):
        return False
    return any(any(character.isdigit() for character in token) for token in tokens)


def _classify(body: str) -> MovementKind:
    tokens = fold(body).split()
    if not tokens:
        return MovementKind.UNKNOWN
    kind = _VERB_KINDS.get(tokens[0])
    if kind is None:
        return MovementKind.UNKNOWN
    if len(tokens) >= 2:
        # Whole leading token pair, never a substring: ``pago VISALUD`` and
        # ``pago Zxqv Visaplus`` must stay ordinary bill payments.
        pair = f"{tokens[0]} {tokens[1]}"
        if kind is MovementKind.PURCHASE and pair == "compra tarjeta":
            return MovementKind.CARD_PURCHASE
        if kind is MovementKind.BILL_PAYMENT and pair == "pago visa":
            return MovementKind.CARD_SETTLEMENT
    return kind


@dataclass(frozen=True)
class Movement:
    """One reconciled movement, with the position it came from for diagnostics.

    ``description`` is empty on a reference row and ``reference`` is empty on a
    described row; exactly one of them carries the head after the date. The
    ``value_date`` is the statement's raw ``dd-mm`` field (it carries no year).
    """

    date: dt.date
    description: str
    reference: str
    amount: Decimal
    value_date: str
    balance: Decimal
    page: int
    line: int
    kind: MovementKind


@dataclass(frozen=True)
class CheckResult:
    """One reconciliation check and its **masked** detail."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Extracto:
    """A fully reconciled statement. Only :func:`parse_extracto` builds one.

    ``closing`` and ``declared_debits`` are the last page's closing-summary figures
    (a line of two ``$``-prefixed amounts): the closing saldo and the total debits
    the closing-balance and declared-debits checks reconciled against.
    """

    opening: Decimal
    closing: Decimal
    declared_debits: Decimal
    movements: tuple[Movement, ...]
    checks: tuple[CheckResult, ...]


class ExtractoParseError(ValueError):
    """The text is not a parsable ``Extracto de cuenta``.

    Structural failure, distinct from :class:`ReconciliationError`: the opening
    balance row or the closing summary is missing or repeated, a movement has no
    trailing-fields tail or no continuation line, or an amount is not a dot-decimal
    number. The message never echoes statement text.
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
            f"Banco Provincia reconciliation failed: {names}. "
            f"Refusing the document; a partially trusted parse is never returned."
        )


def movement_keys(movements: Sequence[Movement]) -> tuple[str, ...]:
    """Return the natural ``key`` of every movement, in document order.

    ``<date>:<identity>:<amount>`` — the identity is the recipient CUIT of a
    reference row (or its whole reference string when no CUIT-shaped token is
    present) or the description of a described row, and the second and later
    occurrences of the same ``(date, identity, amount)`` get a ``#N`` suffix so the
    key stays unique within the batch while remaining deterministic. See the module
    docstring.
    """
    keys: list[str] = []
    counts: dict[tuple[dt.date, str, Decimal], int] = {}
    for movement in movements:
        if movement.description:
            identity = movement.description
        else:
            # The recipient CUIT (personal data, private-ledger key only) when the
            # head carries the ``X:XXXXXXXXXXX`` field; the whole reference string
            # otherwise. See the module docstring, ``The natural key``.
            cuit_match = _CUIT_RE.search(movement.reference)
            identity = cuit_match.group(1) if cuit_match is not None else movement.reference
        seen = (movement.date, identity, movement.amount)
        occurrence = counts.get(seen, 0) + 1
        counts[seen] = occurrence
        if occurrence > 1:
            identity = f"{identity}#{occurrence}"
        keys.append(f"{movement.date.isoformat()}:{identity}:{movement.amount:.2f}")
    return tuple(keys)


def _amount(raw: str, line: int, field: str) -> Decimal:
    try:
        return parse_amount(raw, AmountFormat.PLAIN)
    except AmountParseError:
        # ``from None`` on purpose: the AmountParseError message echoes the raw
        # number, and a diagnostic must never carry statement text.
        raise ExtractoParseError(
            f"line {line}: the {field} is not a well-formed dot-decimal amount (length {len(raw)})"
        ) from None


def _layout(text: str) -> list[tuple[int, int, str]]:
    """Return ``(page, global_line, raw_line)`` for every line of the text."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    rows: list[tuple[int, int, str]] = []
    global_line = 0
    for page_index, page_text in enumerate(normalized.split("\f")):
        for raw in page_text.split("\n"):
            global_line += 1
            rows.append((page_index + 1, global_line, raw))
    return rows


def _movement_from_block(block: list[str], page: int, line: int) -> Movement:
    head_parts: list[str] = []
    value_raw = ""
    value_date = ""
    balance_raw = ""
    for raw in block:
        match = _TAIL_RE.search(raw)
        if match is not None:
            head_parts.append(raw[: match.start()])
            value_raw = match.group("value")
            value_date = match.group("value_date")
            balance_raw = match.group("balance")
            break
        head_parts.append(raw)
    else:  # pragma: no cover - the caller guarantees a tail line
        raise ExtractoParseError(f"line {line}: movement has no trailing-fields tail")

    head = " ".join(part.strip() for part in head_parts).strip()
    date_match = _DATE_BODY_RE.match(head)
    if date_match is None:  # pragma: no cover - the caller guarantees a date
        raise ExtractoParseError(f"line {line}: movement has no leading date")
    try:
        day, month, year = (int(part) for part in date_match.group(0).split()[0].split("/"))
        movement_date = dt.date(year, month, day)
    except ValueError:
        raise ExtractoParseError(f"line {line}: the leading date is not a real date") from None

    body = date_match.group("body").strip()
    if not body:
        raise ExtractoParseError(f"line {line}: movement has neither a description nor a reference")

    kind = _classify(body)
    if kind is MovementKind.UNKNOWN and _is_reference_body(body):
        kind = MovementKind.REFERENCE
    if kind is MovementKind.REFERENCE:
        description = ""
        reference = " ".join(body.split())
    else:
        description = " ".join(body.split())
        reference = ""

    return Movement(
        date=movement_date,
        description=description,
        reference=reference,
        amount=_amount(value_raw, line, "value"),
        value_date=value_date,
        balance=_amount(balance_raw, line, "balance"),
        page=page,
        line=line,
        kind=kind,
    )


def _parse_movements(rows: list[tuple[int, int, str]]) -> list[Movement]:
    movements: list[Movement] = []
    index = 0
    while index < len(rows):
        page, line, raw = rows[index]
        if _DATE_PREFIX_RE.match(raw.strip()):
            if _TAIL_RE.search(raw) is not None:
                movements.append(_movement_from_block([raw], page, line))
                index += 1
                continue
            # A movement head without its tail: the continuation line must follow,
            # on the same page, and carry only the trailing fields.
            if index + 1 >= len(rows):
                raise ExtractoParseError(
                    f"line {line}: a wrapped movement has no continuation line"
                )
            next_page, next_line, next_raw = rows[index + 1]
            if _CONTINUATION_RE.match(next_raw.strip()) is None:
                raise ExtractoParseError(
                    f"line {line}: a wrapped movement has no continuation line "
                    f"before line {next_line}"
                )
            if next_page != page:
                raise ExtractoParseError(
                    f"line {line}: a wrapped movement crosses a page boundary "
                    f"before its continuation line"
                )
            movements.append(_movement_from_block([raw, next_raw], page, line))
            index += 2
            continue
        index += 1
    return movements


def _opening(rows: list[tuple[int, int, str]]) -> Decimal:
    """Return the ``SALDO ANTERIOR`` opening balance; refuse if missing or repeated."""
    found: tuple[int, Decimal] | None = None
    for _page, line, raw in rows:
        match = _OPENING_RE.match(raw.strip())
        if match is None:
            continue
        if found is not None:
            raise ExtractoParseError(
                f"the opening balance row appears more than once (first at line {found[0]})"
            )
        found = (line, _amount(match.group("value"), line, "opening balance"))
    if found is None:
        raise ExtractoParseError("missing opening balance row (SALDO ANTERIOR)")
    return found[1]


def _closing_summary(rows: list[tuple[int, int, str]]) -> tuple[Decimal, Decimal]:
    """Return the footer's ``(closing saldo, total debits)``; refuse if missing or repeated.

    The summary is matched structurally: a line of exactly two ``$``-prefixed
    amounts. The document's own column labels are uncertain from the geometry, so
    no label is required; the fixtures pin that the first figure is the closing
    saldo and the second the total debits. The message is masked: line numbers and
    counts only, never the amounts themselves.
    """
    found: tuple[int, Decimal, Decimal] | None = None
    for _page, line, raw in rows:
        match = _FOOTER_SUMMARY_RE.match(raw.strip())
        if match is None:
            continue
        if found is not None:
            raise ExtractoParseError(
                f"the closing summary appears more than once (first at line {found[0]})"
            )
        found = (
            line,
            _amount(match.group("closing"), line, "closing balance"),
            _amount(match.group("debits"), line, "total debits"),
        )
    if found is None:
        raise ExtractoParseError(
            "missing closing summary: the last page must carry a line of two "
            "'$'-prefixed figures (the closing saldo and the total debits)"
        )
    return found[1], found[2]


def _reconcile(
    opening: Decimal,
    closing: Decimal,
    declared_debits: Decimal,
    movements: list[Movement],
) -> tuple[CheckResult, ...]:
    """Run all checks and return them in :data:`CHECK_NAMES` order.

    Every check is computed even after an earlier one fails, so one run tells the
    whole story. Details are masked: counts, movement indexes and line numbers only.
    """
    checks: list[CheckResult] = []

    chain_ok = True
    previous = opening
    for index, movement in enumerate(movements):
        if movement.balance != previous + movement.amount:
            chain_ok = False
            chain_detail = (
                f"balance chain breaks at movement {index + 1} (line {movement.line}): "
                f"the saldo does not equal the previous saldo plus the importe"
            )
            break
        previous = movement.balance
    else:
        chain_detail = (
            f"balance chain holds from the opening balance across {len(movements)} movement(s)"
        )
    checks.append(CheckResult("running-balance-chain", chain_ok, chain_detail))

    if not movements:
        closing_ok = False
        closing_detail = (
            "the document declares a closing balance but contains no movements; "
            "an empty statement is refused"
        )
    elif movements[-1].balance != closing:
        closing_ok = False
        closing_detail = (
            f"the last movement's saldo (movement {len(movements)}) does not equal the "
            f"declared closing balance"
        )
    else:
        closing_ok = True
        closing_detail = (
            f"the last movement's saldo equals the declared closing balance "
            f"across {len(movements)} movement(s)"
        )
    checks.append(CheckResult("closing-balance", closing_ok, closing_detail))

    negative_total = sum(
        (movement.amount for movement in movements if movement.amount < 0), Decimal(0)
    )
    if -negative_total != declared_debits:
        debits_ok = False
        debits_detail = (
            f"the sum of the {sum(1 for movement in movements if movement.amount < 0)} negative "
            f"importe(s) does not equal the declared total debits"
        )
    else:
        debits_ok = True
        debits_detail = (
            f"the sum of the negative importes equals the declared total debits "
            f"across {len(movements)} movement(s)"
        )
    checks.append(CheckResult("declared-debits", debits_ok, debits_detail))

    keys = movement_keys(movements)
    repeated = len(keys) - len(set(keys))
    if repeated:
        # Unreachable by construction (the #N suffix makes keys unique); this is the
        # guard that turns a key-construction bug into a loud refusal.
        keys_detail = f"{repeated} natural key(s) repeat within the batch"
    else:
        keys_detail = f"{len(movements)} natural key(s) are unique within the batch"
    checks.append(CheckResult("keys-unique", not repeated, keys_detail))

    unknown = [movement for movement in movements if movement.kind is MovementKind.UNKNOWN]
    if not unknown:
        vocabulary_detail = (
            f"all {len(movements)} movement head(s) are in the frozen vocabulary "
            f"or match the reference shape"
        )
    else:
        first = unknown[0]
        vocabulary_detail = (
            f"movement {movements.index(first) + 1} (line {first.line}) uses a head outside "
            f"the frozen vocabulary and the reference shape; {len(unknown)} total"
        )
    checks.append(CheckResult("vocabulary-known", not unknown, vocabulary_detail))

    return tuple(checks)


def parse_extracto(text: str) -> Extracto:
    """Parse and reconcile one extracted statement.

    Raises:
        ExtractoParseError: the text is not an ``Extracto de cuenta``: the opening
            balance row or the closing summary is missing or repeated, a movement has
            no tail or no continuation line, or an amount is malformed. The message
            is masked.
        ReconciliationError: the document parsed but a check failed. The error
            carries every check and the failed subset; nothing partial is returned.
    """
    rows = _layout(text)
    opening = _opening(rows)
    closing, declared_debits = _closing_summary(rows)
    movements = _parse_movements(rows)
    checks = _reconcile(opening, closing, declared_debits, movements)
    if any(not check.ok for check in checks):
        raise ReconciliationError(checks)
    return Extracto(
        opening=opening,
        closing=closing,
        declared_debits=declared_debits,
        movements=tuple(movements),
        checks=checks,
    )
