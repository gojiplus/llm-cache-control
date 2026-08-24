# LLM Cache Control

A portable way for an application to tell an inference service what it may do with the
state cached from its input: four constraints the service must not weaken, and two hints it
may ignore. This repository holds the specification as an Internet-Draft, three JSON
schemas, a binding to the OpenAI Chat Completions API, a reference gateway, and a
conformance suite that runs against a live vLLM server.

It is a proposal, not a deployed standard. Nothing here is implemented outside this
repository yet, and the section on known gaps says what that leaves open.

## What an application knows that the service does not

Applications know things about cached LLM state that the inference service cannot infer,
and there is no portable way to tell it.

An application may know that a prefix can be shared inside a tenant but not across tenants,
that reuse must be exact, that a fragment must not be retained past the request, that a
branch will probably resume in thirty seconds, that a miss on one prefix costs far more
than a miss on another, or that an application-level version bump should force a miss even
though the bytes are identical.

The inference service knows a different set of facts: physical cache state, memory
pressure, placement, access history, transfer cost, and scheduling constraints. Neither
side can do the other's job, so the contract splits the information rather than the
mechanism.

The longer argument is in the essay [Cache-Control for LLMs](https://www.gojiberries.io/cache-control-for-llms/).
The normative version is [`draft-sood-llm-cache-control-01.md`](draft-sood-llm-cache-control-01.md).

## The object

A `cache_intent` object rides on one fragment of model input.

```json
{
  "cache_intent": {
    "version": 1,
    "id": "tools-v7",
    "constraints": {
      "retention": {
        "mode": "allow",
        "max_age": 3600
      },
      "reuse": "exact",
      "share_max": "tenant",
      "namespace": "support-agent:v7"
    },
    "hints": {
      "reuse_within": 300
    }
  }
}
```

That intent says the state may be retained for at most an hour, approximate reuse is not
allowed, reuse may not cross the tenant boundary, `support-agent:v7` participates in cache
identity, another use is expected within five minutes, and avoiding the miss is worth a
good deal. It does not say to pin anything, to use LRU, or to put KV on a GPU. The service
decides how to implement it.

Constraints are requirements an implementation must not silently weaken. There are four.

`retention` says whether state derived from the fragment may survive the request, and for
how long.
`reuse` says `none`, `exact`, or `approximate`. Exact is the safe default and approximation
is always opt-in.
`share_max` sets the widest authorized reuse boundary, from `request` through `session`,
`principal`, and `tenant` to `public`. Those five form a total order under containment, so
intersecting a set of them yields the narrowest member.
`namespace` adds an application-defined string to cache identity. It can force a miss, but
it can never make different input match, and it cannot widen `share_max`.

Version 1 defines one hint. `reuse_within` is the number of seconds within which the
application expects the state to be reused. An implementation that advertises it must report
whether it accepted, clipped, or ignored the value, which is what makes support observable:
a hint may be ignored, so cache behavior alone cannot tell a service that used the value from
one that discarded it.

`id` is an optional label for correlating a fragment with its status report. It does not
enter cache identity. Both `id` and `namespace` are bounded at 256 characters, which is
what keeps per-entry cache metadata bounded.

The service keeps everything else: execution identity, admission, eviction, physical
placement, compression, prefetch implementation, scheduling, and resource-pressure
decisions.

Requests validate against [`llm-cache-control.schema.json`](llm-cache-control.schema.json).

## Constraints are hard

If the service cannot enforce a constraint, it must bypass caching or reject the intent. It
must not proceed under a weaker one. Fresh computation with no cross-request retention
satisfies every constraint in the document, so a conforming fallback always exists.

Both outcomes are conforming and they differ by an HTTP status code, so which one a caller
gets is discoverable rather than guessed: `on_unenforceable` in the
[capability object](llm-cache-capabilities.schema.json) says whether this implementation
rejects or bypasses.

What comes back is a [`cache_status`](llm-cache-status.schema.json) entry per fragment. Its
`effective_share` reports the boundary actually used, not the one permitted, since an
implementation may narrow and a caller reads the field to verify its sharing model. Its
`outcome` may be `unobserved`, which means the implementation cannot tell a hit from a miss;
an implementation must not report `miss` when it means `unobserved`.

## Constraints compose downward

KV state is cumulative, so the constraints on it are cumulative too. State computed from
several fragments carries the most restrictive combination of their constraints: `forbid`
retention wins over `allow`, the smallest `max_age` wins, `none` beats `exact` beats
`approximate`, sharing boundaries intersect, and every contributing namespace must appear in
the effective cache identity.

A fragment carrying no `cache_intent` of its own inherits the effective policy of the
fragments before it, because the state it produces depends on them.

A later fragment cannot relax what it inherited. Put a public tool schema after a
tenant-scoped system prompt and the downstream state is still tenant-scoped, because it
depends on both. Put the schema first and the schema-only prefix can be shared publicly
while everything after it narrows to the tenant. Order matters, which is also why a gateway
must not reorder model-visible fragments to improve hit rates.

Composition over no fragments has no result at all. An implementation must not resolve it to
the identity element of the lattice, which would be `share_max: public` and
`reuse: approximate`, because a request that never mentioned caching would then authorize
cross-tenant sharing and approximate reuse it never asked for.

The effective namespace digest is specified byte for byte rather than described, down to the
length prefix and its encoding. Two implementations can both choose length-prefixed SHA-256,
both conform to a description, and still fail to reuse a single cache entry the other wrote.
Golden values in the test file pin the construction.

The [test vectors](llm-cache-control-test-vectors.json) encode these cases.

## Why another cache API

Pieces of this exist already. Anthropic exposes [cache breakpoints and two TTLs](https://docs.claude.com/en/docs/build-with-claude/prompt-caching),
OpenAI caches prefixes [automatically with an optional routing key](https://platform.openai.com/docs/guides/prompt-caching),
and Google exposes [named cache objects with an explicit TTL](https://ai.google.dev/gemini-api/docs/caching).
vLLM has [cache salting](https://github.com/vllm-project/vllm/issues/16016) for prefix
isolation, plus open RFCs for [retention priority](https://github.com/vllm-project/vllm/issues/37003)
and [session-scoped cache coordinates](https://github.com/vllm-project/vllm/issues/48501).
[LMCache](https://github.com/LMCache/LMCache) exposes lower-level cache-management
operations.

Each of those is a mechanism bound to one provider or one engine. This proposal sits a layer
above them: an application-facing contract that a trusted gateway can authenticate, compose
across fragments, and lower into whichever mechanism the backend actually has.

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

The lowering is deliberately asymmetric. A soft hint may become an engine hint or be dropped
under pressure, but a hard application constraint stays hard.

The status object is modelled on the HTTP [`Cache-Status`](https://www.rfc-editor.org/rfc/rfc9211.html)
field in purpose, though not in syntax, and the constraint-versus-hint split follows the same
instinct as [`Cache-Control`](https://www.rfc-editor.org/rfc/rfc9111.html).

## Scope

The proposal covers cached input computation, mainly KV state. It does not try to
standardize semantic answer caches, eviction algorithms, GPU, CPU, or disk placement, cache
compression, context editing, approximate-reuse quality metrics, or provider billing. Those
can be separate mechanisms or later extensions.

## What is in the repository

[`draft-sood-llm-cache-control-01.md`](draft-sood-llm-cache-control-01.md) is the
Internet-Draft source, currently Experimental.
[`llm-cache-control.schema.json`](llm-cache-control.schema.json) is the JSON Schema for the
`cache_intent` object, with companion schemas for [capabilities](llm-cache-capabilities.schema.json)
and [status](llm-cache-status.schema.json).
[`llm-cache-control-test-vectors.json`](llm-cache-control-test-vectors.json) holds the
conformance vectors, including the composition cases and the golden namespace digests.
[`binding-openai-chat.md`](binding-openai-chat.md) binds the contract to the OpenAI Chat
Completions API.
[`gateway/`](gateway) is the reference gateway, with the binding implemented as a proxy in
[`gateway/proxy.py`](gateway/proxy.py) and the live engine binding in
[`gateway/vllm_backend.py`](gateway/vllm_backend.py).
[`conformance/`](conformance) runs the vectors and the behavioral requirements against that
gateway.

The wire object is named `cache_intent` because the constraints are what the application
intends, not what the cache does. The repository is named for the analogy that motivates it.

### The binding

The draft is transport neutral on purpose, and it stops at a hard boundary: a host protocol
adopting it must define what a fragment is and where these objects ride.
[`binding-openai-chat.md`](binding-openai-chat.md) does that for the OpenAI-compatible Chat
Completions API, the surface vLLM, OpenAI, and most gateways already speak.

A fragment is one `tools` element or one `messages` element, tools first, because that is how
chat templates render input and the composition rules depend on which fragments precede
which. `cache_intent` rides on the fragment. Scopes resolve from the credential, never from
the body. `cache_status` comes back top level beside `choices` and `usage`, and capabilities
live at `GET /v1/cache_intent_capabilities`.

### The gateway

[`gateway/`](gateway) implements the application-facing half of the contract: it parses a
request, refuses to trust anything the caller says about its own scope, composes the
constraints across fragments, resolves each prefix against a cache, and reports what
happened. It is the part where the draft's requirements either hold or do not.

Its backend is an interface, not a fixed store. Four implementations ship with it, and they
differ on purpose. [`InMemoryBackend`](gateway/backends.py) enforces everything version 1
defines. `SaltOnlyBackend` models an engine that can partition a prefix cache but cannot
bound its lifetime. `NullBackend` caches nothing, which is the constraint-preserving fallback
the draft says always exists. [`VLLMBackend`](gateway/vllm_backend.py) talks to a real
server, and the conformance suite checks that its declared capabilities match what
`SaltOnlyBackend` predicted, so the model is held to the thing it models.

That spread is what makes the central safety property testable. A backend that cannot enforce
a constraint must cause a bypass or a rejection, never a quiet downgrade to a hint. A gateway
with one all-capable backend can never demonstrate that, because nothing is ever
unenforceable. With three, the suite asserts for every constraint that the weak backend
bypasses, the strict-mode gateway raises, and the capable backend does neither, so the bypass
means something.

## Running it

```
pip install jsonschema pytest
make check
```

That runs the conformance suite, the minimality analysis, and the gateway unit tests.

The conformance suite validates every JSON example in this readme and the draft against a
schema, composes the test vector fragments through [`compose.py`](gateway/compose.py) and
checks each against its expected effective policy, runs the negative cases, and then
exercises the behavioral requirements against the gateway. It closes with a coverage report,
because a run that only counts passes hides what it never looked at.

All ten interoperability tests in the draft's appendix are covered: seven as data vectors,
and three against the gateway because they constrain behavior rather than document content.
So are the three requirements no JSON Schema can express, which need an implementation for
the same reason: rejection of duplicate member names, exact namespace comparison, and the
prohibition on Unicode normalization before comparison. Every guarantee the suite makes has
mutation coverage, meaning the rule it rests on has been inverted on purpose and the specific
case that covers it turns red.

`make draft` renders the Internet-Draft to `.xml` and `.txt` (run `make tools` once first);
both are build output and are gitignored, so edit the markdown.

To run the live tests, start a server and point the suite at it:

```
pip install vllm 'transformers<5'
vllm serve Qwen/Qwen2.5-0.5B-Instruct --max-model-len 2048 --enable-prefix-caching
VLLM_BASE_URL=http://127.0.0.1:8000 python -m pytest gateway/tests/test_vllm_live.py -v
```

The `transformers` pin matters: vLLM 0.11 calls an API that version 5 removed, and the server
dies at tokenizer load without it. These tests skip when `VLLM_BASE_URL` is unset, and the
skip says the binding is unverified rather than passing quietly.

The proxy runs as a server you can point any OpenAI client at:

```
VLLM_BASE_URL=http://127.0.0.1:8000 python -m gateway.proxy
```

## What is verified on a live engine

[`VLLMBackend`](gateway/vllm_backend.py) lowers the contract onto a running vLLM. The engine
offers one hook for this, `cache_salt`, a string sent alongside the prompt that vLLM folds
into the hash of the first KV block, so only requests carrying the same salt can reuse each
other's blocks. The gateway derives that string from the authenticated partition and a digest
of the application namespace, then hashes it, because the engine only ever compares salts for
equality and a customer name in engine metadata buys nothing.

Measured on a real server with `python -m gateway.demo_vllm_isolation`, two authenticated
tenants sending byte-identical prompts:

```
  no cache_salt sent                         688 tokens reused
  gateway-derived salt per tenant              0 tokens reused
  same tenant twice (control)                688 tokens reused
```

The control line is the point. Without it, zero reuse would be indistinguishable from a
server that caches nothing.

Read the measurement for what it is. It was taken on CPU with a 0.5B model, and it
establishes that `cache_salt` partitions the prefix cache the way `share_max` needs. That is
a policy property, decided before any token is computed, so it does not vary with hardware.
It says nothing about behavior under GPU-scale concurrency, memory pressure, or eviction, and
nothing about whether the isolation holds on vLLM code paths these tests do not exercise.

Two constraints do not reach the engine at all. vLLM cannot bound a cache entry's lifetime or
match approximately, so `max_age` and `reuse: approximate` are unenforceable on it and the
gateway bypasses rather than serving them under weaker terms. The proxy still performs the
inference, but sends a request-unique salt so the request cannot read from or publish state
into a partition available to a later request. Omitting the salt would select vLLM's default
cache domain and would not be a bypass.

One status value is unobservable through this surface. vLLM 0.11 returns
`prompt_tokens_details` as null on the OpenAI endpoint, so a gateway in front of it cannot
tell a hit from a miss for any single request and reports `unobserved`. Reporting `miss`
would be a lie a caller could not detect: they would measure a zero hit rate and conclude the
cache was broken.

## What it does not buy yet

On vLLM, application intent wins nothing over a blanket per-tenant salt. The workload is the
draft's flagship shape: a large public tool schema every tenant shares, then a small
tenant-private system prompt. Tenant A warms the cache, tenant B sends the same public prefix
with its own private suffix. Run `python -m gateway.experiment_policy_comparison`:

```
Ceiling: an identical repeat reuses 1472 tokens.

  no isolation               1392 tokens reused   (95% of ceiling)
  blanket tenant salt           0 tokens reused   (0% of ceiling)
  application intent            0 tokens reused   (0% of ceiling)
```

The first line is what you get with no cache control: near-total reuse, and the private suffix
leaks across tenants, so it is not deployable. The second is what an engine can do knowing
nothing about the application. The third should have been the payoff, and it is identical to
the second.

The cause is structural. vLLM folds one `cache_salt` into the hash of the first block, and
every later block chains off that hash, so a request carries exactly one sharing boundary.
There is nowhere to say that the first eighty blocks are public and the rest are
tenant-private. A per-message `cache_salt` is accepted by the API and silently ignored, which
at least fails safe: you get more isolation than you asked for, not less.

This is not a bug. vLLM's [cache salting RFC](https://github.com/vllm-project/vllm/issues/16016)
calls this the single-barrier design and names the multi-barrier version as deferred future
work, blocked on forwarding message boundaries through template rendering. So the contract's
composition model is enforceable on this engine but unrewarded. Recovering the public prefix
needs a partition boundary that can change partway through the input, which no backend here
offers.

The draft does not require engines to provide one. It does not prescribe engine internals,
and a requirement aimed at implementers it has no standing over would be decoration. What it
requires instead is that the gap be discoverable: `per_fragment_share` in the capability
object says whether an implementation applies one boundary per fragment or one per request.
That matters because the loss is invisible at the call site. The request succeeds, every
constraint is honored, and the only symptom is reuse that never happens.

## Known gaps

Every member of version 1 earns its place, on evidence. `python -m conformance.minimality`
tests each constraint for being discriminating, meaning some change to its value changes
observable behavior, and irreducible, meaning its effect cannot be had by setting some other
member. All four pass: `retention.mode` and `max_age` change status and retention,
`share_max` produces five distinct behaviors for five values, `namespace` partitions cache
identity, and `reuse` needs its third value because `approximate` differs from `exact` only
on input `exact` cannot serve.

Hints get a different criterion, because a hint may be ignored and so is entitled to fail the
constraint test. `reuse_within` clears the bar that applies: a value above the advertised
`reuse_within_max` must be reported as `clipped` and a value at or below it must not, so both
the support and the value are observable.

There is deliberately no priority number. A hint stating how much avoiding a miss is worth is
the obvious second one to define, and the draft already carries the standard it fails, in its
section on approximate reuse: a bare numeric budget is not interoperable unless the metric,
baseline, measurement procedure, and enforcement semantics are also defined. A 0-to-100
priority has none of those, so two implementations could both claim support, treat 80 and 30
identically or oppositely, and no caller could tell which. An extension may define it
together with the semantics that make the number mean something.

No second implementation exists. A specification is precise enough when two people can
implement it from the text and interoperate, and that has not been tried. A cold read of the
document by a same-lineage model is the closest thing here, and it is not a substitute for a
human implementer.

The draft carries one open issue, and it is the one evidence cannot settle: which community
is the long-term home. It is not the IETF's AI Preferences working group, the only chartered
IETF group on the subject, whose charter covers how a content owner expresses preferences
about collection and processing and explicitly puts authorization and enforcement out of
scope, which is the opposite of every constraint here. The nearest work is an expired
individual draft in the IRTF Network Management Research Group on requirements for LLM
inference services, which treats prefix caching as a requirements question rather than an
application-facing data model. A research group is the right maturity for a document with no
independent implementations. Two things would make a chartered working group the right ask
instead: a second implementation that interoperates with the first, and an engine that can
express a sharing boundary partway through the input.

Everything else the appendix once listed is now decided in the section that carries the rule.
Named groups stay out of version 1 because a group is a boundary only if some authority can
say who is in it and this document defines none. `max_age` is relative rather than an
absolute deadline because an absolute one needs the application, gateway, and engine to agree
on a clock, and a skewed clock lengthens retention past what was authorized. `session` stays
in the ladder because a multi-turn conversation is the shape this contract exists to serve.

LMCache is untouched. vLLM is the only engine the contract has been lowered onto.

## Contributing

The most valuable contribution is a second implementation, and
[`CONTRIBUTING.md`](CONTRIBUTING.md) has the one instruction that matters, which is
counterintuitive: read the specification and do not read the reference implementation. Once
you have read `gateway/`, you resolve every ambiguous sentence the way it resolved them, and
the ambiguity becomes invisible to you.

Divergence from the reference implementation is a finding about the specification, not a bug
report. Both implementations may be conforming; that is the problem.

The point is not standardization by decree. It is to make the contract precise enough to
implement twice, test for interoperability, and find out where the abstraction is wrong.

## Licensing

Two licenses apply. The code is BSD-3-Clause, which is what the IETF Trust Legal Provisions
specify for code components. The Internet-Draft carries the provisions declared in its own
front matter, `ipr: trust200902`.
