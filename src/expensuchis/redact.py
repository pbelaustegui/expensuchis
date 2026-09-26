"""The shared redaction primitives: a keyed content hash and its local key.

The leak guard's default finding output (:mod:`expensuchis.leakguard`) and the
import pipeline's refusal messages (:mod:`expensuchis.pipeline`) both need to name
something -- a statement token, a statement file -- without reproducing it, because
their output can reach a remote model. Both use this exact helper and length
(T-01d), so a hash printed by one is computed the same way as a hash printed by
the other.

**What this hash promises, and what it does not (T-01d follow-up).** The first
version of this module was an unsalted, truncated ``sha256``. Printed next to a
token's exact length and character class, that let a reader brute-force the
underlying token: a numeric token (for example an eleven-digit CUIT) in minutes,
and a name by dictionary attack, because the attacker could hash every candidate
and compare. The hash is now :func:`hmac.new` keyed with a 32-byte secret that is
generated once and stored **only** in the ledger directory (never in this
repository), via :func:`load_or_create_key`. This promises:

* the same input and the same key always produce the same hash (stable across
  runs on the machine that holds the key);
* a different key produces an unrelated hash for the same input; and
* a reader of the guard's or the pipeline's output, who does not also have the
  local key file, cannot brute-force the hash the way they could the old
  unsalted one.

It does **not** promise anything once the key itself leaks: HMAC's brute-force
resistance depends entirely on the key staying secret, and this module makes no
attempt to protect the key beyond restrictive file permissions
(:func:`load_or_create_key`). It is also still a short, truncated digest, so
collisions between unrelated inputs are not cryptographically excluded -- it is a
diagnostic aid, not an integrity primitive.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

__all__ = [
    "HASH_LENGTH",
    "KEY_LENGTH",
    "REDACTION_KEY_FILENAME",
    "RedactionKeyError",
    "content_hash",
    "load_or_create_key",
]

#: Length, in hex characters, of the short content hash used wherever a
#: diagnostic must name something without reproducing it.
HASH_LENGTH = 12

#: Length, in bytes, of the local HMAC key (:func:`load_or_create_key`).
KEY_LENGTH = 32

#: Filename of the local HMAC key inside the ledger directory. Shared by
#: :mod:`expensuchis.leakguard` (which resolves it against a bare ledger root) and
#: :class:`expensuchis.paths.LedgerPaths` (``redaction_key()``), so both name the
#: same file when pointed at the same ledger directory.
REDACTION_KEY_FILENAME = "redaction.key"


class RedactionKeyError(Exception):
    """The redaction key file exists but is not a valid key.

    Raised by :func:`load_or_create_key` when the file is present but is not
    exactly :data:`KEY_LENGTH` bytes. A corrupt key is never silently regenerated:
    overwriting it would make every hash computed before the corruption
    unreproducible without any record that this happened.
    """


def content_hash(data: bytes, key: bytes) -> str:
    """Return the first :data:`HASH_LENGTH` hex characters of ``HMAC-SHA256(key, data)``.

    Deterministic for a fixed ``key``: the same bytes and the same key always hash
    to the same value, so a hash printed twice for the same input is recognisably
    the same input, and the truncation is long enough to tell unrelated inputs
    apart in a report. See the module docstring for exactly what this does and
    does not promise once the key is not also known to the reader.
    """
    return hmac.new(key, data, hashlib.sha256).hexdigest()[:HASH_LENGTH]


def _write_key_atomically(path: Path, key: bytes) -> None:
    """Write ``key`` to ``path`` atomically, with permissions ``0600``.

    Writes to a sibling temporary file created with restrictive permissions from
    the start (never a moment of world- or group-readability), then renames it
    into place with :func:`os.replace`, which is atomic on the same filesystem.
    """
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        os.chmod(temporary, 0o600)  # belt-and-braces: umask can widen os.open's mode
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def load_or_create_key(key_path: Path) -> bytes:
    """Return the local HMAC key at ``key_path``, creating it on first use.

    Created from :func:`secrets.token_bytes` (:data:`KEY_LENGTH` bytes) the first
    time this is called for a given path, and written atomically with permissions
    ``0600`` (:func:`_write_key_atomically`). Once created, the same bytes are read
    back on every later call, so hashes computed with this key stay stable across
    runs on the machine that holds it.

    Raises:
        RedactionKeyError: if the file exists but is not exactly :data:`KEY_LENGTH`
            bytes. A corrupt key is an error, never silently regenerated: doing so
            would make every hash computed before the corruption unreproducible,
            with no record that it happened.
    """
    try:
        data = key_path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(KEY_LENGTH)
        _write_key_atomically(key_path, key)
        return key
    except OSError as exc:
        raise RedactionKeyError(f"cannot read redaction key {key_path}: {exc}") from exc

    if len(data) != KEY_LENGTH:
        raise RedactionKeyError(
            f"redaction key {key_path} is {len(data)} bytes, expected {KEY_LENGTH}; "
            f"refusing to use or silently regenerate a corrupt key."
        )
    return data
