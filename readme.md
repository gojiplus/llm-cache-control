# LLM Cache Control

Applications know things about cached LLM state that the inference service cannot infer, and there is no portable way to tell it. This repository proposes one: a small JSON object attached to an input fragment, carrying constraints the service must not weaken and hints it may ignore.

An application may know that a prefix can be shared inside a tenant but not across tenants, that reuse must be exact, that a fragment must not be retained past the request, that a branch will probably resume in thirty seconds, that a miss on one prefix costs far more than a miss on another, or that an application-level version bump should force a miss even though the bytes are identical.

The inference service knows a different set of facts: physical cache state, memory pressure, placement, access history, transfer cost, and scheduling constraints. Neither side can do the other's job, so the contract splits the information rather than the mechanism.

The longer argument is in the essay [Cache-Control for LLMs](https://www.gojiberries.io/cache-control-for-llms/). The normative version is [`draft-sood-llm-cache-control-00.md`](draft-sood-llm-cache-control-00.md).

## What the application supplies

Constraints are requirements an implementation must not silently weaken. There are four.

`retention` says whether state derived from the fragment may survive the request, and for how long.
`reuse` says `none`, `exact`, or `approximate`. Exact is the safe default and approximation is always opt-in.
`share_max` sets the widest authorized reuse boundary, from `request` through `session`, `principal`, and `tenant` to `public`.
`namespace` adds an application-defined string to cache identity. It can force a miss, but it can never make different input match, and it cannot widen `share_max`.

If the service cannot enforce a constraint, it must bypass caching or reject the intent. Fresh computation with no cross-request retention satisfies every constraint in the document, so a conforming fallback always exists.

Hints are information the service may use or ignore. `reuse_within` is the number of seconds within which the application expects the state to be reused. `priority` is a 0 to 100 statement of how much avoiding this particular miss is worth.

The service keeps everything else: execution identity, admission, eviction, physical placement, compression, prefetch implementation, scheduling, and resource-pressure decisions.

## Constraints compose downward

KV state is cumulative, so the constraints on it are cumulative too. State computed from several fragments carries the most restrictive combination of their constraints: `forbid` retention wins over `allow`, the smallest `max_age` wins, `none` beats `exact` beats `approximate`, sharing boundaries intersect, and every contributing namespace must appear in the effective cache identity.

A later fragment cannot relax what it inherited. Put a public tool schema after a tenant-scoped system prompt and the downstream state is still tenant-scoped, because it depends on both. Put the schema first and the schema-only prefix can be shared publicly while everything after it narrows to the tenant. Order matters, which is also why a gateway must not reorder model-visible fragments to improve hit rates.

The [test vectors](llm-cache-control-test-vectors.json) encode these cases.

## Example

```json
{
  "cache_intent": {
    "version": 1,
    "constraints": {
      "retention": {
        "mode": "allow",
        "max_age": 3600
      },
      "reuse": "exact",
      "share_max": "tenant",
      "namespace": "support-agent-v7"
    },
    "hints": {
      "reuse_within": 300,
      "priority": 80
    }
  }
}
```

That intent says the state may be retained for at most an hour, approximate reuse is not allowed, reuse may not cross the tenant boundary, `support-agent-v7` participates in cache identity, another use is expected within five minutes, and avoiding the miss is worth a good deal. It does not say to pin anything, to use LRU, or to put KV on a GPU. The service decides how to implement it. Requests validate against [`llm-cache-control.schema.json`](llm-cache-control.schema.json).

## Why another cache API

Pieces of this exist already. Anthropic exposes [cache breakpoints and two TTLs](https://docs.claude.com/en/docs/build-with-claude/prompt-caching), OpenAI caches prefixes [automatically with an optional routing key](https://platform.openai.com/docs/guides/prompt-caching), and Google exposes [named cache objects with an explicit TTL](https://ai.google.dev/gemini-api/docs/caching). vLLM has [cache salting](https://github.com/vllm-project/vllm/issues/16016) for prefix isolation, plus open RFCs for [retention priority](https://github.com/vllm-project/vllm/issues/37003) and [session-scoped cache coordinates](https://github.com/vllm-project/vllm/issues/48501). [LMCache](https://github.com/LMCache/LMCache) exposes lower-level cache-management operations.

Each of those is a mechanism bound to one provider or one engine. This proposal sits a layer above them: an application-facing contract that a trusted gateway can authenticate, compose across fragments, and lower into whichever mechanism the backend actually has.

```text
application intent
       |
       v
trusted gateway / control plane
       |
       v
engine lifecycle hints
       |
       v
physical KV cache
```

The lowering is deliberately asymmetric. A soft hint may become an engine hint or be dropped under pressure, but a hard application constraint must stay hard. When a backend cannot enforce one, the gateway bypasses caching or rejects the intent instead of quietly demoting the constraint to a hint.

The status object is modelled on the HTTP [`Cache-Status`](https://www.rfc-editor.org/rfc/rfc9211.html) field in purpose, though not in syntax, and the constraint-versus-hint split follows the same instinct as [`Cache-Control`](https://www.rfc-editor.org/rfc/rfc9111.html).

## Scope

The initial proposal covers cached input computation, mainly KV state. It does not try to standardize semantic answer caches, eviction algorithms, GPU, CPU, or disk placement, cache compression, context editing, approximate-reuse quality metrics, or provider billing. Those can be separate mechanisms or later extensions.

## What is in the repository

[`draft-sood-llm-cache-control-00.md`](draft-sood-llm-cache-control-00.md) is the Internet-Draft source, currently Experimental.
[`llm-cache-control.schema.json`](llm-cache-control.schema.json) is the JSON Schema for the `cache_intent` object.
[`llm-cache-control-test-vectors.json`](llm-cache-control-test-vectors.json) holds the conformance examples, including the composition cases above.

The wire object is named `cache_intent` because the constraints are what the application intends, not what the cache does. The repository is named for the analogy that motivates it.

## What would count as progress

1. Agree on the smallest useful set of constraints and hints.
2. Define one concrete API binding.
3. Build a reference gateway over an existing inference stack.
4. Map the intent onto vLLM and LMCache mechanisms where possible.
5. Compare engine-only cache policy against application-provided intent.
6. Get a second independent implementation.
7. Revise the draft from what those implementations teach.

The point is not standardization by decree. It is to make the contract precise enough to implement twice, test for interoperability, and find out where the abstraction is wrong.
