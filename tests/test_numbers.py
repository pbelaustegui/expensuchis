"""Table-driven tests for the locale amount parser."""

from __future__ import annotations

from decimal import Decimal

import pytest

from expensuchis.numbers import (
    AmountFormat,
    AmountParseError,
    detect_amount_format,
    format_amount,
    parse_amount,
)

ARS = AmountFormat.ARS
PLAIN = AmountFormat.PLAIN


@pytest.mark.parametrize(
    ("text", "fmt", "expected"),
    [
        # Argentine: dot thousands, comma decimals.
        ("1.234.567,89", ARS, Decimal("1234567.89")),
        ("12.345,67", ARS, Decimal("12345.67")),
        ("1234567,89", ARS, Decimal("1234567.89")),  # no grouping
        ("1.234", ARS, Decimal(1234)),  # dot is thousands, so this is 1234
        ("0,00", ARS, Decimal("0.00")),
        ("987.654.321,00", ARS, Decimal("987654321.00")),  # large
        # Plain / beancount: comma thousands, dot decimals.
        ("1,234,567.89", PLAIN, Decimal("1234567.89")),
        ("12,345.67", PLAIN, Decimal("12345.67")),
        ("1234567.89", PLAIN, Decimal("1234567.89")),  # no grouping
        ("12345.67", PLAIN, Decimal("12345.67")),
        ("1,234", PLAIN, Decimal(1234)),
        ("0.00", PLAIN, Decimal("0.00")),
        ("987,654,321.00", PLAIN, Decimal("987654321.00")),  # large
        ("12345", PLAIN, Decimal(12345)),  # integer, no decimals
        # Sign, in both positions, and a leading currency symbol.
        ("-12.345,67", ARS, Decimal("-12345.67")),
        ("12.345,67-", ARS, Decimal("-12345.67")),  # Provincia's trailing minus
        ("$12.345,67", ARS, Decimal("12345.67")),
        ("$ 12.345,67", ARS, Decimal("12345.67")),
        ("$1,234.56", PLAIN, Decimal("1234.56")),
        ("-12345.67", PLAIN, Decimal("-12345.67")),
        ("12345.67-", PLAIN, Decimal("-12345.67")),
    ],
)
def test_parse_amount(text: str, fmt: AmountFormat, expected: Decimal) -> None:
    assert parse_amount(text, fmt) == expected


@pytest.mark.parametrize(
    ("text", "fmt"),
    [
        ("", ARS),
        ("   ", ARS),
        ("", PLAIN),
        ("-", ARS),
        ("$", ARS),
        ("$  ", PLAIN),
        # Mismatched separators for the declared format.
        ("1,234.56", ARS),
        ("1.234,56", PLAIN),
        # A separator followed by an unexpected number of digits.
        ("123.45", ARS),  # dot thousands with two digits
        ("1.234.567,8", ARS),  # one decimal digit
        ("1.234.567,890", ARS),  # three decimal digits
        ("1234.5", PLAIN),
        ("1234.567", PLAIN),
        ("1,23.45", PLAIN),  # comma thousands with two digits
        # More than one decimal separator.
        ("1,23,45", ARS),
        ("1.23.45", PLAIN),
        ("1.234.567,89,0", ARS),
        # Malformed grouping and non-numeric noise.
        ("1..234", ARS),
        ("1.2345", PLAIN),
        ("abc", ARS),
        ("12,34,56.78", PLAIN),
        # A minus on both ends is ambiguous.
        ("-12.345,67-", ARS),
    ],
)
def test_parse_amount_rejects_ambiguity_and_malformation(text: str, fmt: AmountFormat) -> None:
    with pytest.raises(AmountParseError):
        parse_amount(text, fmt)


def test_trailing_minus_is_negative_and_a_leading_only_parser_gets_the_wrong_sign() -> None:
    # The real string from Provincia's card liquidación.
    assert parse_amount("12.345,67-", ARS) == Decimal("-12345.67")

    def leading_minus_only(text: str) -> Decimal:
        # Recognises a leading minus and ignores (drops) a trailing one.
        raw = text.strip()
        negative = raw.startswith("-")
        digits = raw.lstrip("-").rstrip("-").replace(".", "").replace(",", ".")
        return Decimal(digits) * (-1 if negative else 1)

    # A leading-minus-only implementation reads the payment as a purchase.
    assert leading_minus_only("12.345,67-") == Decimal("12345.67")
    assert leading_minus_only("12.345,67-") != parse_amount("12.345,67-", ARS)


@pytest.mark.parametrize(
    ("value", "fmt", "expected"),
    [
        (Decimal("1234567.89"), PLAIN, "1,234,567.89"),
        (Decimal("1234567.89"), ARS, "1.234.567,89"),
        (Decimal("1234.56"), PLAIN, "1,234.56"),
        (Decimal("1234.56"), ARS, "1.234,56"),
        (Decimal("0.00"), PLAIN, "0.00"),
        (Decimal("0.00"), ARS, "0,00"),
        (Decimal("-12345.67"), PLAIN, "-12,345.67"),
        (Decimal("-12345.67"), ARS, "-12.345,67"),
        (Decimal(987654321), PLAIN, "987,654,321.00"),
        (Decimal(987654321), ARS, "987.654.321,00"),
    ],
)
def test_format_amount(value: Decimal, fmt: AmountFormat, expected: str) -> None:
    assert format_amount(value, fmt) == expected


@pytest.mark.parametrize("fmt", [ARS, PLAIN])
@pytest.mark.parametrize(
    "value",
    [Decimal("0.00"), Decimal("0.05"), Decimal("12.34"), Decimal("-99.99"), Decimal("987654321.01")],
)
def test_format_then_parse_round_trips(value: Decimal, fmt: AmountFormat) -> None:
    assert parse_amount(format_amount(value, fmt), fmt) == value


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.234.567,89", ARS),
        ("12.345,67", ARS),
        ("1.234", ARS),
        ("$ 1.234,56", ARS),
        ("12.345,67-", ARS),
        ("1,234,567.89", PLAIN),
        ("12,345.67", PLAIN),
        ("12345.67", PLAIN),
        ("1,234", PLAIN),
        ("$1,234.56", PLAIN),
        # Insufficient or no evidence: must return None, never guess.
        ("12345", None),
        ("0", None),
        ("", None),
        ("abc", None),
        ("1,23,45", None),
    ],
)
def test_detect_amount_format(text: str, expected: AmountFormat | None) -> None:
    assert detect_amount_format(text) is expected
