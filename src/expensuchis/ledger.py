"""Resolution of the out-of-repository ledger location (privacy guard #1).

The ledger and every statement live outside this repository, permanently: the
repository is public, so a ledger path can never be derived from it. The only
accepted source of truth is the ``EXPENSUCHIS_LEDGER_DIR`` environment variable,
and even then the resolved path must be an existing directory outside every
repository this code can discover.

Repository roots are discovered by marker, not by positional path arithmetic.
A root is the nearest ancestor of a starting directory that contains either a
``.git`` entry or a ``pyproject.toml`` file; when the two markers appear at
different levels, ``.git`` wins, because the VCS root is the boundary that
matters for a public repository. Two starting points are used — this module's
own directory and the current working directory — and the guard holds whenever at
least one of them lies inside the checkout. That covers this project's normal usage
(editable install, ``uv run`` from the checkout, from any cwd) and a wheel install
invoked from inside the checkout.

**Known boundary, documented rather than hidden:** a non-editable install invoked
from an unrelated directory leaves neither start point inside the checkout, so no
root is discovered and a candidate inside the checkout would be accepted. That
combination has no trigger in this project's usage. The guard is deliberately not
tightened further, because the stricter rule — refusing any ledger inside *any* git
worktree — would block legitimate setups, such as versioning the ledger in a private
repository of its own.

A starting point that yields no marker contributes no root: an installed tool run
from an unrelated directory legitimately has no repository to protect.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "EXPENSUCHIS_LEDGER_DIR"

_GIT_MARKER = ".git"
_PROJECT_MARKER = "pyproject.toml"


class LedgerDirError(RuntimeError):
    """Raised when the ledger location is missing or points inside the repository."""


def find_repository_root(start: Path) -> Path | None:
    """Return the repository root for ``start``, or ``None`` if no marker exists.

    Walks up from ``start`` through its ancestors and returns the nearest one
    containing a ``.git`` entry or a ``pyproject.toml`` file. When ``.git`` and
    ``pyproject.toml`` appear at different levels, ``.git`` is preferred, so a
    nested project directory does not shadow the enclosing VCS root.
    """
    start = start.resolve()
    fallback: Path | None = None
    for ancestor in (start, *start.parents):
        if (ancestor / _GIT_MARKER).exists():
            return ancestor
        if fallback is None and (ancestor / _PROJECT_MARKER).is_file():
            fallback = ancestor
    return fallback


def repository_roots() -> set[Path]:
    """Return the deduplicated repository roots discovered from two start points.

    The module directory covers an editable install, where the code lives inside
    the checkout; the current working directory covers a wheel install invoked
    from inside the checkout. A start point without a marker contributes nothing
    rather than a guessed fallback.
    """
    roots: set[Path] = set()
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        found = find_repository_root(start)
        if found is not None:
            roots.add(found)
    return roots


def _containing_repository(candidate: Path) -> Path | None:
    """Return the discovered root that contains ``candidate``, if any."""
    for root in repository_roots():
        if candidate == root or root in candidate.parents:
            return root
    return None


def assert_outside_repository(path: Path) -> Path:
    """Return ``path`` resolved, refusing any location inside a repository.

    This is the per-path guard that closes the defect a previous review found in
    :func:`ledger_dir`: bounding only the base directory is not enough, because a
    derived path below the base can still resolve inside a repository (a symlinked
    subdirectory, or any layout that nests a checkout under the base). Every path
    the ledger layout hands out therefore goes through this check individually,
    not just the root it descends from.

    The path does not need to exist: ``resolve`` follows the existing components
    and normalises the rest, so a symlinked parent is still detected.

    Raises:
        LedgerDirError: if ``path`` resolves to a repository root or a descendant
            of one.
    """
    candidate = Path(path).expanduser().resolve()
    root = _containing_repository(candidate)
    if root is not None:
        raise LedgerDirError(
            f"Ledger path {candidate} is inside the repository at {root}. "
            f"Ledger data must never live inside this public repository; derive "
            f"it from {ENV_VAR} instead."
        )
    return candidate


def ledger_dir() -> Path:
    """Return the resolved ledger directory, outside every discoverable repository.

    The directory must already exist: a typo would otherwise be accepted and
    silently split the ledger across two directories.

    Raises:
        LedgerDirError: if ``EXPENSUCHIS_LEDGER_DIR`` is unset, empty, does not
            exist, is not a directory, or resolves inside a repository (or equal
            to its root).
    """
    raw = os.environ.get(ENV_VAR)
    if not raw:
        raise LedgerDirError(
            f"{ENV_VAR} is not set. The ledger must live outside this repository. "
            f"Set it to an absolute path outside the repository, for example: "
            f'export {ENV_VAR}="$HOME/expensuchis-ledger"'
        )

    candidate = Path(raw).expanduser().resolve()

    if not candidate.exists():
        raise LedgerDirError(
            f"{ENV_VAR}={candidate} does not exist. Create the ledger directory "
            f"first (outside this public repository), for example: "
            f"mkdir -p {candidate}"
        )
    if not candidate.is_dir():
        raise LedgerDirError(
            f"{ENV_VAR}={candidate} is a file, not a directory. Point {ENV_VAR} "
            f"at a directory outside this public repository."
        )

    assert_outside_repository(candidate)

    return candidate
