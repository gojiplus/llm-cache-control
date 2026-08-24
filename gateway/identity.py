"""Cache identity: what must match for state to be reusable.

Two rules here are easy to violate by accident and impossible to express in
a schema:

1. Namespace comparison is exact.  No Unicode normalization.  "café"
   and "café" render identically and MUST NOT match.
2. Approximate state must never be addressable in the exact namespace, so
   exactness is part of the key rather than a flag on the value.
"""

import hashlib
import unicodedata
from typing import Optional, Sequence

# Stand-in for the execution properties a real service owns: model revision,
# tokenizer, adapters, rendering, positional encoding, and so on.  The draft
# makes the inference service responsible for this; the gateway only adds
# the application namespace on top.
EXECUTION_IDENTITY_FIELDS = ("model", "tokenizer_rev")


def namespaces_equal(a: str, b: str) -> bool:
    """Exact comparison, no normalization.

    Written as an explicit codepoint comparison rather than `a == b` so the
    intent survives someone later "helpfully" adding a normalize step.
    """
    return list(a) == list(b)


def is_normalization_collision(a: str, b: str) -> bool:
    """True when two distinct namespaces would collide under normalization.

    Used by the conformance suite to prove the gateway does not normalize.
    """
    if namespaces_equal(a, b):
        return False
    return unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)


def namespace_identity(namespaces: Sequence[str]) -> Optional[str]:
    """The effective namespace digest, per the draft's Conservative
    Composition section.

    Specified there byte for byte rather than left to local choice: for each
    namespace in fragment order, its UTF-8 length in octets as decimal
    ASCII, a colon, then the encoded bytes; SHA-256 of the whole, lowercase
    hex.  Duplicates are kept and order is preserved.

    This function is a spelling of that rule, not a design decision.  Two
    implementations that pick their own length encodings produce different
    digests and cannot share a cache entry, which is what happened before
    the construction was written down.
    """
    present = [n for n in namespaces if n is not None]
    if not present:
        return None
    h = hashlib.sha256()
    for value in present:
        raw = value.encode("utf-8")
        h.update(f"{len(raw)}:".encode("ascii"))
        h.update(raw)
    return h.hexdigest()


def cache_key(
    execution: dict,
    contents: Sequence[str],
    namespaces: Sequence[str],
    partition: str,
    exact: bool,
) -> str:
    """Key for the prefix ending at the last supplied fragment.

    `partition` is the authenticated sharing boundary, never a caller-
    supplied string.  `exact` is part of the key, so approximate state
    cannot be found by an exact lookup even if everything else matches.
    """
    h = hashlib.sha256()
    for field in EXECUTION_IDENTITY_FIELDS:
        h.update(f"{field}={execution[field]}\x00".encode("utf-8"))
    h.update(f"partition={partition}\x00".encode("utf-8"))
    h.update(f"exact={exact}\x00".encode("utf-8"))
    identity = namespace_identity(namespaces)
    h.update(f"ns={identity}\x00".encode("utf-8"))
    for content in contents:
        raw = content.encode("utf-8")
        h.update(f"{len(raw)}:".encode("ascii"))
        h.update(raw)
    return h.hexdigest()
