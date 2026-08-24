# Cache control binding for the OpenAI-compatible Chat Completions API

Status: experimental. Companion to `draft-sood-llm-cache-control-01.md`.

The draft is transport neutral. It says what a cache intent means and how
intents compose, and then it stops:

> A host protocol adopting this specification MUST define what constitutes a
> fragment and where the objects defined here are carried.

This document does that for the OpenAI-compatible Chat Completions API, the
surface vLLM, OpenAI, and most gateways already speak. It is the smallest
binding that makes the contract testable end to end. The key words MUST,
MUST NOT, SHOULD, and MAY are to be interpreted as in BCP 14.

## 1. What is a fragment

A fragment is one element of the `tools` array or one element of the
`messages` array.

Fragment order is every `tools` element in array order, followed by every
`messages` element in array order. Chat templates conventionally render tool
definitions ahead of the conversation, which is why this binding declares
that order.

The declaration is a claim about rendering, and a template that renders
otherwise breaks it without producing any error. If a tenant-scoped system
message renders ahead of a `public` tools fragment, treating the tools
fragment as the shareable prefix puts tenant content in a public partition,
and nothing in the response says so. An implementation MUST therefore
establish that the model's template renders tools before messages. If it
cannot, it MUST apply the effective policy of the whole request to every
fragment, which is the single-partition rule in section 7.

A binding for a template with a different order MUST state that order, because
the composition rules in the draft depend on which fragments precede which.

Nothing else is a fragment. Top-level request members such as `model`,
`temperature`, and `max_tokens` are execution identity and belong to the
inference service, not the application.

In particular a content part inside a message's `content` array is not a
fragment in this version, even though the draft names "content block" as a
candidate fragment for host protocols generally. An implementation that
finds a `cache_intent` on a content part MUST reject the request with
`cache_intent_invalid` rather than ignore it. Silently discarding it is the
one unacceptable option of the three: the application stated a constraint,
believes it is in force, and gets neither enforcement nor an error. Lifting
this restriction is future work and needs a rule for how a part-level
boundary composes with its containing message.

## 2. Where cache intent is carried

A fragment MAY carry a `cache_intent` member holding the object defined at
`#/$defs/cache_intent` in `llm-cache-control.schema.json`.

Validate against that subschema, not against the schema's root. The root
describes a standalone envelope whose only member is `cache_intent`, so a
chat message validated against it fails on `role` and `content`. Pointing at
the file rather than the subschema rejects every conforming request, so the
pointer has to name `#/$defs/cache_intent` explicitly.

```json
{
  "model": "some-model",
  "tools": [
    {
      "type": "function",
      "function": {"name": "search", "parameters": {}},
      "cache_intent": {
        "version": 1,
        "id": "tools",
        "constraints": {
          "retention": {"mode": "allow", "max_age": 86400},
          "reuse": "exact",
          "share_max": "public",
          "namespace": "tools:v7"
        }
      }
    }
  ],
  "messages": [
    {
      "role": "system",
      "content": "You are a support agent for this tenant.",
      "cache_intent": {
        "version": 1,
        "id": "system",
        "constraints": {
          "retention": {"mode": "allow", "max_age": 3600},
          "reuse": "exact",
          "share_max": "tenant",
          "namespace": "support-agent:v4"
        }
      }
    },
    {"role": "user", "content": "Where is my order?"}
  ]
}
```

A fragment with no `cache_intent` supplies no constraints of its own. It
still inherits the effective policy of everything before it, because the
state it produces depends on those fragments.

An implementation MUST NOT forward `cache_intent` to an upstream inference
service that does not define it. It is a gateway-facing member, and leaving
it in the body changes the rendered prompt on some servers.

## 3. How scopes are resolved

The scope values in `share_max` are logical. This binding resolves them from
authenticated request context and never from the request body.

`request` is the current HTTP request. `session` is the session bound to the
presented credential. `principal` is the authenticated end user or API key.
`tenant` is the account, organization, or project that principal belongs to.
`public` is unrestricted.

An implementation MUST derive tenant, principal, and session from the
credential presented in the `Authorization` header or an equivalent
authenticated channel. A member of the request body naming a tenant,
principal, session, group, or cache partition MUST be ignored, and an
implementation SHOULD strip such members before forwarding.

`cache_salt` is an engine-level cache partition and is therefore included in
that rule. A trusted gateway MUST strip a caller-supplied value before deriving
the salt, if any, that it sends upstream.

## 4. Where status is returned

A response carries a top-level `cache_status` array as defined by
`llm-cache-status.schema.json`, beside `choices` and `usage`. Each entry
correlates to a fragment's `cache_intent.id` when one was supplied.

```json
{
  "choices": [],
  "usage": {},
  "cache_status": [
    {"id": "tools", "outcome": "exact_hit", "effective_share": "public",
     "retention": "retained", "constraint_result": "satisfied"}
  ]
}
```

An implementation MAY omit `cache_status` entirely. It MUST NOT report an
outcome it did not observe, and it MUST NOT report cache activity outside
the caller's authorized observation boundary.

## 5. Where capabilities are advertised

`GET /v1/cache_intent_capabilities` returns the object defined by
`llm-cache-capabilities.schema.json`.

The capability object includes `on_unenforceable`, naming which of the two
permitted responses this server gives to an intent it cannot honor. Section
6 permits both, and they differ by an HTTP status code, so a client that
cannot read this member cannot write a portable handler.

A client MUST NOT infer support from the absence of an error. A server that
does not implement this binding will accept `cache_intent` as an unknown
member and ignore it, which is indistinguishable from enforcement at the
call site. The capabilities endpoint exists so that difference is
observable before anything relies on it.

## 6. Errors

An implementation that rejects a cache intent MUST return HTTP 400 with an
`error` object whose `code` is one of `cache_intent_invalid`,
`cache_intent_unsupported_version`, or `cache_intent_unenforceable`.

An implementation MUST NOT respond to an unenforceable constraint by
serving the request under a weaker one. Bypassing the cache and reporting
`outcome: bypass` is the other permitted response, and is the default in
the reference gateway.

## 7. Known limitation on per-request partitioning

An engine that accepts a single cache partition per request can honor only
the effective policy of the whole request, which is the narrowest boundary
any fragment asked for. Every constraint still holds, because narrowing is
always permitted. What is lost is sharing: a public tool schema in front of
a tenant-scoped system prompt is recomputed for every tenant rather than
shared.

vLLM is such an engine today. Its `cache_salt` is folded into the hash of
the first block, and later blocks chain off that hash, so one request
carries one boundary. `gateway/experiment_policy_comparison.py` measures the
cost. Bindings SHOULD report this as a capability gap rather than presenting
per-fragment scoping as effective when it is not.

When any fragment requires fresh computation or an unenforceable constraint
uses the fallback, a single-partition binding MUST narrow the whole request to
a request-unique partition. Omitting the partition would select the engine's
default cache domain and would not be a bypass. The status for every governed
fragment MUST report the request boundary actually used.
