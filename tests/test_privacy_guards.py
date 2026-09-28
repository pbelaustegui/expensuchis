"""Privacy guard #2: no data-bearing files inside the working tree.

This repository is public. Real financial statements and the real ledger must never
enter it, not even untracked. This test walks the *working tree* (not just tracked
files) because an untracked statement is one ``git add .`` away from being published.

Data-bearing files are only allowed under the synthetic-fixture allowlist.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from expensuchis import privacy
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


def test_git_gentle_ai_cache_is_scanned(tmp_path: Path) -> None:
    """The tooling cache that held the third copy of the leak is scanned."""
    (tmp_path / ".git" / "gentle-ai" / "candidate-views").mkdir(parents=True)
    (tmp_path / ".git" / "gentle-ai" / "candidate-views" / "planted.pdf").write_text("")

    assert find_data_bearing_files(tmp_path) == [".git/gentle-ai/candidate-views/planted.pdf"]


def test_rest_of_git_is_still_excluded(tmp_path: Path) -> None:
    """Only ``.git/gentle-ai/**`` is scanned; the object store and refs are not."""
    (tmp_path / ".git" / "gentle-ai" / "candidate-views").mkdir(parents=True)
    (tmp_path / ".git" / "gentle-ai" / "candidate-views" / "found.csv").write_text("")
    (tmp_path / ".git" / "objects" / "ab").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "ab" / "blob.csv").write_text("")
    (tmp_path / ".git" / "lfs" / "store").mkdir(parents=True)
    (tmp_path / ".git" / "lfs" / "store" / "object.ofx").write_text("")
    (tmp_path / ".git" / "COMMIT_EDITMSG.pdf").write_text("")

    assert find_data_bearing_files(tmp_path) == [".git/gentle-ai/candidate-views/found.csv"]


def test_a_nested_git_directory_stays_excluded(tmp_path: Path) -> None:
    """A submodule's ``.git`` is not the top-level tooling cache and stays excluded."""
    (tmp_path / "sub" / ".git" / "x").mkdir(parents=True)
    (tmp_path / "sub" / ".git" / "x" / "leak.csv").write_text("")

    assert find_data_bearing_files(tmp_path) == []


def test_a_crash_is_reported_as_a_crash_not_as_a_finding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash must exit 3, never the 1 that means data-bearing files were found."""

    def boom() -> list[str]:
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(privacy, "find_data_bearing_files", boom)

    assert privacy._entrypoint() == privacy.EXIT_CODES["crash"]
    assert "crashed" in capsys.readouterr().err.lower()


def _scratch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def test_pre_commit_hook_reports_a_privacy_crash_as_a_crash(tmp_path: Path) -> None:
    """The hook's crash branch must not say "data-bearing files found".

    The guard's own crash-to-3 mapping is covered by
    :func:`test_a_crash_is_reported_as_a_crash_not_as_a_finding`; this test covers
    the shell mapping from exit 3 to the CRASHED message.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *expensuchis.privacy*) exit 3 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"

    completed = subprocess.run(
        [str(REPO_ROOT / ".githooks" / "pre-commit")],
        cwd=_scratch_repo(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "CRASHED" in completed.stderr
    assert "data-bearing files found" not in completed.stderr
