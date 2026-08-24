"""Cache backends, and the lowering rule that governs all of them.

The draft's appendix states the asymmetry that makes this contract safe:

    "An engine hint may be ignored under pressure, but an application
    constraint may not be weakened.  When a backend cannot enforce a
    constraint, the gateway must bypass caching or reject the cache intent
    rather than translate the constraint into a soft hint."

A gateway with one all-capable backend cannot demonstrate that rule, because
nothing is ever unenforceable.  So there are three backends here with
deliberately different capabilities, and the gateway asks each what it can
enforce before it decides anything.

`InMemoryBackend`  full capability, the reference.
`SaltOnlyBackend`  models an engine offering only prefix-cache salting, the
                   shape vLLM's cache_salt provides.  It can partition, so
                   it can enforce share_max and namespace.  It has no
                   lifetime control and no approximate reuse, so max_age
                   and reuse: approximate are unenforceable and must bypass.
`NullBackend`      caches nothing.  This is the constraint-preserving
                   fallback the draft says always exists, so it must satisfy
                   every constraint by construction.
"""

from typing import Dict, Optional, Protocol, Set

from .cache import Entry, InMemoryCache


class CacheBackend(Protocol):
    """What a gateway needs from a cache to decide policy.

    `capabilities` is the load-bearing member.  Everything the gateway
    refuses to downgrade, it refuses on the strength of this.
    """

    name: str

    def capabilities(self) -> Dict: ...

    def get(self, key: str, now: float) -> Optional[Entry]: ...

    def put(self, key: str, now: float, max_age: Optional[int], exact: bool) -> Optional[Entry]: ...


class InMemoryBackend:
    """Reference backend. Enforces every constraint version 1 defines."""

    name = "in-memory"
    observes_outcomes = True

    def __init__(self):
        self._cache = InMemoryCache()

    def capabilities(self) -> Dict:
        return {
            "retention_modes": {"allow", "forbid"},
            "retention_max_age": True,
            "reuse": {"none", "exact", "approximate"},
            "share_max": {"request", "session", "principal", "tenant", "public"},
            "namespace": True,
            # Advertising a hint obliges the gateway to report that hint's
            # status, which is what makes support observable at all.  The
            # maximum is what makes reuse_within's *value* observable: above
            # it the caller is told `clipped`, at or below it is not.
            "hints": {"reuse_within_max": 3600},
            "per_fragment_share": True,
        }

    def get(self, key, now):
        return self._cache.get(key, now)

    def put(self, key, now, max_age, exact):
        return self._cache.put(key, now, max_age, exact)

    def keys(self) -> Set[str]:
        return self._cache.keys()


class SaltOnlyBackend:
    """An engine that can partition a prefix cache but not time-bound it.

    This is the shape of vLLM's cache_salt: a value folded into the block
    hash so only requests sharing it can match.  Partitioning is enough for
    share_max and namespace.  It gives no way to expire an entry at a
    deadline and no approximate matching, so a request asking for either
    must bypass rather than be quietly served under weaker terms.
    """

    name = "salt-only"
    observes_outcomes = True

    def __init__(self):
        self._cache = InMemoryCache()

    def capabilities(self) -> Dict:
        return {
            "retention_modes": {"allow", "forbid"},
            "retention_max_age": False,
            "reuse": {"none", "exact"},
            "share_max": {"request", "session", "principal", "tenant", "public"},
            "namespace": True,
            "hints": {},
            # One cache_salt is folded into the first block and every later
            # block chains off it, so a request carries exactly one boundary.
            "per_fragment_share": False,
        }

    def salt_for(self, partition: str, namespace_identity: Optional[str]) -> str:
        """Lower an authenticated partition and namespace onto one salt.

        This is the concrete lowering a real vLLM binding performs: the
        gateway hands the engine an opaque salt, and the engine never sees
        the tenant, the principal, or the application namespace.
        """
        return f"{partition}|{namespace_identity or '-'}"

    def get(self, key, now):
        return self._cache.get(key, now)

    def put(self, key, now, max_age, exact):
        if max_age is not None:
            raise AssertionError(
                "gateway must not hand max_age to a backend that cannot enforce it"
            )
        return self._cache.put(key, now, None, exact)

    def keys(self) -> Set[str]:
        return self._cache.keys()


class NullBackend:
    """Fresh computation, no cross-request retention.

    "Fresh computation with no cross-request retention satisfies every
    constraint in this document."  So this backend is always safe and always
    misses, which makes it the fallback of last resort.
    """

    name = "null"
    observes_outcomes = True

    def capabilities(self) -> Dict:
        return {
            "retention_modes": {"allow", "forbid"},
            "retention_max_age": True,
            "reuse": {"none", "exact", "approximate"},
            "share_max": {"request", "session", "principal", "tenant", "public"},
            "namespace": True,
            "hints": {},
            "per_fragment_share": False,
        }

    def get(self, key, now):
        return None

    def put(self, key, now, max_age, exact):
        return None

    def keys(self) -> Set[str]:
        return set()
