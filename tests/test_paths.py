"""Tests for the ledger directory layout and its per-path repository guard."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest

from expensuchis import paths as paths_module
from expensuchis.ledger import ENV_VAR, LedgerDirError, assert_outside_repository
from expensuchis.paths import LedgerPaths

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every accessor, plus ``ensure``, as zero-argument callables over a LedgerPaths.
ACCESSORS = [
    ("root", lambda paths: paths.root),
    ("main", lambda paths: paths.main()),
    ("accounts", lambda paths: paths.accounts()),
    ("transactions_dir", lambda paths: paths.transactions_dir()),
    ("transaction_file", lambda paths: paths.transaction_file(dt.date(2026, 1, 1))),
    ("statements_dir", lambda paths: paths.statements_dir()),
    ("statement_dir", lambda paths: paths.statement_dir("MercadoPago")),
    ("staging_dir", lambda paths: paths.staging_dir()),
    ("staging_batch", lambda paths: paths.staging_batch("batch-1")),
    ("counterparties", lambda paths: paths.counterparties()),
    ("fx_dir", lambda paths: paths.fx_dir()),
    ("fx_series", lambda paths: paths.fx_series("bolsa")),
    ("redaction_key", lambda paths: paths.redaction_key()),
    ("ensure", lambda paths: paths.ensure()),
]


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))
    return root


def test_accessors_derive_every_path_from_the_ledger_root(ledger: Path) -> None:
    paths = LedgerPaths()
    assert paths.root == ledger.resolve()
    assert paths.main() == ledger.resolve() / "main.beancount"
    assert paths.accounts() == ledger.resolve() / "accounts.beancount"
    assert paths.transactions_dir() == ledger.resolve() / "transactions"
    assert paths.statements_dir() == ledger.resolve() / "statements"
    assert paths.statement_dir("MercadoPago") == ledger.resolve() / "statements" / "MercadoPago"
    assert paths.staging_dir() == ledger.resolve() / "staging"
    assert paths.staging_batch("2026-09-p1") == ledger.resolve() / "staging" / "2026-09-p1"
    assert paths.counterparties() == ledger.resolve() / "counterparties.tsv"
    assert paths.fx_dir() == ledger.resolve() / "fx"
    assert paths.fx_series("bolsa") == ledger.resolve() / "fx" / "bolsa.json"
    assert paths.redaction_key() == ledger.resolve() / "redaction.key"


def test_transaction_file_uses_the_month_of_the_date(ledger: Path) -> None:
    paths = LedgerPaths()
    assert paths.transaction_file(dt.date(2026, 1, 31)) == (
        ledger.resolve() / "transactions" / "2026-01.beancount"
    )
    # A month boundary and a year boundary.
    assert paths.transaction_file(dt.date(2026, 12, 1)).name == "2026-12.beancount"
    assert paths.transaction_file(dt.date(2027, 1, 1)).name == "2027-01.beancount"


def test_reading_a_path_creates_nothing(ledger: Path) -> None:
    paths = LedgerPaths()
    paths.transaction_file(dt.date(2026, 2, 10))
    paths.counterparties()
    paths.staging_batch("batch-1")
    key_path = paths.redaction_key()
    for accessor in (paths.main(), paths.accounts(), paths.transactions_dir()):
        assert not accessor.exists()
    assert not key_path.exists()


def test_ensure_is_idempotent_and_creates_only_inside(ledger: Path) -> None:
    paths = LedgerPaths()
    paths.ensure()
    paths.ensure()

    assert paths.transactions_dir().is_dir()
    assert paths.statements_dir().is_dir()
    assert paths.staging_dir().is_dir()
    assert paths.fx_dir().is_dir()

    created = sorted(entry.name for entry in ledger.iterdir())
    assert created == ["fx", "staging", "statements", "transactions"]


def test_ledger_dir_inside_repository_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, str(REPO_ROOT / "tests"))
    with pytest.raises(LedgerDirError):
        LedgerPaths()


def test_repository_root_as_ledger_dir_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, str(REPO_ROOT))
    with pytest.raises(LedgerDirError):
        LedgerPaths()


def test_assert_outside_repository_refuses_a_repository_path() -> None:
    with pytest.raises(LedgerDirError):
        assert_outside_repository(REPO_ROOT / "tests" / "test_paths.py")


def test_derived_path_below_the_base_inside_repository_is_refused(
    ledger: Path,
) -> None:
    """Regression: the base check alone is not enough.

    The ledger directory itself is outside the repository, but a subdirectory
    inside it symlinks back into the repository, so a *derived* path (a
    transaction file two levels down) resolves inside it. A base-only check
    accepts this; the per-path guard must refuse it.
    """
    transactions = ledger / "transactions"
    transactions.symlink_to(REPO_ROOT / "tests", target_is_directory=True)

    paths = LedgerPaths()
    with pytest.raises(LedgerDirError):
        paths.transactions_dir()
    with pytest.raises(LedgerDirError):
        paths.transaction_file(dt.date(2026, 3, 15))
    with pytest.raises(LedgerDirError):
        paths.ensure()


def test_statement_and_staging_names_cannot_escape_the_ledger(ledger: Path) -> None:
    paths = LedgerPaths()
    for bad in ("../escape", "a/b", "", ".", ".."):
        with pytest.raises(LedgerDirError):
            paths.statement_dir(bad)
        with pytest.raises(LedgerDirError):
            paths.staging_batch(bad)
        with pytest.raises(LedgerDirError):
            paths.fx_series(bad)


def test_root_is_re_validated_after_the_base_is_swapped(ledger: Path) -> None:
    """Regression: ``root`` used to return the stored path unchecked.

    ``ledger_dir`` validates the base once at construction. If the base is then
    replaced by a symlink that resolves into the repository, every *derived*
    accessor refuses, but ``root`` returned the stale path. It must re-validate.
    """
    paths = LedgerPaths()
    assert paths.root == ledger.resolve()

    ledger.rmdir()
    ledger.symlink_to(REPO_ROOT / "tests", target_is_directory=True)

    with pytest.raises(LedgerDirError):
        _ = paths.root


def test_every_accessor_refuses_after_the_base_is_swapped(ledger: Path) -> None:
    """A repository-pointing base must be refused by *every* accessor and ensure."""
    paths = LedgerPaths()
    ledger.rmdir()
    ledger.symlink_to(REPO_ROOT / "tests", target_is_directory=True)

    for name, accessor in ACCESSORS:
        with pytest.raises(LedgerDirError, match="inside the repository"):
            accessor(paths)


@pytest.mark.parametrize(("name", "accessor"), ACCESSORS)
def test_each_accessor_routes_through_the_per_path_guard(
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    accessor: Callable[[LedgerPaths], object],
) -> None:
    """Load-bearing proof: neutering the guard must make every accessor fail.

    If an accessor stopped calling ``assert_outside_repository``, this test would
    return a path instead of raising, so a single assertion no longer carries the
    guard for the whole module.
    """
    paths = LedgerPaths()

    def refuse(path: Path) -> Path:
        raise LedgerDirError(f"refused {path}")

    monkeypatch.setattr(paths_module, "assert_outside_repository", refuse)

    with pytest.raises(LedgerDirError):
        accessor(paths)
