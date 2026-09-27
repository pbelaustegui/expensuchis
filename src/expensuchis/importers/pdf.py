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
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

__all__ = ["read_creation_date", "read_pdf"]

#: The separator ``read_pdf`` joins pages with; the parser's line layout splits on it.
PAGE_SEPARATOR = "\f"

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
        return dt.date(
            int(match.group("year")), int(match.group("month")), int(match.group("day"))
        )
    except ValueError:
        return None
