"""Locale amount parsing for statement importers.

Real Argentine statements mix number formats, sometimes inside one bank:
Mercado Pago, Brubank, BBVA and Provincia's card statement write
``1.234.567,89`` (dot thousands, comma decimals), while Provincia's account
extracto writes ``12345.67`` (beancount's own format). Beancount accepts only
``1,234,567.89``.

The caller must declare the format, because the format is a property of the
source document and only the caller knows it. Guessing is what silently corrupts
a ledger: ``1.234`` is 1234 under the Argentine convention, and a shared
heuristic would read it as 1.234 under the plain one. A wrong amount must be
loud, never plausible, so anything ambiguous or malformed raises instead of being
guessed.

The sign may lead or trail (``12.345,67-`` is a payment in Provincia's card
statement, and a leading-minus-only parser reads it as a purchase). A leading
``$``, with an optional space, is accepted because the statements contain it.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import Enum

__all__ = [
    "AmountFormat",
    "AmountParseError",
    "detect_amount_format",
    "format_amount",
    "parse_amount",
]


class AmountFormat(Enum):
    """The declared number format of a source document."""

    ARS = "ars"
    """Argentine: dot thousands, comma decimals (``1.234.567,89``)."""

    PLAIN = "plain"
    """Beancount: comma thousands, dot decimals (``1,234,567.89``); no grouping allowed too."""


class AmountParseError(ValueError):
    """Raised when an amount is malformed or ambiguous for its declared format."""


_ARS_RE = re.compile(r"^(?P<int>\d{1,3}(?:\.\d{3})*|\d+)(?:,(?P<dec>\d{2}))?$")
_PLAIN_RE = re.compile(r"^(?P<int>\d{1,3}(?:,\d{3})*|\d+)(?:\.(?P<dec>\d{2}))?$")

_CENT = Decimal("0.01")


def _strip_sign_and_currency(text: str) -> tuple[str, bool]:
    """Return the numeric body and whether the amount is negative.

    Accepts a leading or trailing minus and a leading ``$`` with optional space.
    A minus on both ends is ambiguous and refused.
    """
    body = text.strip()
    if not body:
        raise AmountParseError("empty amount")

    negative = False
    if body.startswith("-"):
        negative = True
        body = body[1:].strip()
    if body.startswith("$"):
        body = body[1:].lstrip()
    if body.endswith("-"):
        if negative:
            raise AmountParseError(f"ambiguous amount {text!r}: a minus on both ends")
        negative = True
        body = body[:-1].strip()

    if not body:
        raise AmountParseError(f"empty amount in {text!r}")
    return body, negative


def _body_to_decimal(body: str, fmt: AmountFormat, original: str) -> Decimal:
    pattern = _ARS_RE if fmt is AmountFormat.ARS else _PLAIN_RE
    match = pattern.fullmatch(body)
    if match is None:
        raise AmountParseError(
            f"malformed amount {original!r} for format {fmt.name}: "
            f"expected {_describe(fmt)}"
        )
    integer = match.group("int").replace(".", "").replace(",", "")
    decimals = match.group("dec")
    digits = integer if decimals is None else f"{integer}.{decimals}"
    return Decimal(digits)


def _describe(fmt: AmountFormat) -> str:
    if fmt is AmountFormat.ARS:
        return "dot thousands and exactly two comma-decimal digits, e.g. 1.234.567,89"
    return "comma thousands and exactly two dot-decimal digits, e.g. 1,234,567.89"


def parse_amount(text: str, fmt: AmountFormat) -> Decimal:
    """Parse ``text`` using the caller-declared ``fmt``.

    Raises:
        AmountParseError: on empty input, mismatched separators, a separator
            followed by the wrong number of digits, or more than one decimal
            separator.
    """
    body, negative = _strip_sign_and_currency(text)
    value = _body_to_decimal(body, fmt, text)
    return -value if negative else value


def detect_amount_format(text: str) -> AmountFormat | None:
    """Return the format ``text`` unambiguously proves, or ``None``.

    For tests and for asserting that a file matches the format its importer
    declared. Returns ``None`` rather than guessing when the evidence is
    insufficient: a bare integer such as ``12345`` is valid in both formats, so
    it proves neither.
    """
    try:
        body, _ = _strip_sign_and_currency(text)
    except AmountParseError:
        return None

    is_ars = _ARS_RE.fullmatch(body) is not None
    is_plain = _PLAIN_RE.fullmatch(body) is not None
    if is_ars and is_plain:
        return None
    if is_ars:
        return AmountFormat.ARS
    if is_plain:
        return AmountFormat.PLAIN
    return None


def _group(digits: str, separator: str) -> str:
    groups: list[str] = []
    remaining = digits
    while len(remaining) > 3:
        groups.insert(0, remaining[-3:])
        remaining = remaining[:-3]
    groups.insert(0, remaining)
    return separator.join(groups)


def _coerce_decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool is not an amount")
    return Decimal(str(value))


def format_amount(value: Decimal | int | str, fmt: AmountFormat) -> str:
    """Format ``value`` in ``fmt`` for round-tripping and beancount output.

    ``AmountFormat.PLAIN`` produces beancount's own form (``1,234,567.89``); the
    sign always leads, because that is what beancount parses.
    """
    amount = _coerce_decimal(value).quantize(_CENT)
    negative = amount < 0
    magnitude = -amount if negative else amount
    integer, _, decimals = f"{magnitude:.2f}".partition(".")

    if fmt is AmountFormat.ARS:
        body = f"{_group(integer, '.')},{decimals}"
    elif fmt is AmountFormat.PLAIN:
        body = f"{_group(integer, ',')}.{decimals}"
    else:  # pragma: no cover - defensive, the enum is closed
        raise ValueError(f"unknown amount format {fmt!r}")

    return f"-{body}" if negative else body
