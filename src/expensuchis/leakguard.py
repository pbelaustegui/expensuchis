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

**A reviewed stop-list — not "no stop-list".** A second helper had a hand-written
stoplist of tokens to skip, and it masked real data: it hid two of the three
service providers named in the tracker while a third slipped through. This guard's
baseline **is** a stop-list in the mechanical sense: every token in it is skipped.
The honest description is a **reviewed, minimal, load-bearing stop-list**, and the
difference from that hand-written list is real rather than rhetorical:

* it is **derived from a reviewed sweep** — it is what the intersection actually
  contained after the leak was understood, not a guess written in advance;
* it **lives with the data**, in the ledger directory, next to the statements it
  came from, never in the repository it protects;
* it is **meant to be small** — with an empty baseline the guard flags exactly the
  72 tokens the current file skips, and each of those was verified as necessary;
* **no entry masks a private identifier** — each is a generic word the repository
  itself uses (``supermercado``, ``seguros``, …), not a name or account number;
* **every addition is a deliberate act** with a reason recorded inline next to the
  entry; and
* **every numeric entry is derived, not listed** (see the numeric rule below).

A hand-written stoplist grows silently and hides things; this reviewed stop-list is
small, lives with the data, and each line is something a human decided to ignore,
with the reason written beside it. Calling it "no stoplist" would be rhetoric that
overstates the mechanism: the guard skips tokens, and the honesty is in *which*
tokens and *why*, not in a claim that nothing is skipped.

What the guard catches, and what it does not
--------------------------------------------

The guard catches **distinctive tokens** — names, account numbers and distinctive
amounts — that appear verbatim in the statements and are reproduced in repository
content. It is a token guard, not a data-loss-prevention system. The following are
known to evade it and are recorded here rather than left implied:

* an identifier **split across two lines or across two files** — the token never
  appears whole;
* a **zero-width or lookalike character** inserted inside a name — the token is no
  longer byte-equal;
* an identifier in a **filename** rather than in file content — only content is
  scanned, never path names;
* **base64** (or any other encoding) of an identifier;
* an amount written with a **space as the thousands separator** (``13 000,00``):
  the numeric check does not join numbers across whitespace, so it sees only short
  fragments;
* **fixed by T-01d, recorded because it was a real incident**: the finding output
  used to print the raw token and the source statement path unconditionally, and
  where that path contains the account holder's name — one real statement
  filename does — the output was not safe to paste into a public log. The default
  is now redacted (:func:`format_finding`, ``reveal=False``): a token's shape, a
  short content hash and the repository file and line, never the token or the
  path. ``--reveal`` restores the old output, for the owner at a terminal only;
* a statement that parses to **empty text** (a scanned statement with no text
  layer, for example): it contributes no tokens and is silently ignored. The guard
  fails closed only when *every* statement is empty, not when one of them is;
* a leak phrased as a **single generic domain word**. This repository's category
  tree and tracker use the statements' own vocabulary (``Supermercado``,
  ``Seguros``, ``Sueldo``, …), so those words cannot be flagged without flagging
  the repository itself; they are reviewed baseline entries instead. **A leak
  phrased as a single generic domain word is undetectable by a token guard, and
  this is what the guard does not protect.**

The git **author identity** is public by design and is out of scope for this guard.

Token rules
-----------

Every alphabetic token of five or more characters, lowercased. Numeric tokens are
normalised by stripping ``.`` and ``,`` so ``123,456`` and ``123456`` are the same
token. **Only numeric tokens of six or more digits are checked at all**, and the
legitimate numbers of this repository are **derived at scan time**, never listed by
hand (below). Amounts without decimals matter as much as amounts with them;
matching only ``1,234.56``-shaped numbers was one of the three reasons the first
sweep missed the leak.

Derived numeric rule, and its documented consequence
----------------------------------------------------

The guard carries no hand-listed numeric baseline. The numbers that legitimately
live in this repository are those found in ``uv.lock`` (dependency versions and
hashes) and under ``sample/`` and ``docs/``, computed at scan time. Any other
numeric token of six or more digits that appears both in a statement and in
repository content is a finding.

``docs/`` is included because its worked-example amounts are required to mirror the
synthetic ledger in ``sample/``. That is the intent, not the mechanism: the
implementation exempts **every** number in ``uv.lock``, ``sample/`` and ``docs/``
**unconditionally**, whether or not it mirrors anything. It is therefore not a
neutral exemption but a **blanket numeric blind spot**: **a real amount becomes
legitimate the moment it is saved into one of those three places**, exactly as if
it had been added to the baseline. ``odd/`` — the tracker, where the original leak
happened — and every other path are still checked, and **a new six-or-more-digit
amount in a new place fails**, which is the intent. Numbers below six digits are
not checked at all, so the numeric rule is a *delta* check, weaker than it first
reads. The clean message reports both the total number of derived numbers and how
many are actually checkable (six digits or more); the rest can never be flagged.

Where state lives
-----------------

The token index and its cache metadata, and the reviewed baseline, live **only in
the ledger directory** (:data:`INDEX_FILENAME`, :data:`BASELINE_FILENAME`). They
are never written inside the repository. The baseline is one token per line;
``#`` starts a comment and a reason may follow the token on the same line. The
cache is keyed on the statements' paths, sizes and modification times, so a commit
does not re-read every PDF; :func:`build_index` with ``force=True``, or
``--rebuild-index`` on the command line, rebuilds it.

Fail-closed contract
--------------------

* **Ledger directory unset** — ``EXPENSUCHIS_LEDGER_DIR`` is not set: a fresh clone
  or CI. The guard **reports that it cannot run and lets the commit proceed**
  (:attr:`GuardStatus.CANNOT_RUN`, exit 0), because the repository must stay usable
  without the private data.
* **Ledger directory set but missing or unusable** — the variable is set, but its
  path does not exist, is a file rather than a directory, lies inside a repository,
  or has no readable ``statements`` directory; or a statement cannot be read. The
  guard **fails closed** (:attr:`GuardStatus.UNREADABLE`, exit 2). It does not pass
  silently. A **typo must not silently disable a safety control**, which is why a
  set-but-missing path is unreadable while an unset variable is not.
* **Intersection with a non-baseline token** — :attr:`GuardStatus.LEAK`, exit 1.
  Each finding is printed redacted by default: the token's shape, a short content
  hash of the token, and the repository file and line it appeared in — never the
  raw token, never the statement file it came from. ``--reveal`` restores the raw
  token and the statement path, for the owner at a terminal only (T-01d).
* **The guard itself crashes** — exit 3, deliberately distinct from ``LEAK`` (1)
  so that a crash is never reported as a leaked token.

The absent-vs-unusable outcomes are deliberately different and are stated in the
output as well as here.

Commit messages
---------------

A commit message is permanent, and the pre-commit hook never sees it: git passes
**no** message argument to ``pre-commit``. The guard therefore accepts
``--message-file PATH``, which folds the message into the scanned text, and the
``.githooks/commit-msg`` hook invokes it with git's message-file argument. If the
message file is missing or unreadable the guard fails closed
(:attr:`GuardStatus.UNREADABLE`), rather than skipping the message silently.

Where this guard runs, and the bypass it does not close
-------------------------------------------------------

This guard runs from the local hooks only. CI runs ``bean-check``, ``pytest`` and
``ruff``, and ``pytest`` re-runs privacy guard #2; it never runs the leak guard,
because CI has no ledger. There is therefore **no CI backstop** for this guard.
``git commit --no-verify`` skips both ``.githooks/pre-commit`` and
``.githooks/commit-msg`` and fully evades it: a leaked token committed that way is
caught only when a human runs ``--tree`` (or when a later non-bypassed commit scans
the same content). This guard is **local-only by nature**, and the bypass is
**unpoliced**, not deferred to CI.

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
    python -m expensuchis.leakguard --message-file .git/COMMIT_EDITMSG

Every one of these is redacted by default (T-01d): a finding shows the token's
shape, a short content hash and its repository location, never the raw token or
the statement path. Add ``--reveal`` at a terminal, as the owner, to see the raw
token and the statement file(s) it matched::

    python -m expensuchis.leakguard --tree --reveal

Neither hook invocation ever passes ``--reveal``, so the hooks stay redacted.

:func:`staged_texts` lists only added/copied/modified/renamed paths
(``--diff-filter=ACMR``), so a token that was committed while the guard could not
run is **not** re-examined by a later commit. Run ``--tree`` periodically to
re-scan everything that is tracked; that is the recommended workflow, not an
optional extra.
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
from .redact import content_hash

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
    "derived_legitimate_numbers",
    "evaluate",
    "extract_tokens",
    "format_finding",
    "format_result",
    "load_baseline",
    "main",
    "pdfium_text",
    "scan",
    "statement_files",
]

INDEX_FILENAME = "leakguard-index.json"
BASELINE_FILENAME = "leakguard-baseline.txt"

MIN_ALPHA_TOKEN_LENGTH = 5
MIN_NUMERIC_TOKEN_LENGTH = 6

#: Repository files whose numbers are legitimate by construction: dependency
#: metadata. See :func:`derived_legitimate_numbers`.
DERIVED_NUMBER_FILES = ("uv.lock",)

#: Repository directories whose numbers are legitimate by construction: the
#: synthetic ledger and the design documents that mirror it.
DERIVED_NUMBER_DIRS = ("sample", "docs")

_LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)
_NUMBERS = re.compile(r"\d[\d.,]*\d|\d")

_EXIT_CODES = {"clean": 0, "leak": 1, "cannot_run": 0, "unreadable": 2, "crash": 3}


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
    """The ledger is unset; the commit proceeds because the repo must work without it."""

    UNREADABLE = "unreadable"
    """The ledger is configured but missing or unusable; the guard refuses to pass silently."""


EXIT_CODES = _EXIT_CODES


@dataclass(frozen=True)
class Finding:
    """One non-baseline statement token found in one repository file.

    ``line`` is the 1-based line number of the token's first occurrence in
    ``file``, or ``0`` when the token was not found on any single physical line
    (for example, it is split by a line wrap the extractor does not rejoin).
    ``token`` and ``statements`` are carried for :func:`format_finding`'s
    ``reveal=True`` path; the default rendering never prints them (T-01d).
    """

    token: str
    file: str
    line: int
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
    stripping ``.`` and ``,`` so grouping is irrelevant to the comparison. This
    function extracts; :func:`scan` decides which extracted tokens are checked.
    """
    tokens: set[str] = set()
    for match in _LETTERS.finditer(text):
        token = match.group().lower()
        if len(token) >= MIN_ALPHA_TOKEN_LENGTH:
            tokens.add(token)
    for match in _NUMBERS.finditer(text):
        tokens.add(match.group().replace(".", "").replace(",", ""))
    return tokens


def derived_legitimate_numbers(root: Path) -> set[str]:
    """Return the numbers that legitimately live in the repository at ``root``.

    Read at scan time from :data:`DERIVED_NUMBER_FILES` and
    :data:`DERIVED_NUMBER_DIRS`. Every number in ``uv.lock``, ``sample/`` and
    ``docs/`` is exempted **unconditionally**: the exemption does not check that a
    number mirrors the synthetic ledger, so a real amount becomes legitimate the
    moment it is saved into one of those places. That blanket blind spot — not a
    neutral exemption — is documented in the module docstring and is not softened
    here.

    Only files that exist are read; a repository without ``uv.lock`` or the
    directories yields an empty set. Numeric tokens are normalised exactly as
    :func:`extract_tokens` normalises them.
    """
    candidates: list[Path] = [root / name for name in DERIVED_NUMBER_FILES]
    for directory in DERIVED_NUMBER_DIRS:
        base = root / directory
        if base.is_dir():
            candidates.extend(sorted(path for path in base.rglob("*") if path.is_file()))

    numbers: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        numbers.update(token for token in extract_tokens(text) if token.isdigit())
    return numbers


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

    The file is one token per line, optionally followed by ``#`` and a reason on the
    same line; ``#`` comments the rest of the line. Blank lines are ignored. A
    missing file means an empty baseline, so every intersection fails. Tokens are
    lowercased on read to match the index.
    """
    path = root / BASELINE_FILENAME
    if not path.exists():
        return set()
    baseline: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.split("#", 1)[0].strip()
        if not token:
            continue
        baseline.add(token.lower())
    return baseline


def _is_checked(token: str) -> bool:
    """Return whether ``token`` is checked at all.

    Numeric tokens below :data:`MIN_NUMERIC_TOKEN_LENGTH` digits are not checked:
    the numeric rule is deliberately a six-digit delta check, and flagging every
    one- or two-digit number would drown the signal.
    """
    return not (token.isdigit() and len(token) < MIN_NUMERIC_TOKEN_LENGTH)


def _first_line_numbers(text: str) -> dict[str, int]:
    """Return, for every token :func:`extract_tokens` finds in ``text``, its first line.

    Lines are 1-based. A token that only matches when characters are joined
    across a line break (a wrapped word or number) is absent from the result;
    :func:`scan` reports ``0`` for it.
    """
    first: dict[str, int] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        for token in extract_tokens(line):
            first.setdefault(token, line_number)
    return first


def scan(texts: Mapping[str, str], index: TokenIndex, baseline: set[str]) -> list[Finding]:
    """Return every non-baseline statement token found in ``texts``.

    ``texts`` maps a repository-relative file name to its content. A token found
    in several files yields one finding per file; a token found twice in one file
    yields one finding, at the line of its first occurrence. Numeric tokens
    shorter than :data:`MIN_NUMERIC_TOKEN_LENGTH` are ignored; ``baseline`` is
    expected to already include the derived legitimate numbers.
    """
    findings: list[Finding] = []
    for file, text in texts.items():
        first_lines = _first_line_numbers(text)
        for token in extract_tokens(text):
            if not _is_checked(token):
                continue
            if token in baseline or token not in index.token_to_files:
                continue
            findings.append(
                Finding(token, file, first_lines.get(token, 0), tuple(index.token_to_files[token]))
            )
    return sorted(findings, key=lambda finding: (finding.file, finding.token))


def _resolve_root() -> tuple[Path | None, str]:
    """Return the ledger root, or ``(None, reason)`` when the ledger is unset.

    **Unset** means ``EXPENSUCHIS_LEDGER_DIR`` is not set: a fresh clone or CI, and
    the commit proceeds. **Set but unusable** -- the path does not exist, is a file,
    lies inside a repository, or is otherwise rejected by :func:`ledger_dir` --
    raises :class:`StatementsUnreadable`, so the guard fails closed. A typo in the
    variable must not silently disable a safety control.
    """
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return None, f"{ENV_VAR} is not set"
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
    repo_root: Path | None = None,
) -> GuardResult:
    """Run the guard over ``texts`` and return a printable result.

    ``ledger_root`` overrides environment resolution, which the tests use. When it
    is ``None``, the ledger is resolved from ``EXPENSUCHIS_LEDGER_DIR`` under the
    fail-closed contract described in the module docstring.

    ``repo_root`` is the directory the derived legitimate-number set is computed
    from. When it is ``None`` the derived set is empty, which is what the synthetic
    unit tests use; the command line always passes the repository root so the
    derived numeric rule is active in production.
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
    derived = derived_legitimate_numbers(repo_root) if repo_root is not None else set()
    findings = scan(texts, index, baseline | derived)
    if findings:
        return GuardResult(
            GuardStatus.LEAK,
            f"leakguard refused: {len(findings)} statement-derived token(s) found in "
            f"repository content. Remove them, or add a genuinely generic token to "
            f"{root / BASELINE_FILENAME} deliberately, with a reason.",
            findings=findings,
            tokens_indexed=len(index.token_to_files),
        )
    checkable_derived = sum(1 for number in derived if _is_checked(number))
    below_floor = len(derived) - checkable_derived
    return GuardResult(
        GuardStatus.CLEAN,
        f"leakguard: clean ({len(index.token_to_files)} statement tokens indexed, "
        f"{len(baseline)} baselined, {checkable_derived} derived legitimate numbers are "
        f"checkable (six digits or more) of {len(derived)} derived in total "
        f"({below_floor} sit below the floor and can never be flagged); "
        f"no intersection with the scanned content).",
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
    """Return the staged content of every file a commit would add or change.

    ``--diff-filter=ACMR`` lists only added, copied, modified and renamed paths.
    A token committed while the guard could not run is therefore **not**
    re-examined by a later commit; ``--tree`` does re-examine everything tracked,
    which is why the recommended workflow includes a periodic ``--tree``.
    """
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


def _token_shape(token: str) -> str:
    """Return a coarse, non-identifying description of ``token``'s shape.

    The guard has no notion of what a token *means* (it does not know, for
    example, that an eleven-digit run is typically a CUIT/CUIL), so the shape it
    can honestly report is length and character class -- exactly what
    :func:`extract_tokens` already distinguishes.
    """
    if token.isdigit():
        return f"{len(token)}-digit number"
    return f"{len(token)}-letter word"


def format_finding(finding: Finding, *, reveal: bool) -> str:
    """Render one finding, redacted by default (T-01d).

    Redacted (``reveal=False``, the default): the token's shape, a short content
    hash of the token, and the repository file and line -- never the raw token,
    never the statement path. ``reveal=True`` restores the raw token and the
    statement file(s) it matched, meant for the owner at a terminal only.
    """
    location = (
        f"{finding.file}:{finding.line}" if finding.line else f"{finding.file} (line unknown)"
    )
    if reveal:
        statements = ", ".join(finding.statements)
        return f"  token {finding.token!r} in {location} (statement: {statements})"
    shape = _token_shape(finding.token)
    digest = content_hash(finding.token.encode("utf-8"))
    return f"  {shape} {digest} in {location}"


def format_result(result: GuardResult, *, reveal: bool = False) -> str:
    """Render a full guard result as :func:`main` prints it.

    One line per finding, via :func:`format_finding`; redacted unless
    ``reveal=True``.
    """
    lines = [result.message]
    lines.extend(format_finding(finding, reveal=reveal) for finding in result.findings)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the guard for the pre-commit / commit-msg hooks or by hand.

    Default scans the staged content of the commit being made; ``--tree`` scans
    every tracked file instead. ``--message-file`` folds a commit message into the
    scanned text, which is how the ``commit-msg`` hook checks a message that the
    pre-commit hook never sees. ``--rebuild-index`` ignores and rewrites the cache.
    ``--reveal`` prints the raw token and the statement path a finding matched,
    instead of the default redacted shape/hash/location (T-01d); neither hook
    invocation passes it, so both stay redacted.
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
        "--message-file",
        metavar="PATH",
        default=None,
        help="also scan this commit-message file (git passes it to the commit-msg hook)",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="ignore and rewrite the statement token index cache",
    )
    parser.add_argument(
        "--reveal",
        action="store_true",
        help=(
            "show the raw token and the statement file(s) it matched, instead of the "
            "default redacted shape, hash and location; for the owner at a terminal "
            "only -- never pass this where the output is read by an agent or a remote model"
        ),
    )
    args = parser.parse_args(argv)

    root = repository_root()
    try:
        texts = tracked_texts(root) if args.tree else staged_texts(root)
    except LeakguardError as exc:
        print(f"leakguard refused: {exc}", file=sys.stderr)
        return EXIT_CODES[GuardStatus.UNREADABLE.value]

    if args.message_file is not None:
        message_path = Path(args.message_file)
        try:
            message = message_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(
                f"leakguard refused: cannot read commit message {message_path}: {exc}",
                file=sys.stderr,
            )
            return EXIT_CODES[GuardStatus.UNREADABLE.value]
        texts = {**texts, f"commit message ({message_path})": message}

    result = evaluate(texts, force_rebuild=args.rebuild_index, repo_root=root)
    print(format_result(result, reveal=args.reveal))
    return EXIT_CODES[result.status.value]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a crash must be reported distinctly
        print(f"leakguard crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CODES["crash"]) from None
