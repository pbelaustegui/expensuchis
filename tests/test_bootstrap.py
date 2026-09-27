"""Tests for the ledger bootstrap (structural skeleton only, never account names)."""

from __future__ import annotations

from pathlib import Path

import pytest

from expensuchis import bootstrap as bootstrap_module
from expensuchis.bootstrap import bootstrap
from expensuchis.ledger import ENV_VAR
from expensuchis.paths import LedgerPaths
from expensuchis.pipeline import ledger_errors


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LedgerPaths:
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))
    return LedgerPaths()


def test_bootstrap_writes_main_and_accounts(ledger: LedgerPaths) -> None:
    created = bootstrap(ledger)
    assert ledger.main() in created
    assert ledger.accounts() in created
    assert ledger.main().is_file()
    assert ledger.accounts().is_file()


def test_bootstrap_main_contains_the_pinned_options_and_includes(ledger: LedgerPaths) -> None:
    bootstrap(ledger)
    text = ledger.main().read_text(encoding="utf-8")
    assert 'option "title"' in text
    assert 'option "operating_currency" "ARS"' in text
    assert 'include "accounts.beancount"' in text
    assert 'include "transactions/*.beancount"' in text


def test_bootstrap_accounts_open_only_equity_and_clearing(ledger: LedgerPaths) -> None:
    bootstrap(ledger)
    text = ledger.accounts().read_text(encoding="utf-8")
    assert "open Equity:Opening-Balances" in text
    assert "open Assets:TransferenciaEnTransito" in text
    # The tool never invents person, entity or card accounts: those name the
    # household's real accounts and are personal data.
    assert "Assets:BBVA" not in text
    assert "Liabilities:" not in text
    assert "Income:" not in text


def test_bootstrap_accounts_state_the_naming_rule(ledger: LedgerPaths) -> None:
    bootstrap(ledger)
    text = ledger.accounts().read_text(encoding="utf-8")
    assert "personal" in text.lower()
    assert "by hand" in text.lower()


def test_bootstrapped_ledger_passes_the_gate(ledger: LedgerPaths) -> None:
    """An empty ledger is only clean because bootstrap seeds the include glob.

    ``include "transactions/*.beancount"`` fails ``bean-check`` when no file
    matches, so the empty-glob case is covered explicitly here.
    """
    bootstrap(ledger)
    assert ledger_errors(ledger) == []


@pytest.mark.parametrize("fail_on", [1, 2, 3])
def test_bootstrap_rolls_back_a_failed_write(
    ledger: LedgerPaths, monkeypatch: pytest.MonkeyPatch, fail_on: int
) -> None:
    real = Path.write_text
    calls = {"n": 0}

    def flaky(self, data, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == fail_on:
            raise OSError("synthetic disk failure")
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky)
    with pytest.raises(bootstrap_module.BootstrapError) as excinfo:
        bootstrap(ledger)
    assert excinfo.value.reason == bootstrap_module.BOOTSTRAP_FAILED
    assert not ledger.main().exists()
    assert not ledger.accounts().exists()


def test_bootstrap_refuses_to_overwrite_existing_files(ledger: LedgerPaths) -> None:
    bootstrap(ledger)
    main_before = ledger.main().read_bytes()
    with pytest.raises(bootstrap_module.BootstrapError) as excinfo:
        bootstrap(ledger)
    assert excinfo.value.reason == bootstrap_module.BOOTSTRAP_EXISTS
    assert ledger.main().read_bytes() == main_before


def test_bootstrap_refuses_and_writes_nothing_when_main_exists(ledger: LedgerPaths) -> None:
    ledger.main().write_text(";; hand-written\n", encoding="utf-8")
    with pytest.raises(bootstrap_module.BootstrapError):
        bootstrap(ledger)
    assert not ledger.accounts().exists()


def test_bootstrap_creates_the_layout_directories(ledger: LedgerPaths) -> None:
    bootstrap(ledger)
    assert ledger.transactions_dir().is_dir()
    assert ledger.statements_dir().is_dir()
    assert ledger.staging_dir().is_dir()
