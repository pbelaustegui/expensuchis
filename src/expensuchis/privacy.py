"""Privacy guard #2: no data-bearing files inside the working tree.

This repository is public. Real financial statements and the real ledger must
never enter it, not even untracked: an untracked statement is one ``git add .``
away from being published, so scanning tracked files only is not enough.

The scan walks the *working tree* and reports every file whose extension names a
data-bearing format, unless it lives under the synthetic-fixture allowlist.
Detection is name-based and therefore a tripwire, not a wall: a statement renamed
to ``.txt``/``.json``/``.dat``, a hardlink with a benign name, or a file inside an
excluded directory all pass. The structural mitigations are that the ledger never
lives inside the tree (privacy guard #1) and that this check runs in CI (``pytest``
re-runs it).

There is one deliberate exception to the directory exclusions: ``.git`` is skipped
as a whole **except** for ``.git/gentle-ai/**``, the tooling cache that once held
plain-text candidate views of repository files and was the third place the original
leak appeared. That cache is scanned so a data-bearing file copied into it is
caught; the rest of ``.git`` — the object store — stays excluded, because it holds
no scannable data files and walking it is useless.

Exit codes distinguish a crash from a finding: ``1`` means data-bearing files were
found, ``3`` means the guard itself crashed. A crash is never reported as a finding.

Run it directly to scan the repository and fail on any offender::

    python -m expensuchis.privacy
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from expensuchis.ledger import repository_roots

#: Exit code contract: 0 clean, 1 offenders found, 3 the guard crashed.
EXIT_CODES = {"clean": 0, "offender": 1, "crash": 3}

DATA_EXTENSIONS = {
    ".pdf",
    ".xls",
    ".xlsx",
    ".csv",
    ".ofx",
    ".qif",
    ".beancount",
    ".bean",
    ".ledger",
}

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}


def is_allowlisted(rel: Path) -> bool:
    """Return whether ``rel`` (repo-relative) is under the synthetic allowlist.

    The comparison is on exact path *components*, never on string prefixes, so a
    sibling such as ``tests-fixtures-old`` is not mistaken for ``tests/fixtures``.
    """
    parts = rel.parts
    if parts[:2] == ("tests", "fixtures"):
        return True
    return parts[:1] == ("sample",)


def _excluded(rel: Path) -> bool:
    """Return whether ``rel`` (repo-relative) is an excluded file path.

    ``.git`` is excluded as a whole, except the tooling cache ``.git/gentle-ai/**``,
    which is scanned because it once held plain-text candidate views of repository
    files. A nested ``.git`` (a submodule) stays excluded.
    """
    parts = rel.parts
    if parts[:1] == (".git",):
        return parts[:2] != (".git", "gentle-ai")
    return any(part in EXCLUDED_DIRS for part in parts)


def _prune(rel: Path) -> bool:
    """Return whether the directory ``rel`` (repo-relative) must not be walked.

    The top-level ``.git`` is entered only to reach its ``gentle-ai`` cache, so it
    is kept while every other ``.git`` directory is pruned. This avoids walking the
    object store on every scan.
    """
    parts = rel.parts
    if parts == (".git",):
        return False
    if parts[:2] == (".git", "gentle-ai"):
        return False
    if ".git" in parts:
        return True
    return any(part in EXCLUDED_DIRS for part in parts)


def find_data_bearing_files(root: Path) -> list[str]:
    """Return the sorted repo-relative paths of data-bearing files under ``root``."""
    root = Path(root).resolve()
    offenders: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        rel_dir = current.relative_to(root)
        dirnames[:] = [name for name in dirnames if not _prune(rel_dir / name)]
        for name in filenames:
            path = current / name
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if _excluded(rel):
                continue
            if path.suffix.lower() not in DATA_EXTENSIONS:
                continue
            if is_allowlisted(rel):
                continue
            offenders.append(rel.as_posix())
    return sorted(offenders)


def repository_root() -> Path:
    """Return the repository root to scan.

    A discovered root that contains the current working directory wins, so a
    non-editable install invoked from inside the checkout scans the checkout and
    not ``site-packages``. Otherwise fall back to the module directory's root,
    and finally to the current working directory when no marker is found.
    """
    roots = repository_roots()
    cwd = Path.cwd().resolve()
    for root in roots:
        if root == cwd or root in cwd.parents:
            return root
    if roots:
        return min(roots, key=lambda path: len(path.parts))
    return cwd


def main() -> int:
    """Print offenders one per line and return 1 if any exist, else 0."""
    offenders = find_data_bearing_files(repository_root())
    for offender in offenders:
        print(offender)
    return EXIT_CODES["offender"] if offenders else EXIT_CODES["clean"]


def _entrypoint() -> int:
    """Run :func:`main`, mapping an unexpected exception to the crash exit code.

    A crash must be distinguishable from a real finding, so an unhandled error
    returns ``3`` and prints that the guard crashed, rather than the ``1`` that
    means data-bearing files were found. The hook can then say "crashed" instead
    of mislabelling it as a finding.
    """
    try:
        return main()
    except Exception as exc:  # noqa: BLE001 - a crash must be reported distinctly
        print(f"privacy guard crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CODES["crash"]


__all__ = [
    "DATA_EXTENSIONS",
    "EXCLUDED_DIRS",
    "EXIT_CODES",
    "find_data_bearing_files",
    "is_allowlisted",
    "main",
    "repository_root",
]


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
