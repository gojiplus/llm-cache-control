"""Backend that lowers cache intent onto a live vLLM server.

vLLM exposes one hook for this: `cache_salt`, a top-level string on a chat
completion request that gets folded into the hash of the first KV block.
Only requests carrying the same salt can reuse each other's blocks.  That is
exactly the primitive `share_max` and `namespace` need, and it is the only
one vLLM offers, which is why this backend's capabilities are narrower than
the in-memory reference.

What vLLM gives us:
    partitioning, so share_max and namespace are enforceable.
What it does not:
    any notion of an expiry deadline, so max_age is unenforceable, and no
    approximate matching, so reuse: approximate is unenforceable.  Both must
    bypass rather than be served under weaker terms.

Standard library only.  A specification repository should not need an HTTP
client dependency to demonstrate a binding.
"""

import hashlib
import json
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

DEFAULT_TIMEOUT = 120


class VLLMBackend:
    """Lowers an authenticated partition and namespace onto a vLLM cache_salt."""

    name = "vllm"
    # vLLM 0.11 returns prompt_tokens_details as null on the OpenAI-
    # compatible endpoint, so a gateway in front of it cannot tell a hit
    # from a miss for a given request. The /metrics counters are
    # server-wide, which is enough to measure isolation in aggregate but
    # not to attribute an outcome to one request.
    observes_outcomes = False

    def __init__(self, base_url: str, model: str, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def capabilities(self) -> Dict:
        return {
            "retention_modes": {"allow", "forbid"},
            "retention_max_age": False,
            "reuse": {"none", "exact"},
            "share_max": {"request", "session", "principal", "tenant", "public"},
            "namespace": True,
            # vLLM has no lifetime or priority control reachable from here.
            "hints": {},
            # cache_salt is per request, not per message.  A per-message value
            # is accepted by the API and silently ignored.
            "per_fragment_share": False,
        }

    def salt_for(self, partition: str, namespace_identity: Optional[str]) -> str:
        """Derive the opaque salt handed to the engine.

        Digested rather than passed through.  The partition contains the
        authenticated tenant and principal, and the draft requires a gateway
        to strip or replace untrusted values before forwarding engine-level
        coordinates.  Sending `tenant:acme` to the engine in the clear would
        put a customer name in the engine's cache metadata for no benefit,
        since the engine only ever compares salts for equality.
        """
        h = hashlib.sha256()
        h.update(f"{len(partition)}:{partition}".encode("utf-8"))
        ns = namespace_identity or "-"
        h.update(f"{len(ns)}:{ns}".encode("utf-8"))
        return h.hexdigest()

    def _post(self, path: str, payload: Dict) -> Dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def chat(self, messages, salt: Optional[str] = None, max_tokens: int = 1) -> Dict:
        """Send a chat completion, optionally carrying a cache salt.

        `cache_salt` is top-level in the request body, beside `messages`.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        if salt is not None:
            payload["cache_salt"] = salt
        return self._post("/v1/chat/completions", payload)

    def prefix_cache_stats(self) -> Tuple[int, int]:
        """Read (queries, hits) from the Prometheus endpoint.

        These are token counts, not request counts.  The difference matters:
        a request that reuses nothing still registers queries, so isolation
        is proved by hits staying flat while queries climb.
        """
        with urllib.request.urlopen(f"{self.base_url}/metrics", timeout=self.timeout) as response:
            body = response.read().decode("utf-8")
        queries = hits = 0
        for line in body.splitlines():
            if line.startswith("#"):
                continue
            if line.startswith("vllm:prefix_cache_queries_total"):
                queries += int(float(line.rsplit(" ", 1)[1]))
            elif line.startswith("vllm:prefix_cache_hits_total"):
                hits += int(float(line.rsplit(" ", 1)[1]))
        return queries, hits

    def is_reachable(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/v1/models", timeout=5):
                return True
        except (urllib.error.URLError, OSError):
            return False

    # The cache-store half of the CacheBackend protocol. vLLM owns physical
    # residency, so the gateway does not track entries itself; it decides
    # what salt a request may carry and lets the engine do the rest.
    def get(self, key, now):
        return None

    def put(self, key, now, max_age, exact):
        if max_age is not None:
            raise AssertionError("vLLM cannot enforce max_age; the gateway must bypass instead")
        return None

    def keys(self):
        return set()
