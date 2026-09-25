"""Privacy guard #3: statement tokens must never appear in this repository.

The repository is public and the statements are private. A real personal name or
aggregate amount reached local history once; the commit was never pushed, but a
plain ``git push`` would have published it permanently. Guards #1 and #2 keep the
statements *out* of the tree; this guard catches the opposite failure — a
*transformation* of statement text written into repository content.

**Sweep by token, not by phrase.** The sweep that finally found the leak failed
twice first. Its patterns were phrases taken from the statements, but what leaked
was the source text truncated, recased and reformatted, so the phrases were longer
than the leaks and matched nothing. What works is the inverse: take every token
from the statements and look for those tokens in repository content.

**No stoplist.** A second helper had a hand-written stoplist of tokens to skip, and
it masked real data: it hid two of the three service providers named in the tracker
while a third slipped through. This guard has no stoplist. It has a **reviewed
baseline** instead, and the difference is deliberate:

* the baseline is **derived from a reviewed sweep** — it is what the intersection
  actually contained after the leak was understood, not a guess written in advance;
* it **lives with the data**, in the ledger directory, next to the statements it
  came from, never in the repository it protects;
* it is **meant to be small**; and
* **every addition is a deliberate act** with a reason in the review record.

A hand-written stoplist grows silently and hides things; a reviewed baseline is
small, lives with the data, and each line is something a human decided to ignore.

Token rules
-----------

Every alphabetic token of five or more characters, lowercased, plus **every**
numeric token, normalised by stripping ``.`` and ``,`` so ``123,456`` and
``123456`` are the same token. Amounts without decimals matter as much as amounts
with them; matching only ``1,234.56``-shaped numbers was one of the three reasons
the first sweep missed the leak.

Where state lives
-----------------

The token index and its cache metadata, and the reviewed baseline, live **only in
the ledger directory** (:data:`INDEX_FILENAME`, :data:`BASELINE_FILENAME`). They
are never written inside the repository. The cache is keyed on the statements'
paths, sizes and modification times, so a commit does not re-read every PDF;
:func:`build_index` with ``force=True``, or ``--rebuild-index`` on the command
line, rebuilds it.

Fail-closed contract
--------------------

* **Ledger directory absent entirely** — ``EXPENSUCHIS_LEDGER_DIR`` unset, or
  pointing at a path that does not exist — a fresh clone or CI. The guard
  **reports that it cannot run and lets the commit proceed**, because the
  repository must stay usable without the private data (:attr:`GuardStatus.CANNOT_RUN`,
  exit 0).
* **Ledger directory configured but unusable** — exists yet its ``statements``
  directory is missing, empty, or a statement cannot be read. The guard **fails
  closed**: it reports the problem and refuses (:attr:`GuardStatus.UNREADABLE`,
  exit 2). It does not pass silently.
* **Intersection with a non-baseline token** — :attr:`GuardStatus.LEAK`, exit 1.
  The offending token is printed so the user can fix it, together with the
  repository file it appeared in and the statement file it came from.

The two absent-vs-unusable outcomes are deliberately different and are stated in
the output as well as here.

Token source seam
-----------------

The reader is injected. Production uses :func:`pdfium_text`, which imports
``pypdfium2`` lazily so that importing this module — which the test suite does —
never requires the dependency to be installed. Tests use a synthetic text source,
so the suite needs neither a real PDF nor a private ledger.

Run it directly, scanning staged content (what a commit would add) or the whole
tracked tree::

    python -m expensuchis.leakguard
    python -m expensuchis.leakguard --tree
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .ledger import ENV_VAR, LedgerDirError, ledger_dir
from .privacy import repository_root

__all__ = [
    "BASELINE_FILENAME",
    "INDEX_FILENAME",
    "Finding",
    "GuardResult",
    "GuardStatus",
    "LeakguardError",
    "StatementsUnreadable",
    "TokenIndex",
    "build_index",
    "evaluate",
    "extract_tokens",
    "load_baseline",
    "main",
    "pdfium_text",
    "scan",
    "statement_files",
]

INDEX_FILENAME = "leakguard-index.json"
BASELINE_FILENAME = "leakguard-baseline.txt"

MIN_ALPHA_TOKEN_LENGTH = 5

_LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)
_NUMBERS = re.compile(r"\d[\d.,]*\d|\d")

_EXIT_CODES = {"clean": 0, "leak": 1, "cannot_run": 0, "unreadable": 2}


class LeakguardError(Exception):
    """Base class for leak-guard failures."""


class StatementsUnreadable(LeakguardError):
    """The ledger is configured but its statements cannot be read."""


class GuardStatus(Enum):
    """The outcome of one guard run."""

    CLEAN = "clean"
    """Scanned content held no non-baseline statement token."""

    LEAK = "leak"
    """A non-baseline statement token appeared in repository content."""

    CANNOT_RUN = "cannot_run"
    """The ledger is absent; the commit proceeds because the repo must work without it."""

    UNREADABLE = "unreadable"
    """The ledger is configured but unusable; the guard refuses to pass silently."""


EXIT_CODES = _EXIT_CODES


@dataclass(frozen=True)
class Finding:
    """One non-baseline statement token found in one repository file."""

    token: str
    file: str
    statements: tuple[str, ...]


@dataclass
class GuardResult:
    """The result of :func:`evaluate`, ready to print from :func:`main`."""

    status: GuardStatus
    message: str
    findings: list[Finding] = field(default_factory=list)
    tokens_indexed: int = 0


def extract_tokens(text: str) -> set[str]:
    """Return the statement tokens of ``text`` under the guard's token rules.

    Alphabetic runs are lowercased and kept only at length
    :data:`MIN_ALPHA_TOKEN_LENGTH` or more. Numeric runs are normalised by
    stripping ``.`` and ``,`` so grouping is irrelevant to the comparison.
    """
    tokens: set[str] = set()
    for match in _LETTERS.finditer(text):
        token = match.group().lower()
        if len(token) >= MIN_ALPHA_TOKEN_LENGTH:
            tokens.add(token)
    for match in _NUMBERS.finditer(text):
        tokens.add(match.group().replace(".", "").replace(",", ""))
    return tokens


def pdfium_text(path: Path) -> str:
    """Return the text of a PDF statement, importing ``pypdfium2`` lazily.

    The import happens inside the function on purpose: importing this module must
    not require the dependency, because the test suite imports it and CI has no
    statements. ``pypdfium2`` is a declared runtime dependency, not an import-time
    one.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        return " ".join(page.get_textpage().get_text_range() for page in document)
    finally:
        document.close()


TokenSource = Callable[[Path], str]


def statement_files(root: Path) -> list[Path]:
    """Return the statement files under ``root/statements``, sorted.

    Sidecar ``*:Zone.Identifier`` files and dotfiles are skipped: they are download
    metadata, not statements.

    Raises:
        StatementsUnreadable: if ``root/statements`` is not a directory.
    """
    statements = root / "statements"
    if not statements.is_dir():
        raise StatementsUnreadable(
            f"the ledger directory {root} has no readable 'statements' directory; "
            f"the guard cannot verify the repository."
        )
    return sorted(
        path
        for path in statements.rglob("*")
        if path.is_file() and ":Zone.Identifier" not in path.name and not path.name.startswith(".")
    )


def _fingerprint(root: Path, files: list[Path]) -> list[dict[str, object]]:
    fingerprint: list[dict[str, object]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError as exc:
            raise StatementsUnreadable(f"cannot stat statement {path}: {exc}") from exc
        fingerprint.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return fingerprint


@dataclass
class TokenIndex:
    """Statement tokens mapped to the statement files that contain them."""

    token_to_files: dict[str, list[str]]

    @property
    def tokens(self) -> set[str]:
        return set(self.token_to_files)


def _load_cached_index(
    cache_path: Path, fingerprint: list[dict[str, object]]
) -> TokenIndex | None:
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict) or cached.get("fingerprint") != fingerprint:
        return None
    token_to_files = cached.get("tokens")
    if not isinstance(token_to_files, dict):
        return None
    return TokenIndex({str(token): [str(p) for p in paths] for token, paths in token_to_files.items()})


def build_index(
    root: Path,
    *,
    source: TokenSource | None = None,
    force: bool = False,
    files: list[Path] | None = None,
) -> TokenIndex:
    """Build (or load from cache) the statement token index.

    The cache lives at ``root/leakguard-index.json`` and is keyed on every
    statement's relative path, size and modification time. ``force=True`` ignores
    and rewrites it.

    Raises:
        StatementsUnreadable: if a statement cannot be read, or the resulting index
            is empty (an empty index would verify nothing).
    """
    reader = source if source is not None else pdfium_text
    discovered = files if files is not None else statement_files(root)
    fingerprint = _fingerprint(root, discovered)

    cache_path = root / INDEX_FILENAME
    if not force:
        cached = _load_cached_index(cache_path, fingerprint)
        if cached is not None:
            return cached

    token_to_files: dict[str, list[str]] = {}
    for path in discovered:
        try:
            text = reader(path)
        except Exception as exc:
            raise StatementsUnreadable(f"cannot read statement {path}: {exc}") from exc
        rel = path.relative_to(root).as_posix()
        for token in extract_tokens(text):
            token_to_files.setdefault(token, []).append(rel)

    if not token_to_files:
        raise StatementsUnreadable(
            f"no statement tokens were extracted from {root / 'statements'}; "
            f"the guard refuses to verify nothing."
        )

    index = TokenIndex(token_to_files)
    payload = json.dumps({"fingerprint": fingerprint, "tokens": token_to_files}, sort_keys=True)
    temporary = cache_path.with_name(cache_path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, cache_path)
    return index


def load_baseline(root: Path) -> set[str]:
    """Return the reviewed benign tokens from ``root/leakguard-baseline.txt``.

    The file is one token per line; blank lines and ``#`` comments are ignored. A
    missing file means an empty baseline, so every intersection fails. The tokens
    are lowercased on read to match the index.
    """
    path = root / BASELINE_FILENAME
    if not path.exists():
        return set()
    baseline: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        baseline.add(token.lower())
    return baseline


def scan(texts: Mapping[str, str], index: TokenIndex, baseline: set[str]) -> list[Finding]:
    """Return every non-baseline statement token found in ``texts``.

    ``texts`` maps a repository-relative file name to its content. A token found
    in several files yields one finding per file; a token found twice in one file
    yields one finding.
    """
    findings: list[Finding] = []
    for file, text in texts.items():
        for token in extract_tokens(text):
            if token in baseline or token not in index.token_to_files:
                continue
            findings.append(Finding(token, file, tuple(index.token_to_files[token])))
    return sorted(findings, key=lambda finding: (finding.file, finding.token))


def _resolve_root() -> tuple[Path | None, str]:
    """Return the ledger root, or ``(None, reason)`` when the ledger is absent.

    Absent means the environment variable is unset or the path does not exist: a
    fresh clone or CI. A configured-but-invalid ledger (inside the repository, a
    file, an unresolvable path) raises :class:`StatementsUnreadable` instead, so
    the guard fails closed.
    """
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return None, f"{ENV_VAR} is not set"
    candidate = Path(raw).expanduser()
    if not candidate.exists():
        return None, f"{ENV_VAR}={candidate} does not exist"
    try:
        return ledger_dir(), ""
    except LedgerDirError as exc:
        raise StatementsUnreadable(str(exc)) from exc


def evaluate(
    texts: Mapping[str, str],
    *,
    source: TokenSource | None = None,
    force_rebuild: bool = False,
    ledger_root: Path | None = None,
) -> GuardResult:
    """Run the guard over ``texts`` and return a printable result.

    ``ledger_root`` overrides environment resolution, which the tests use. When it
    is ``None``, the ledger is resolved from ``EXPENSUCHIS_LEDGER_DIR`` under the
    fail-closed contract described in the module docstring.
    """
    if ledger_root is None:
        try:
            root, reason = _resolve_root()
        except StatementsUnreadable as exc:
            return GuardResult(GuardStatus.UNREADABLE, f"leakguard refused: {exc}")
        if root is None:
            return GuardResult(
                GuardStatus.CANNOT_RUN,
                f"leakguard cannot run: {reason}. The ledger directory is absent, so "
                f"there is nothing to compare against; allowing the commit. Run the "
                f"guard where the statements are available.",
            )
    else:
        root = Path(ledger_root)

    try:
        index = build_index(root, source=source, force=force_rebuild)
    except StatementsUnreadable as exc:
        return GuardResult(GuardStatus.UNREADABLE, f"leakguard refused: {exc}")

    baseline = load_baseline(root)
    findings = scan(texts, index, baseline)
    if findings:
        return GuardResult(
            GuardStatus.LEAK,
            f"leakguard refused: {len(findings)} statement-derived token(s) found in "
            f"repository content. Remove them, or add a genuinely generic token to "
            f"{root / BASELINE_FILENAME} deliberately.",
            findings=findings,
            tokens_indexed=len(index.token_to_files),
        )
    return GuardResult(
        GuardStatus.CLEAN,
        f"leakguard: clean ({len(index.token_to_files)} statement tokens indexed, "
        f"{len(baseline)} baselined; no intersection with the scanned content).",
        tokens_indexed=len(index.token_to_files),
    )


def _git_lines(*args: str, root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise LeakguardError(
            f"git {' '.join(args)} failed in {root}: {completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    return [name for name in completed.stdout.decode("utf-8", "replace").split("\0") if name]


def _git_blob(root: Path, name: str) -> str:
    completed = subprocess.run(
        ["git", "show", f":{name}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise LeakguardError(f"cannot read staged content of {name}")
    return completed.stdout.decode("utf-8", "replace")


def staged_texts(root: Path) -> dict[str, str]:
    """Return the staged content of every file a commit would add or change."""
    names = _git_lines("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", root=root)
    return {name: _git_blob(root, name) for name in names}


def tracked_texts(root: Path) -> dict[str, str]:
    """Return the working-tree content of every tracked file."""
    texts: dict[str, str] = {}
    for name in _git_lines("ls-files", "-z", root=root):
        path = root / name
        if not path.is_file():
            continue
        texts[name] = path.read_text(encoding="utf-8", errors="replace")
    return texts


def _print_result(result: GuardResult) -> None:
    print(result.message)
    for finding in result.findings:
        statements = ", ".join(finding.statements)
        print(
            f"  token {finding.token!r} in {finding.file} "
            f"(statement: {statements})"
        )


def main(argv: list[str] | None = None) -> int:
    """Run the guard for the pre-commit hook or by hand.

    Default scans the staged content of the commit being made; ``--tree`` scans
    every tracked file instead. ``--rebuild-index`` ignores and rewrites the cache.
    """
    parser = argparse.ArgumentParser(
        prog="python -m expensuchis.leakguard",
        description="Refuse a commit whose repository content repeats a statement token.",
    )
    parser.add_argument(
        "--tree",
        action="store_true",
        help="scan every tracked file instead of the staged content of a commit",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="ignore and rewrite the statement token index cache",
    )
    args = parser.parse_args(argv)

    root = repository_root()
    try:
        texts = tracked_texts(root) if args.tree else staged_texts(root)
    except LeakguardError as exc:
        print(f"leakguard refused: {exc}", file=sys.stderr)
        return EXIT_CODES[GuardStatus.UNREADABLE.value]

    result = evaluate(texts, force_rebuild=args.rebuild_index)
    _print_result(result)
    return EXIT_CODES[result.status.value]


if __name__ == "__main__":
    raise SystemExit(main())
