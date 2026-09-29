"""Source importers for the expensuchis ingest pipeline.

One importer per source, as a package (``src/expensuchis/importers/``) built on
beangulp's :class:`beangulp.Importer` ABC. The first registered source is Mercado
Pago, which builds each entry's natural ``key`` from date + operation id + amount
and runs its own reconciliation checks as a hard gate before emitting anything.

Two rules every importer here follows, because the pipeline depends on them:

- **A failing importer raises, it never returns partial entries.** A reconciliation
  check that does not hold means the document was not understood; emitting the rows
  that happened to parse would write a plausible, wrong ledger. The Mercado Pago
  parser gate runs **seven** checks (header arithmetic, the two sum checks, the
  running-balance chain, the closing balance, key uniqueness and the frozen
  vocabulary), and the importer refuses a purchase or bill payment whose
  counterparty is not classified rather than defaulting it.
- **Heavyweight dependencies are imported lazily, inside ``identify`` and
  ``extract``.** A PDF engine such as ``pypdfium2`` is read through
  :func:`expensuchis.importers.pdf.read_pdf`, which imports it inside the function,
  so ``get_importers()`` and the CLI stay fast and a machine without the engine can
  still run everything that does not need it. The counterparty map is likewise
  created lazily at ``extract`` time, so no importer construction needs a ledger
  directory or environment variable.

A Mercado Pago statement declares whose account it is by its **per-person folder**:
``statements/MercadoPago/<person>/<file>.pdf`` maps to
``Assets:MercadoPago:<person>:Caja``. A path outside that layout is refused.
"""

from __future__ import annotations

from beangulp import Importer

from .bbva_card_importer import BBVACardImporter
from .bbva_importer import BBVAImporter
from .brubank_importer import BrubankImporter
from .mercadopago_importer import MercadoPagoImporter
from .provincia_importer import ProvinciaImporter
from .provincia_visa_importer import ProvinciaVisaImporter

__all__ = ["get_importers"]


def get_importers(*, reveal: bool = False) -> list[Importer]:
    """Return one fresh importer instance per supported source.

    Mercado Pago, the Provincia account extracto, the Provincia card
    liquidación, the Brubank account statement, the BBVA consolidated account
    statement (T-07b) and the BBVA Visa/Mastercard card liquidación (T-07e)
    are registered. The pipeline resolves its default importers through this
    function, so registering a source is a one-line change that every
    command sees.

    ``reveal`` is threaded only to :class:`BBVAImporter`, the only importer
    whose refusal message redacts a counterparty identity by default (a sent
    transfer's recipient CUIT; see :mod:`expensuchis.importers.bbva_importer`).
    No other importer here has this concept, so none of them takes it.
    """
    return [
        MercadoPagoImporter(),
        ProvinciaImporter(),
        ProvinciaVisaImporter(),
        BrubankImporter(),
        BBVAImporter(reveal=reveal),
        BBVACardImporter(),
    ]
