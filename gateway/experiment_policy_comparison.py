"""Engine-only cache policy versus application-provided intent.

Roadmap item 5.  The question this proposal has to answer is whether
application intent buys anything an engine could not work out for itself.

The workload is the draft's flagship shape: a large public tool schema every
tenant shares, followed by a small tenant-private system prompt.  Tenant A
sends first and warms the cache.  Tenant B sends the same public prefix and
its own private suffix.  The number reported is how many tokens of cached
work tenant B reused.

Three policies:

  no isolation      no cache_salt at all.  Maximum reuse, and the private
                    suffix leaks across tenants, so it is not deployable.
  blanket tenant    one tenant-scoped salt for the whole request.  Safe, and
                    what an engine can do without being told anything.
  application       what the draft asks for: the public prefix shared across
                    tenants, the private suffix isolated.

The gap between "blanket tenant" and "application" is the value of the
contract.  If there is no gap, the engine did not need the application.

    VLLM_BASE_URL=http://127.0.0.1:8000 python -m gateway.experiment_policy_comparison
"""

import os
import sys
import uuid

from gateway.vllm_backend import VLLMBackend

PUBLIC_BLOCKS = 80
PRIVATE_BLOCKS = 10


def public_prefix() -> str:
    return " ".join(
        f"Tool {i}: signature, arguments, and usage notes for the shared catalogue."
        for i in range(PUBLIC_BLOCKS)
    )


def private_suffix(tenant: str) -> str:
    return " ".join(f"Tenant {tenant} policy clause {i}." for i in range(PRIVATE_BLOCKS))


def messages(tenant: str, run: str, public_salt: str = None) -> list:
    public = {"role": "system", "content": f"Catalogue {run}. {public_prefix()}"}
    if public_salt is not None:
        public["cache_salt"] = public_salt
    return [
        public,
        {"role": "system", "content": private_suffix(tenant)},
        {"role": "user", "content": "Reply with the single word ok."},
    ]


def trial(backend, run, salt_a, salt_b, public_salt=None):
    """Tenant A warms, tenant B measures. Returns tokens B reused."""
    backend.chat(messages("A", run, public_salt), salt=salt_a)
    before = backend.prefix_cache_stats()[1]
    backend.chat(messages("B", run, public_salt), salt=salt_b)
    return backend.prefix_cache_stats()[1] - before


def prefix_size(backend, run) -> int:
    """Tokens of cached work an identical repeat reuses.

    This is the ceiling: the most any policy could possibly share.
    """
    salt = uuid.uuid4().hex
    backend.chat(messages("A", run), salt=salt)
    before = backend.prefix_cache_stats()[1]
    backend.chat(messages("A", run), salt=salt)
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

    print(f"server {base_url}\nmodel  {model}\n")
    ceiling = prefix_size(backend, uuid.uuid4().hex)
    print(f"Ceiling: an identical repeat reuses {ceiling} tokens.")
    print("Tenant B sends the same public prefix as tenant A, with its own private suffix.\n")

    results = {}
    results["no isolation"] = trial(backend, uuid.uuid4().hex, None, None)
    results["blanket tenant salt"] = trial(
        backend, uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    )
    shared_public = uuid.uuid4().hex
    results["application intent"] = trial(
        backend,
        uuid.uuid4().hex,
        uuid.uuid4().hex,
        uuid.uuid4().hex,
        public_salt=shared_public,
    )

    for label, tokens in results.items():
        share = f"{tokens / ceiling:.0%}" if ceiling else "n/a"
        print(f"  {label:<24} {tokens:>6} tokens reused   ({share} of ceiling)")

    print()
    gap = results["application intent"] - results["blanket tenant salt"]
    if gap > 0:
        print(f"  Application intent recovers {gap} tokens a blanket policy cannot.")
    else:
        print("  Application intent recovers nothing over a blanket tenant salt here.")
        print("  vLLM folds one cache_salt into the hash of the FIRST block, so a")
        print("  request carries a single sharing boundary. The draft's public-prefix-")
        print("  then-private-suffix pattern cannot be lowered onto it as it stands.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
