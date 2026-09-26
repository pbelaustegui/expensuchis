"""Tests for the shared redaction primitives (T-01d, and its keyed-hash follow-up).

``content_hash`` is the one hash helper the leak guard's default finding output
and the pipeline's redacted refusals both use, so a name can be located without
being reproduced. It is HMAC-keyed with a local secret (:func:`load_or_create_key`)
that lives only in the ledger directory, never in this repository, so that a
reader of the redacted output alone cannot brute-force the token the way they
could against the original unsalted ``sha256`` prefix.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from expensuchis.redact import (
    HASH_LENGTH,
    KEY_LENGTH,
    RedactionKeyError,
    content_hash,
    load_or_create_key,
)


def test_content_hash_is_deterministic_for_the_same_input_and_key() -> None:
    key = b"k" * KEY_LENGTH
    first = content_hash(b"some statement bytes", key)
    second = content_hash(b"some statement bytes", key)

    assert first == second
    assert len(first) == HASH_LENGTH


def test_content_hash_is_input_sensitive() -> None:
    key = b"k" * KEY_LENGTH
    assert content_hash(b"alpha", key) != content_hash(b"beta", key)


def test_content_hash_is_key_sensitive() -> None:
    data = b"a statement token"
    first = content_hash(data, b"k" * KEY_LENGTH)
    second = content_hash(data, b"j" * KEY_LENGTH)

    assert first != second


def test_content_hash_is_not_the_plain_sha256_prefix() -> None:
    data = b"a statement token"
    key = b"k" * KEY_LENGTH
    unsalted = hashlib.sha256(data).hexdigest()[:HASH_LENGTH]

    assert content_hash(data, key) != unsalted


def test_load_or_create_key_creates_a_key_with_0600_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / "redaction.key"

    key = load_or_create_key(key_path)

    assert len(key) == KEY_LENGTH
    assert key_path.is_file()
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600


def test_load_or_create_key_is_reused_across_calls(tmp_path: Path) -> None:
    key_path = tmp_path / "redaction.key"

    first = load_or_create_key(key_path)
    second = load_or_create_key(key_path)

    assert first == second


def test_a_corrupt_key_file_raises_rather_than_being_regenerated(tmp_path: Path) -> None:
    key_path = tmp_path / "redaction.key"
    key_path.write_bytes(b"too-short")

    with pytest.raises(RedactionKeyError):
        load_or_create_key(key_path)

    # The corrupt bytes are untouched: a second attempt raises the same way,
    # proving nothing silently overwrote them.
    with pytest.raises(RedactionKeyError):
        load_or_create_key(key_path)
    assert key_path.read_bytes() == b"too-short"
