"""The shared redaction primitive: a short, stable content hash.

The leak guard's default finding output (:mod:`expensuchis.leakguard`) and the
import pipeline's refusal messages (:mod:`expensuchis.pipeline`) both need to name
something -- a statement token, a statement file -- without reproducing it, because
their output can reach a remote model. Both use this exact helper and length
(T-01d), so a hash printed by one is computed the same way as a hash printed by
the other.
"""

from __future__ import annotations

import hashlib

__all__ = ["HASH_LENGTH", "content_hash"]

#: Length, in hex characters, of the short content hash used wherever a
#: diagnostic must name something without reproducing it.
HASH_LENGTH = 12


def content_hash(data: bytes) -> str:
    """Return the first :data:`HASH_LENGTH` hex characters of ``sha256(data)``.

    Deterministic and short: the same bytes always hash to the same value, and
    the truncation is long enough to tell unrelated inputs apart in a report
    without being able to recover them.
    """
    return hashlib.sha256(data).hexdigest()[:HASH_LENGTH]
