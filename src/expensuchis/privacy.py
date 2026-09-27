"""Privacy guard #2: no data-bearing files inside the working tree.

This repository is public. Real financial statements and the real ledger must
never enter it, not even untracked: an untracked statement is one ``git add .``
away from being published, so scanning tracked files only is not enough.

The scan walks the *working tree* and reports every file whose extension names a
data-bearing format, unless it lives under the synthetic-fixture allowlist.
Detection is name-based and therefore a tripwire, not a wall: a statement renamed
to ``.txt``/``.json``/``.dat``, a hardlink with a benign name, or a file inside an
excluded directory all pass. The structural mitigations are that the ledger never
lives inside the tree (privacy guard #1) and that this check runs in CI.

Run it directly to scan the repository and fail on any offender::

    python -m expensuchis.privacy
"""

from __future__ import annotations

from pathlib import Path

from expensuchis.ledger import repository_roots

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


def find_data_bearing_files(root: Path) -> list[str]:
    """Return the sorted repo-relative paths of data-bearing files under ``root``."""
    root = Path(root).resolve()
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
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
    return 1 if offenders else 0


__all__ = [
    "DATA_EXTENSIONS",
    "EXCLUDED_DIRS",
    "find_data_bearing_files",
    "is_allowlisted",
    "main",
    "repository_root",
]


if __name__ == "__main__":
    raise SystemExit(main())
