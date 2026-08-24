"""Measure cross-tenant cache isolation against a live vLLM server.

Sends byte-identical prompts as two different authenticated tenants, once
with the salt the gateway derives and once without any salt, and reports how
many tokens of cached work crossed the boundary in each case.

    VLLM_BASE_URL=http://127.0.0.1:8000 python -m gateway.demo_vllm_isolation
"""

import os
import sys
import uuid

from gateway import AuthContext
from gateway.auth import partition_for
from gateway.identity import namespace_identity
from gateway.vllm_backend import VLLMBackend

ACME = AuthContext(tenant="acme", principal="alice", session="s1")
OTHER = AuthContext(tenant="other-corp", principal="bob", session="s2")


def prompt(nonce: str) -> list:
    body = " ".join(f"sentence {i} about caching and reuse in inference." for i in range(60))
    return [
        {"role": "system", "content": f"Test assistant. Run {nonce}. {body}"},
        {"role": "user", "content": "Reply with the single word ok."},
    ]


def measure(backend, salt_a, salt_b) -> int:
    """Tenant A warms the cache, tenant B sends identical tokens.

    Returns the tokens of cached work tenant B reused.
    """
    messages = prompt(uuid.uuid4().hex)
    backend.chat(messages, salt=salt_a)
    before = backend.prefix_cache_stats()[1]
    backend.chat(messages, salt=salt_b)
    return backend.prefix_cache_stats()[1] - before


def main() -> int:
    base_url = os.environ.get("VLLM_BASE_URL")
    if not base_url:
        print("VLLM_BASE_URL is not set. Nothing was measured.")
        return 1
    model = os.environ.get("VLLM_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
    backend = VLLMBackend(base_url, model)
    if not backend.is_reachable():
        print(f"{base_url} is not reachable. Nothing was measured.")
        return 1

    ns = namespace_identity(["agent:v1"])
    salt_acme = backend.salt_for(partition_for("tenant", ACME, "r1"), ns)
    salt_other = backend.salt_for(partition_for("tenant", OTHER, "r2"), ns)

    print(f"server   {base_url}")
    print(f"model    {model}\n")
    print("Two authenticated tenants send byte-identical prompts.")
    print("The number is the tokens of cached work the second tenant reused.\n")

    leaked = measure(backend, None, None)
    print(f"  no cache_salt sent                      {leaked:>6} tokens reused")
    isolated = measure(backend, salt_acme, salt_other)
    print(f"  gateway-derived salt per tenant         {isolated:>6} tokens reused")
    same = measure(backend, salt_acme, salt_acme)
    print(f"  same tenant twice (control)             {same:>6} tokens reused\n")

    print(f"  salt for tenant acme        {salt_acme}")
    print(f"  salt for tenant other-corp  {salt_other}")
    print("\n  Neither salt contains a tenant name; the engine only compares them.")

    ok = leaked > 0 and isolated == 0 and same > 0
    print(f"\n  isolation holds: {ok}")
    if not ok:
        print("  (leaked must be > 0 or the control proves nothing; isolated must be 0)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
