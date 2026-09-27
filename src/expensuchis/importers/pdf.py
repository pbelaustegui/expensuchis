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
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["read_pdf"]

#: The separator ``read_pdf`` joins pages with; the parser's line layout splits on it.
PAGE_SEPARATOR = "\f"


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
