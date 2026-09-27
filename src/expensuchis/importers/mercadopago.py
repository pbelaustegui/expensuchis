"""Parser for Mercado Pago's ``RESUMEN DE CUENTA EN PESOS`` statement.

Pure over extracted text: no PDF library, no filesystem, no clock and no
environment. The caller (the importer's ``extract``, or the acceptance probe in
``tools/probe_mercadopago.py``) owns text extraction; this module owns meaning.

The parser is a **gate**, not a reader. :func:`parse_resumen` reconciles the
document against itself and raises :class:`ReconciliationError` whenever any check
fails; it never returns a partially trusted result. There is no ``strict=False``:
the caller cannot opt out of a check it does not like.

What the document looks like
----------------------------

A movement is a block of one to three lines, never one line on its own:

* the compact form is a single line, ``dd-mm-yyyy <verb> <description...> <id> $
  <value> $ <balance>``;
* the wrapped form puts the date alone on a line, runs the description over one or
  two lines, and ends on a payload line whose head is the operation id:
  ``<id> $ <value> $ <balance>``.

The payload line is identifiable without knowing the layout: it is the line that
ends with **two** amounts. Everything else on the page is furniture, and the table
header repeats on every page. Pages are separated by a form feed (``\\f``); the
probe joins ``pypdfium2``'s per-page text with it. A movement never spans a page.

The verb vocabulary is small, closed and frozen in :data:`MOVEMENT_VERBS`. A verb
outside it yields :attr:`MovementKind.UNKNOWN`, which the ``vocabulary-known`` check
turns into a refusal. **A new verb in a future statement therefore refuses the
import rather than silently mis-classifying a movement**; the fix is to add the verb
to :data:`MOVEMENT_VERBS` deliberately. That fail-closed behaviour is the point: an
unknown verb might be a new kind of hold or a new fee, and guessing its category is
exactly the silent corruption this ledger exists to avoid.

Masking
-------

The real statements are household data and the models that run this workflow are
remote. **No diagnostic may echo a raw statement line.** Every :class:`CheckResult`
detail and every error message in this module reports line numbers, movement
indexes, counts and field lengths only — never a date, a description, an operation
id, an amount or a balance. The check details are printed by the acceptance probe
and read by a human, so this is a property of the code, not of how it is used.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ..numbers import AmountFormat, AmountParseError, parse_amount

__all__ = [
    "CHECK_NAMES",
    "MOVEMENT_VERBS",
    "CheckResult",
    "Movement",
    "MovementKind",
    "ReconciliationError",
    "Resumen",
    "ResumenParseError",
    "parse_resumen",
]


class MovementKind(Enum):
    """The closed movement vocabulary of the account statement.

    The value is the verb as the statement spells it, so a probe can print counts
    per kind without translating. ``Pago con`` (a purchase that debits the account
    directly) and ``Pago <servicio>`` (a bill payment) are separate kinds even
    though both start with ``Pago``.
    """

    TRANSFER_SENT = "Transferencia enviada"
    TRANSFER_RECEIVED = "Transferencia recibida"
    RETURNS = "Rendimientos"
    PURCHASE = "Pago con"
    WITHDRAWAL = "Dinero retirado"
    HOLD = "Dinero reservado"
    BILL_PAYMENT = "Pago"
    UNKNOWN = "unknown"


#: The frozen verb vocabulary. Longest verbs first, so ``Pago con`` is matched
#: before ``Pago``; :func:`_classify` relies on this order. Adding a verb here is a
#: deliberate act; a statement that uses one outside this tuple is refused.
MOVEMENT_VERBS: tuple[tuple[str, MovementKind], ...] = (
    ("Transferencia enviada", MovementKind.TRANSFER_SENT),
    ("Transferencia recibida", MovementKind.TRANSFER_RECEIVED),
    ("Dinero reservado", MovementKind.HOLD),
    ("Dinero retirado", MovementKind.WITHDRAWAL),
    ("Pago con", MovementKind.PURCHASE),
    ("Rendimientos", MovementKind.RETURNS),
    ("Pago", MovementKind.BILL_PAYMENT),
)

#: The checks, in the fixed order they are reported. Stable strings: a caller may
#: branch on them and a reader may grep for them.
CHECK_NAMES: tuple[str, ...] = (
    "header-arithmetic",
    "sum-positive-matches-entries",
    "sum-negative-matches-withdrawals",
    "running-balance-chain",
    "closing-balance",
    "keys-unique",
    "vocabulary-known",
)

_DATE = r"\d{2}-\d{2}-\d{4}"
_AMOUNT = r"-?\d[\d.]*,\d{2}"
_DATE_ONLY_RE = re.compile(rf"^{_DATE}$")
_DATE_PREFIX_RE = re.compile(rf"^({_DATE})(?:\s|$)")
_PAYLOAD_RE = re.compile(rf"\$\s*(?P<value>{_AMOUNT})\s+\$\s*(?P<balance>{_AMOUNT})\s*$")
_DOLLAR_RE = re.compile(r"\$")
_HEADER_FIGURE_RES = {
    "opening": re.compile(rf"Saldo inicial:\s*\$\s*(?P<value>{_AMOUNT})"),
    "entries": re.compile(rf"Entradas:\s*\$\s*(?P<value>{_AMOUNT})"),
    "withdrawals": re.compile(rf"Salidas:\s*\$\s*(?P<value>{_AMOUNT})"),
    "closing": re.compile(rf"Saldo final:\s*\$\s*(?P<value>{_AMOUNT})"),
}
_PERIOD_RE = re.compile(r"Periodo:\s*(?P<value>.*)$")


@dataclass(frozen=True)
class Movement:
    """One reconciled movement, with the position it came from for diagnostics."""

    date: dt.date
    operation_id: str
    description: str
    amount: Decimal
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
class Resumen:
    """A fully reconciled statement. Only :func:`parse_resumen` builds one."""

    opening: Decimal
    declared_entries: Decimal
    declared_withdrawals: Decimal
    closing: Decimal
    period_text: str
    movements: tuple[Movement, ...]
    checks: tuple[CheckResult, ...]


class ResumenParseError(ValueError):
    """The text is not a parsable ``RESUMEN DE CUENTA EN PESOS``.

    Structural failure, distinct from :class:`ReconciliationError`: the header
    figures are missing, a movement has no payload line, or an amount is not an
    Argentine number. The message never echoes statement text.
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
            f"Mercado Pago reconciliation failed: {names}. "
            f"Refusing the document; a partially trusted parse is never returned."
        )


def _classify(description: str) -> MovementKind:
    for verb, kind in MOVEMENT_VERBS:
        if description == verb or description.startswith(verb + " "):
            return kind
    return MovementKind.UNKNOWN


def _amount(raw: str, line: int, field: str) -> Decimal:
    try:
        return parse_amount(raw, AmountFormat.ARS)
    except AmountParseError:
        # ``from None`` on purpose: the AmountParseError message echoes the raw
        # number, and a diagnostic must never carry statement text.
        raise ResumenParseError(
            f"line {line}: the {field} is not a well-formed Argentine amount (length {len(raw)})"
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
    balance_raw = ""
    for raw in block:
        match = _PAYLOAD_RE.search(raw)
        if match is not None:
            head_parts.append(raw[: match.start()])
            value_raw = match.group("value")
            balance_raw = match.group("balance")
            break
        head_parts.append(raw)
    else:  # pragma: no cover - the caller guarantees a payload line
        raise ResumenParseError(f"line {line}: movement has no payload line")

    head = " ".join(part.strip() for part in head_parts).strip()
    date_match = _DATE_PREFIX_RE.match(head)
    if date_match is None:  # pragma: no cover - the caller guarantees a date
        raise ResumenParseError(f"line {line}: movement has no leading date")
    try:
        day, month, year = (int(part) for part in date_match.group(1).split("-"))
        movement_date = dt.date(year, month, day)
    except ValueError:
        raise ResumenParseError(f"line {line}: the leading date is not a real date") from None

    tokens = head[date_match.end() :].split()
    if len(tokens) < 2:
        raise ResumenParseError(
            f"line {line}: movement has {len(tokens)} head token(s); "
            f"a description and an operation id are required"
        )
    operation_id = tokens[-1]
    description = " ".join(tokens[:-1])

    return Movement(
        date=movement_date,
        operation_id=operation_id,
        description=description,
        amount=_amount(value_raw, line, "value"),
        balance=_amount(balance_raw, line, "balance"),
        page=page,
        line=line,
        kind=_classify(description),
    )


def _parse_movements(rows: list[tuple[int, int, str]]) -> list[Movement]:
    movements: list[Movement] = []
    index = 0
    while index < len(rows):
        page, line, raw = rows[index]
        stripped = raw.strip()
        if _PAYLOAD_RE.search(raw) is not None and _DATE_PREFIX_RE.match(stripped):
            movements.append(_movement_from_block([raw], page, line))
            index += 1
            continue
        if _DATE_ONLY_RE.match(stripped):
            block = [raw]
            cursor = index + 1
            while cursor < len(rows) and _PAYLOAD_RE.search(rows[cursor][2]) is None:
                next_page, next_line, next_raw = rows[cursor]
                if next_page != page:
                    raise ResumenParseError(
                        f"line {line}: a wrapped movement crosses a page boundary "
                        f"before its payload line"
                    )
                if _DATE_ONLY_RE.match(next_raw.strip()):
                    raise ResumenParseError(
                        f"line {line}: a wrapped movement has no payload line before "
                        f"the next movement at line {next_line}"
                    )
                block.append(next_raw)
                cursor += 1
            if cursor >= len(rows):
                raise ResumenParseError(f"line {line}: a wrapped movement has no payload line")
            block.append(rows[cursor][2])
            movements.append(_movement_from_block(block, page, line))
            index = cursor + 1
            continue
        if _DATE_PREFIX_RE.match(stripped) and _DOLLAR_RE.search(raw):
            raise ResumenParseError(
                f"line {line}: a movement line carries an amount that is not a "
                f"well-formed Argentine amount"
            )
        index += 1
    return movements


def _parse_header(rows: list[tuple[int, int, str]]) -> dict[str, Decimal]:
    figures: dict[str, Decimal] = {}
    for _page, line, raw in rows:
        for name, pattern in _HEADER_FIGURE_RES.items():
            if name in figures:
                continue
            match = pattern.search(raw)
            if match is not None:
                figures[name] = _amount(match.group("value"), line, name)
    missing = [name for name in _HEADER_FIGURE_RES if name not in figures]
    if missing:
        raise ResumenParseError(f"missing header figure(s): {', '.join(missing)}")
    return figures


def _period(rows: list[tuple[int, int, str]]) -> str:
    for _page, _line, raw in rows:
        match = _PERIOD_RE.search(raw)
        if match is not None:
            return match.group("value").strip()
    raise ResumenParseError("missing header field(s): period")


def _reconcile(
    opening: Decimal,
    declared_entries: Decimal,
    declared_withdrawals: Decimal,
    closing: Decimal,
    movements: list[Movement],
) -> tuple[CheckResult, ...]:
    """Run all seven checks and return them in :data:`CHECK_NAMES` order.

    Every check is computed even after an earlier one fails, so one run tells the
    whole story. Details are masked: counts, movement indexes and line numbers only.
    """
    checks: list[CheckResult] = []

    sum_positive = sum((m.amount for m in movements if m.amount > 0), Decimal(0))
    sum_negative = sum((m.amount for m in movements if m.amount < 0), Decimal(0))
    positive_count = sum(1 for m in movements if m.amount > 0)
    negative_count = sum(1 for m in movements if m.amount < 0)

    arithmetic_ok = opening + declared_entries + declared_withdrawals == closing
    if arithmetic_ok:
        arithmetic_detail = "opening + entradas + salidas equals saldo final"
    else:
        arithmetic_detail = "opening + entradas + salidas does not equal saldo final"
    checks.append(CheckResult("header-arithmetic", arithmetic_ok, arithmetic_detail))

    positive_ok = sum_positive == declared_entries
    if positive_ok:
        positive_detail = f"{positive_count} positive movement(s) sum to Entradas"
    else:
        positive_detail = f"{positive_count} positive movement(s) do not sum to Entradas"
    checks.append(CheckResult("sum-positive-matches-entries", positive_ok, positive_detail))

    negative_ok = sum_negative == declared_withdrawals
    if negative_ok:
        negative_detail = f"{negative_count} negative movement(s) sum to Salidas"
    else:
        negative_detail = f"{negative_count} negative movement(s) do not sum to Salidas"
    checks.append(CheckResult("sum-negative-matches-withdrawals", negative_ok, negative_detail))

    chain_ok = True
    previous = opening
    for index, movement in enumerate(movements):
        if movement.balance != previous + movement.amount:
            chain_ok = False
            chain_detail = f"balance chain breaks at movement {index + 1} (line {movement.line})"
            break
        previous = movement.balance
    else:
        chain_detail = f"balance chain holds across {len(movements)} movement(s)"
    checks.append(CheckResult("running-balance-chain", chain_ok, chain_detail))

    if movements:
        closing_ok = movements[-1].balance == closing
        if closing_ok:
            closing_detail = "last parsed balance equals the declared Saldo final"
        else:
            closing_detail = (
                f"last parsed balance (movement {len(movements)}, "
                f"line {movements[-1].line}) does not equal the declared Saldo final"
            )
    else:
        closing_ok = opening == closing
        if closing_ok:
            closing_detail = "no movements: opening balance equals the declared Saldo final"
        else:
            closing_detail = "no movements: opening does not equal the declared Saldo final"
    checks.append(CheckResult("closing-balance", closing_ok, closing_detail))

    seen: set[tuple[dt.date, str, Decimal]] = set()
    repeated = 0
    duplicate_at: tuple[int, int] | None = None
    for index, movement in enumerate(movements):
        key = (movement.date, movement.operation_id, movement.amount)
        if key in seen:
            repeated += 1
            if duplicate_at is None:
                duplicate_at = (index, movement.line)
        else:
            seen.add(key)
    if duplicate_at is None:
        keys_detail = f"{len(movements)} movement key(s) are unique"
    else:
        index, line = duplicate_at
        keys_detail = (
            f"{repeated} key(s) repeat; first repeat at movement {index + 1} (line {line})"
        )
    checks.append(CheckResult("keys-unique", duplicate_at is None, keys_detail))

    unknown = [
        (index, movement)
        for index, movement in enumerate(movements)
        if movement.kind is MovementKind.UNKNOWN
    ]
    if not unknown:
        vocabulary_detail = f"all {len(movements)} movement verb(s) are in the frozen vocabulary"
    else:
        index, movement = unknown[0]
        vocabulary_detail = (
            f"movement {index + 1} (line {movement.line}) uses a verb outside the frozen "
            f"vocabulary; {len(unknown)} total"
        )
    checks.append(CheckResult("vocabulary-known", not unknown, vocabulary_detail))

    return tuple(checks)


def parse_resumen(text: str) -> Resumen:
    """Parse and reconcile one extracted statement.

    Raises:
        ResumenParseError: the text is not a ``RESUMEN DE CUENTA EN PESOS``: a
            header figure is missing, a movement has no payload line, or an amount
            is malformed. The message is masked.
        ReconciliationError: the document parsed but a check failed. The error
            carries every check and the failed subset; nothing partial is returned.
    """
    rows = _layout(text)
    figures = _parse_header(rows)
    period_text = _period(rows)
    movements = _parse_movements(rows)
    checks = _reconcile(
        figures["opening"],
        figures["entries"],
        figures["withdrawals"],
        figures["closing"],
        movements,
    )
    if any(not check.ok for check in checks):
        raise ReconciliationError(checks)
    return Resumen(
        opening=figures["opening"],
        declared_entries=figures["entries"],
        declared_withdrawals=figures["withdrawals"],
        closing=figures["closing"],
        period_text=period_text,
        movements=tuple(movements),
        checks=checks,
    )
