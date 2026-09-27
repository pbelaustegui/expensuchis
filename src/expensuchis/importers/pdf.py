"""The shared PDF text reader for statement importers.

Reading real PDF bytes is the one operation the whole privacy design is built
around, so it lives in exactly one place. The caller — the Mercado Pago
importer's ``extract``/``identify`` or the local acceptance probe — decides what,
if anything, may be shown of the extracted text; this module only extracts.

``pypdfium2`` is imported lazily inside :func:`read_pdf`, so importing the
``expensuchis.importers`` package never pulls the PDF engine into the process:
``identify`` and the CLI stay fast, and a machine without the engine can still run
everything that does not need it.

This reader never prints and never writes. It returns the pages' text joined with
a form feed (``"\\f"``), the separator the parser's line layout understands, and
the page count.

:func:`read_creation_date` reads the same document's ``CreationDate`` metadata
field, for a source (BBVA) whose statement never prints its own year and whose
parser instead takes the caller's clock proxy as an ``anchor`` argument.

:func:`read_pdf_rows` is a second, independent reader for a document whose flat
text loses information a parser needs: the BBVA card ``Liquidación`` (Visa and
Mastercard) prints two money columns, ``PESOS`` and ``DÓLARES``, side by side,
and ``pypdfium2``'s flat text extraction interleaves them -- the column an
amount belongs to is only recoverable from its horizontal position on the page.
:func:`read_pdf_rows` returns :class:`PositionedRow` values instead: every
page's characters, grouped into visual rows and tokens with their ``x0``/``x1``
extent, so a parser can decide a movement's currency from where its amount sits
on the page. It is generic -- it carries no statement-specific meaning -- and
:mod:`expensuchis.importers.bbva_card` is pure over the sequence it returns,
never over a PDF library, exactly as :func:`read_pdf` lets the other parsers
stay pure over flat text.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "PositionedRow",
    "PositionedToken",
    "read_creation_date",
    "read_pdf",
    "read_pdf_rows",
]

#: The separator ``read_pdf`` joins pages with; the parser's line layout splits on it.
PAGE_SEPARATOR = "\f"

#: Two characters belong to the same visual row when their ``loose=True`` top y
#: differs by no more than this many points. See :func:`read_pdf_rows`.
_ROW_Y_TOLERANCE = 2.0

#: Two adjacent non-whitespace characters belong to different tokens when the
#: horizontal gap between them exceeds this many points -- what splits two
#: table cells placed with no literal space character between them, and what
#: still splits normal words once whitespace characters are dropped (a real
#: space's width comfortably exceeds this).
_TOKEN_GAP_TOLERANCE = 2.5

#: The PDF date-string format (ISO 32000): ``D:YYYYMMDDHHmmSS`` followed by an
#: optional UTC-offset suffix this reader does not need. The leading ``D:`` is
#: itself optional per the spec, so it is not required here.
_PDF_DATE_RE = re.compile(r"^(?:D:)?(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})")


def read_pdf(path: str | Path) -> tuple[str, int]:
    """Return ``(text, page_count)`` for the PDF at ``path``.

    The extracted text is the per-page text joined with :data:`PAGE_SEPARATOR`. The
    document is closed in a ``finally`` block, so a failure while reading a page
    still releases the underlying file handle. Never prints and never writes.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        pages = [page.get_textpage().get_text_range() for page in document]
        return PAGE_SEPARATOR.join(pages), len(document)
    finally:
        document.close()


def read_creation_date(path: str | Path) -> dt.date | None:
    """Return the PDF's ``CreationDate`` metadata field as a bare date.

    Returns ``None`` when the field is absent, empty or does not match the PDF
    date-string format well enough to yield a real calendar date -- never
    raises on malformed metadata, since a missing or unparseable date is the
    caller's business to refuse (with its own, masked message), not this
    reader's. The document is closed in a ``finally`` block. Never prints and
    never writes.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        raw = document.get_metadata_value("CreationDate")
    finally:
        document.close()
    match = _PDF_DATE_RE.match(raw)
    if match is None:
        return None
    try:
        return dt.date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None


@dataclass(frozen=True)
class PositionedToken:
    """One run of characters with its horizontal extent on the page.

    ``x0``/``x1`` are the leftmost and rightmost character-box edges (points,
    PDF user space), read with ``loose=True`` -- see :func:`read_pdf_rows`.
    ``text`` is the joined character run, exactly as extracted (no
    normalization).
    """

    x0: float
    x1: float
    text: str


@dataclass(frozen=True)
class PositionedRow:
    """One visual row of tokens, grouped by y position on one page.

    ``page`` is 1-based; ``row`` is 1-based within its own page -- the same
    convention the flat-text parsers use for masked diagnostics via
    ``(page, line)``. ``tokens`` are in left-to-right (``x0`` ascending) order.
    A row with no non-whitespace characters is never produced (see
    :func:`read_pdf_rows`), so ``tokens`` is never empty.
    """

    page: int
    row: int
    tokens: tuple[PositionedToken, ...]


def read_pdf_rows(path: str | Path) -> tuple[PositionedRow, ...]:
    """Return every page's text as position-aware rows of tokens.

    Built from ``pypdfium2`` character boxes read with ``loose=True`` --
    *tight* boxes put a decimal ``.``/``,`` on a different row from its digits
    and split every amount (observed during the T-07 card reconnaissance;
    :data:`_ROW_Y_TOLERANCE` would otherwise open a new row mid-number).
    Whitespace characters are dropped rather than kept as tokens: a run of
    consecutive non-whitespace characters is one token, split further on a
    horizontal gap wider than :data:`_TOKEN_GAP_TOLERANCE` -- which is what
    separates two table cells placed with no literal space character between
    them (an amount column against the next one over).

    This primitive is generic: it has no notion of what the tokens mean. A
    card liquidación parser (BBVA's two-column layout) is pure over the
    :class:`PositionedRow` sequence this returns, never over a PDF library,
    exactly as :func:`read_pdf`'s flat text keeps the other parsers pure.

    The document is closed in a ``finally`` block. Never prints and never
    writes.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        rows: list[PositionedRow] = []
        for page_index, page in enumerate(document):
            textpage = page.get_textpage()
            try:
                rows.extend(_page_rows(textpage, page_index + 1))
            finally:
                textpage.close()
        return tuple(rows)
    finally:
        document.close()


def _page_rows(textpage: Any, page: int) -> list[PositionedRow]:
    """Return one page's :class:`PositionedRow` values, in reading order."""
    chars: list[tuple[float, float, float, str]] = []  # (x0, x1, top, char)
    for index in range(textpage.count_chars()):
        char = textpage.get_text_range(index, 1)
        if char.strip() == "":
            continue
        left, _bottom, right, top = textpage.get_charbox(index, loose=True)
        chars.append((left, right, top, char))

    groups: list[list[tuple[float, float, float, str]]] = []
    current: list[tuple[float, float, float, str]] = []
    current_top: float | None = None
    for left, right, top, char in chars:
        if current and abs(top - current_top) > _ROW_Y_TOLERANCE:  # type: ignore[arg-type]
            groups.append(current)
            current = []
        if not current:
            current_top = top
        current.append((left, right, top, char))
    if current:
        groups.append(current)

    return [
        PositionedRow(page=page, row=row_index, tokens=tuple(_tokenize(group)))
        for row_index, group in enumerate(groups, start=1)
    ]


def _tokenize(chars: list[tuple[float, float, float, str]]) -> list[PositionedToken]:
    """Split one row's ordered non-whitespace characters into tokens.

    A new token starts when the horizontal gap since the previous character's
    right edge exceeds :data:`_TOKEN_GAP_TOLERANCE`. Whitespace characters are
    never part of ``chars`` (the caller drops them), so an ordinary space
    between words splits tokens through this same gap check, never through the
    space glyph's own (sometimes unreliable) box.
    """
    tokens: list[PositionedToken] = []
    token_x0: float | None = None
    token_x1: float | None = None
    token_text = ""
    for left, right, _top, char in chars:
        if token_x1 is not None and left - token_x1 > _TOKEN_GAP_TOLERANCE:
            tokens.append(PositionedToken(token_x0, token_x1, token_text))  # type: ignore[arg-type]
            token_x0 = None
            token_text = ""
        if token_x0 is None:
            token_x0 = left
            token_text = char
        else:
            token_text += char
        token_x1 = right
    if token_x0 is not None:
        tokens.append(PositionedToken(token_x0, token_x1, token_text))  # type: ignore[arg-type]
    return tokens
