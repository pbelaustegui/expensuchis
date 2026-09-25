"""The ledger directory layout, with every derived path guarded (privacy guard #1).

The ledger lives outside this public repository, in the directory named by
``EXPENSUCHIS_LEDGER_DIR``. :func:`expensuchis.ledger.ledger_dir` already refuses a
base directory that sits inside a discoverable repository, but that bounds **only
the base**: a base outside the repository can still yield a derived path inside it
(a symlinked subdirectory, or a layout that nests a checkout under the base). Every
path this module hands out is therefore passed through
:func:`expensuchis.ledger.assert_outside_repository` individually. That per-path
check is the fix for the base-only defect, not a duplicate of it.

The layout is::

    $EXPENSUCHIS_LEDGER_DIR/
      main.beancount                    the file bean-check runs on
      accounts.beancount                open directives and options
      transactions/<YYYY-MM>.beancount  appended transactions, one file per month
      statements/<Source>/...           the raw files the user downloads
      staging/<batch-id>/               proposed batches awaiting review
      counterparties.tsv                the learned counterparty map

Reading a path never creates anything. :meth:`LedgerPaths.ensure` is the only
place that touches the filesystem, and it only creates directories inside the
ledger directory.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from .ledger import LedgerDirError, assert_outside_repository, ledger_dir

__all__ = ["LedgerPaths"]

_MAIN = "main.beancount"
_ACCOUNTS = "accounts.beancount"
_TRANSACTIONS = "transactions"
_STATEMENTS = "statements"
_STAGING = "staging"
_COUNTERPARTIES = "counterparties.tsv"


def _safe_name(value: str, what: str) -> str:
    """Return ``value`` unchanged, or raise if it could escape its parent directory.

    ``statements/<Source>`` and ``staging/<batch-id>`` take user-supplied names, so
    they must be single directory components: no separators, no traversal, no NUL.
    """
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise LedgerDirError(
            f"Invalid {what} {value!r}: it must be a single directory name, "
            f"not a path or a traversal."
        )
    return value


class LedgerPaths:
    """Resolved layout over a single ledger directory.

    Construction resolves ``EXPENSUCHIS_LEDGER_DIR`` once and stores it, so the
    environment cannot change the root between two accessor calls. Each accessor
    derives its path from that root and validates it independently.
    """

    def __init__(self) -> None:
        self._root = ledger_dir()

    @property
    def root(self) -> Path:
        """The resolved, already-validated ledger directory itself."""
        return self._root

    @staticmethod
    def _validated(path: Path) -> Path:
        return assert_outside_repository(path)

    def main(self) -> Path:
        """The top-level ledger file ``bean-check`` runs on."""
        return self._validated(self._root / _MAIN)

    def accounts(self) -> Path:
        """The file holding the ``open`` directives and ledger options."""
        return self._validated(self._root / _ACCOUNTS)

    def transactions_dir(self) -> Path:
        """The directory holding one transaction file per month."""
        return self._validated(self._root / _TRANSACTIONS)

    def transaction_file(self, when: dt.date) -> Path:
        """Return the transaction file for the month containing ``when``.

        Creates nothing; the file may not exist yet.
        """
        return self._validated(self._root / _TRANSACTIONS / f"{when:%Y-%m}.beancount")

    def statements_dir(self) -> Path:
        """The root of the raw files the user downloads, one subdirectory per source."""
        return self._validated(self._root / _STATEMENTS)

    def statement_dir(self, source: str) -> Path:
        """The directory for one statement ``source`` (for example ``MercadoPago``)."""
        return self._validated(self._root / _STATEMENTS / _safe_name(source, "source"))

    def staging_dir(self) -> Path:
        """The root of the proposed batches awaiting review."""
        return self._validated(self._root / _STAGING)

    def staging_batch(self, batch_id: str) -> Path:
        """The directory for one ``batch_id`` awaiting review."""
        return self._validated(self._root / _STAGING / _safe_name(batch_id, "batch id"))

    def counterparties(self) -> Path:
        """The learned counterparty map (personal data, so it lives here)."""
        return self._validated(self._root / _COUNTERPARTIES)

    def ensure(self) -> None:
        """Create the ledger directory structure idempotently.

        Explicit on purpose: nothing creates directories as a side effect of
        reading a path. Every directory is validated *before* it is created, so a
        symlinked subdirectory pointing into a repository is refused rather than
        materialised.
        """
        for directory in (
            self._root / _TRANSACTIONS,
            self._root / _STATEMENTS,
            self._root / _STAGING,
        ):
            self._validated(directory).mkdir(parents=True, exist_ok=True)
