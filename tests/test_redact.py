"""Tests for the shared redaction primitives (T-01d).

``content_hash`` is the one hash helper the leak guard's default finding output
and the pipeline's redacted refusals both use, so a name can be located without
being reproduced.
"""

from __future__ import annotations

import hashlib

from expensuchis.redact import HASH_LENGTH, content_hash


def test_content_hash_is_the_truncated_sha256_hex_digest() -> None:
    data = b"some statement bytes"
    expected = hashlib.sha256(data).hexdigest()[:HASH_LENGTH]

    digest = content_hash(data)

    assert digest == expected
    assert len(digest) == HASH_LENGTH


def test_content_hash_is_deterministic_and_input_sensitive() -> None:
    first = content_hash(b"alpha")
    second = content_hash(b"alpha")
    third = content_hash(b"beta")

    assert first == second
    assert first != third
