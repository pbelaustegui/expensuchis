"""Source importers for the expensuchis ingest pipeline.

One importer per source, built on beangulp's :class:`beangulp.Importer` ABC. The
list is empty until T-05 registers the first one (Mercado Pago), which builds each
entry's natural ``key`` from date + operation id + amount and runs its own
reconciliation checks as a hard gate.

Two rules every importer here follows, because the pipeline depends on them:

- **A failing importer raises, it never returns partial entries.** A reconciliation
  check that does not hold means the document was not understood; emitting the rows
  that happened to parse would write a plausible, wrong ledger. T-05's five Mercado
  Pago checks gate on this rule.
- **Heavyweight dependencies are imported lazily, inside ``extract``.** A PDF engine
  such as ``pypdfium2`` must not be imported at module import time, so ``identify``
  and the CLI stay fast and a machine without the engine can still run everything
  that does not need it.
"""

from __future__ import annotations

from beangulp import Importer

__all__ = ["get_importers"]


def get_importers() -> list[Importer]:
    """Return one fresh importer instance per supported source.

    Empty for now. T-05 adds the Mercado Pago importer here, T-06 Provincia, T-07
    BBVA and T-08 Brubank. The pipeline resolves its default importers through this
    function, so registering a source is a one-line change that every command sees.
    """
    return []
