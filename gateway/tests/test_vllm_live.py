"""Live integration tests against a running vLLM server.

Skipped unless VLLM_BASE_URL is set, and the skip is loud on purpose: a
suite that silently passes because it tested nothing is worse than one that
fails.

Run with:
    VLLM_BASE_URL=http://127.0.0.1:8000 VLLM_MODEL=<model> \
        python -m pytest gateway/tests/test_vllm_live.py -v

These tests answer one question the rest of the repository cannot: does
vLLM actually honor cache_salt as an isolation boundary, or does it merely
accept the field?  Everything else about the binding is reading docs.
"""

import json
import os
import uuid

import pytest
from gateway import AuthContext, Gateway
from gateway.auth import partition_for
from gateway.binding_openai import OpenAIChatBinding
from gateway.identity import namespace_identity
from gateway.vllm_backend import VLLMBackend

BASE_URL = os.environ.get("VLLM_BASE_URL")
MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
ACME = AuthContext(tenant="acme", principal="alice", session="s1")

pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="VLLM_BASE_URL not set; live vLLM binding is UNVERIFIED in this run",
)


@pytest.fixture(scope="module")
def backend():
    b = VLLMBackend(BASE_URL, MODEL)
    if not b.is_reachable():
        pytest.fail(f"VLLM_BASE_URL={BASE_URL} was set but the server is not reachable")
    return b


# Unique per process run.  Without it these tests are not hermetic: the
# tenant test derives its salts from fixed tenant names, so a second run of
# the suite would find the second tenant's blocks already warm from the
# first run and report a cross-tenant leak that did not happen.
RUN_NONCE = uuid.uuid4().hex


def long_prompt(tag: str) -> list:
    """A prompt long enough to span several KV blocks, cold on every run.

    Prefix caching works on full blocks (16 tokens by default), so a short
    prompt can produce zero cacheable blocks and make every measurement
    read as a miss regardless of the salt.
    """
    body = " ".join(
        f"sentence {i} about caching and reuse in inference systems." for i in range(60)
    )
    return [
        {
            "role": "system",
            "content": f"You are a test assistant. Run {RUN_NONCE}. Corpus {tag}. {body}",
        },
        {"role": "user", "content": "Reply with the single word ok."},
    ]


def test_server_accepts_cache_salt(backend):
    """The field is accepted, not rejected as unknown."""
    response = backend.chat(long_prompt("accept"), salt="a" * 16)
    assert "choices" in response


def test_same_salt_reuses_blocks(backend):
    """Baseline: identical prompt and salt must produce cache hits.

    Without this, the isolation test below would pass trivially on a server
    that never caches anything.
    """
    salt = uuid.uuid4().hex
    prompt = long_prompt("reuse")
    backend.chat(prompt, salt=salt)
    before_q, before_h = backend.prefix_cache_stats()
    backend.chat(prompt, salt=salt)
    after_q, after_h = backend.prefix_cache_stats()

    assert after_q > before_q, "the second request did not query the prefix cache at all"
    assert after_h > before_h, (
        "identical prompt and salt produced no cache hits, so this server is not "
        "reusing blocks and the isolation result below would be meaningless"
    )


def test_different_salt_isolates_identical_prompt(backend):
    """The claim that matters: same tokens, different salt, no reuse."""
    prompt = long_prompt("isolate")
    backend.chat(prompt, salt=uuid.uuid4().hex)
    before_q, before_h = backend.prefix_cache_stats()
    backend.chat(prompt, salt=uuid.uuid4().hex)
    after_q, after_h = backend.prefix_cache_stats()

    assert after_q > before_q, "the second request did not query the prefix cache"
    assert after_h == before_h, (
        f"byte-identical prompts under different salts produced {after_h - before_h} "
        "cache hits; cache_salt is not isolating and share_max cannot be lowered onto it"
    )


def test_gateway_salts_differ_across_tenants(backend):
    """The salts the gateway derives are distinct per authenticated tenant."""
    acme = AuthContext(tenant="acme", principal="alice", session="s1")
    other = AuthContext(tenant="other-corp", principal="bob", session="s2")
    ns = namespace_identity(["agent:v1"])

    salt_acme = backend.salt_for(partition_for("tenant", acme, "r1"), ns)
    salt_other = backend.salt_for(partition_for("tenant", other, "r2"), ns)
    assert salt_acme != salt_other

    prompt = long_prompt("tenants")
    backend.chat(prompt, salt=salt_acme)
    before_q, before_h = backend.prefix_cache_stats()
    backend.chat(prompt, salt=salt_other)
    after_q, after_h = backend.prefix_cache_stats()

    assert after_q > before_q
    assert after_h == before_h, (
        "one tenant reused another tenant's blocks for identical input; "
        "this is the cross-tenant leak share_max exists to prevent"
    )


def test_salt_does_not_leak_the_tenant_name(backend):
    """The engine must not receive a customer identifier in the clear."""
    acme = AuthContext(tenant="acme-corporation", principal="alice", session="s1")
    salt = backend.salt_for(partition_for("tenant", acme, "r1"), None)
    assert "acme" not in salt
    assert len(salt) == 64


def test_binding_namespace_change_isolates_identical_model_input(backend):
    """Exercise namespace isolation through the binding, not salt_for directly."""
    binding = OpenAIChatBinding(Gateway(backend=backend))

    def prepare(namespace, request_id):
        request = {"model": MODEL, "messages": long_prompt("binding-namespace")}
        request["messages"][0]["cache_intent"] = {
            "version": 1,
            "constraints": {
                "retention": {"mode": "allow"},
                "reuse": "exact",
                "share_max": "tenant",
                "namespace": namespace,
            },
        }
        upstream, result, error = binding.prepare(json.dumps(request), ACME, request_id)
        assert error is None
        return upstream["messages"], result.cache_salt

    messages_a, salt_a = prepare("agent:v1", "r1")
    messages_b, salt_b = prepare("agent:v2", "r2")
    assert messages_a == messages_b
    assert salt_a != salt_b

    backend.chat(messages_a, salt=salt_a)
    before_q, before_h = backend.prefix_cache_stats()
    backend.chat(messages_b, salt=salt_b)
    after_q, after_h = backend.prefix_cache_stats()
    assert after_q > before_q
    assert after_h == before_h


def test_binding_reuse_none_uses_a_fresh_partition(backend):
    """Two reuse:none requests must not share vLLM prefix state."""
    binding = OpenAIChatBinding(Gateway(backend=backend))
    request = {"model": MODEL, "messages": long_prompt("binding-reuse-none")}
    request["messages"][0]["cache_intent"] = {
        "version": 1,
        "constraints": {
            "retention": {"mode": "allow"},
            "reuse": "none",
            "share_max": "tenant",
        },
    }
    first, result_a, error_a = binding.prepare(json.dumps(request), ACME, "r1")
    second, result_b, error_b = binding.prepare(json.dumps(request), ACME, "r2")
    assert error_a is None and error_b is None
    assert result_a.cache_salt != result_b.cache_salt

    backend.chat(first["messages"], salt=result_a.cache_salt)
    before_q, before_h = backend.prefix_cache_stats()
    backend.chat(second["messages"], salt=result_b.cache_salt)
    after_q, after_h = backend.prefix_cache_stats()
    assert after_q > before_q
    assert after_h == before_h
