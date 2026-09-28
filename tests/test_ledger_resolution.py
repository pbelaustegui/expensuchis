"""Tests for privacy guard #1: the ledger must resolve outside the repository."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from expensuchis.ledger import ENV_VAR, LedgerDirError, ledger_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC = REPO_ROOT / "src" / "expensuchis"


def test_unset_variable_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(LedgerDirError) as excinfo:
        ledger_dir()
    assert ENV_VAR in str(excinfo.value)


def test_empty_variable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "")
    with pytest.raises(LedgerDirError):
        ledger_dir()


def test_path_inside_repository_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    inside = REPO_ROOT / "tests"
    monkeypatch.setenv(ENV_VAR, str(inside))
    with pytest.raises(LedgerDirError) as excinfo:
        ledger_dir()
    assert "inside the repository" in str(excinfo.value)


def test_repository_root_itself_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, str(REPO_ROOT))
    with pytest.raises(LedgerDirError):
        ledger_dir()


def test_non_existent_path_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "no-such-ledger-dir"
    monkeypatch.setenv(ENV_VAR, str(missing))
    with pytest.raises(LedgerDirError) as excinfo:
        ledger_dir()
    assert "does not exist" in str(excinfo.value)
    assert not missing.exists()


def test_path_that_is_a_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("synthetic placeholder\n")
    monkeypatch.setenv(ENV_VAR, str(file_path))
    with pytest.raises(LedgerDirError) as excinfo:
        ledger_dir()
    assert "is a file, not a directory" in str(excinfo.value)


def test_path_in_temp_dir_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    monkeypatch.setenv(ENV_VAR, str(ledger))
    assert ledger_dir() == ledger.resolve()


def test_sibling_prefix_directory_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A path whose *string* starts with the repository name but is not inside it.
    sibling = tmp_path / "expensuchis-ledgerr"
    sibling.mkdir()
    monkeypatch.setenv(ENV_VAR, str(sibling))
    assert ledger_dir() == sibling.resolve()


def test_non_editable_install_still_refuses_repository_ledger(tmp_path: Path) -> None:
    """Regression: a wheel-style install must not fail open.

    Simulates ``site-packages/expensuchis`` by copying the module files there and
    importing them through ``PYTHONPATH``. The module path then lives outside the
    checkout, so only the current-working-directory start point can discover the
    repository. The guard must still refuse a ledger directory inside it.
    """
    site_packages = tmp_path / "site-packages"
    installed = site_packages / "expensuchis"
    installed.mkdir(parents=True)
    for name in ("__init__.py", "ledger.py"):
        shutil.copyfile(PACKAGE_SRC / name, installed / name)

    inside = REPO_ROOT / "tests"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(site_packages)
    env[ENV_VAR] = str(inside)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from expensuchis.ledger import ledger_dir; ledger_dir()",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, (
        "guard #1 permitted a repository-internal ledger under a simulated "
        f"non-editable install\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "inside the repository" in result.stderr
