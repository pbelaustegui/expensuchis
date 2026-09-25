"""Privacy guard #2: no data-bearing files inside the working tree.

This repository is public. Real financial statements and the real ledger must never
enter it, not even untracked. This test walks the *working tree* (not just tracked
files) because an untracked statement is one ``git add .`` away from being published.

Data-bearing files are only allowed under the synthetic-fixture allowlist.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from expensuchis.privacy import DATA_EXTENSIONS, EXCLUDED_DIRS, find_data_bearing_files

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_data_bearing_files_outside_allowlist() -> None:
    offenders = find_data_bearing_files(REPO_ROOT)
    assert offenders == [], (
        "Data-bearing files found outside the synthetic-fixture allowlist "
        "(tests/fixtures/**, sample/**): "
        + ", ".join(offenders)
        + ". Real financial data must live outside this public repository."
    )


def test_find_data_bearing_files_component_semantics(tmp_path: Path) -> None:
    (tmp_path / "sample").mkdir()
    (tmp_path / "sample" / "smoke.beancount").write_text("")
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    (tmp_path / "tests" / "fixtures" / "allowed.csv").write_text("")
    # Sibling whose name merely *starts with* the allowlist component: not allowed.
    (tmp_path / "tests" / "fixtures-old").mkdir()
    (tmp_path / "tests" / "fixtures-old" / "leak.csv").write_text("")
    (tmp_path / "leak.csv").write_text("")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.csv").write_text("")
    (tmp_path / "notes.txt").write_text("")

    assert find_data_bearing_files(tmp_path) == [
        "leak.csv",
        "tests/fixtures-old/leak.csv",
    ]


def test_privacy_module_exits_zero_when_clean() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "expensuchis.privacy"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"expected a clean scan, exit {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.stdout == ""


def test_guard_configuration_is_non_empty() -> None:
    assert DATA_EXTENSIONS
    assert EXCLUDED_DIRS
