"""In-memory stand-in for the physical cache.

This is deliberately not a real KV store.  Every requirement the gateway
must enforce is decided before any token is computed, so a dict with the
right key discipline exercises the policy exactly as a GPU-backed cache
would, and does it deterministically.

The seam for a real engine is small: replace `get` and `put` with calls
into the engine's block API, keeping the key and the eligibility rules.
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class Entry:
    key: str
    materialized_at: float
    expires_at: Optional[float]
    exact: bool


class InMemoryCache:
    """Cache whose eligibility rules mirror the draft's retention section."""

    def __init__(self):
        self._entries: Dict[str, Entry] = {}

    def get(self, key: str, now: float) -> Optional[Entry]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        # "After max_age, the state MUST NOT be reused."  A read does not
        # refresh: expires_at is fixed at materialization and never touched
        # here, which is the whole point of the requirement.
        if entry.expires_at is not None and now >= entry.expires_at:
            del self._entries[key]
            return None
        return entry

    def put(self, key: str, now: float, max_age: Optional[int], exact: bool) -> Entry:
        entry = Entry(
            key=key,
            materialized_at=now,
            expires_at=None if max_age is None else now + max_age,
            exact=exact,
        )
        self._entries[key] = entry
        return entry

    def __len__(self) -> int:
        return len(self._entries)

    def keys(self):
        return set(self._entries)
