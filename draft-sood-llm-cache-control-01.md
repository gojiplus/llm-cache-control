---
stand_alone: true
ipr: trust200902
cat: exp
submissiontype: IETF
area: Applications and Real-Time

docname: draft-sood-llm-cache-control-01
title: Application Cache Control for Large Language Model Inference
abbrev: LLM Cache Control
lang: en
kw:
  - large language models
  - inference
  - caching
  - KV cache

date: 2026-08-22

author:
- ins: G. Sood
  name: Gaurav Sood
  organization: Independent
  country: United States

normative:
  RFC8259:

informative:
  RFC9111:
  RFC9211:

--- abstract

Large language model applications often know facts about cached input
state that an inference service cannot reliably infer.  Some of those
facts are constraints, such as the maximum authorized sharing boundary or
whether reuse must be exact.  Others are hints, such as an expected reuse
window or the relative cost of a miss.

This document defines a transport-neutral JSON data model for carrying
that information from an application, through a trusted gateway when
present, to an inference service.  It defines conservative composition for
state that depends on multiple input fragments, requires fail-closed
handling of unsupported constraints, and defines capability and status
objects.  It does not standardize cache storage, eviction, placement,
answer caching, or context editing.

--- middle

# Introduction {#intro}

Inference services cache intermediate state to avoid repeating work over
shared model input.  An inference service can observe tokens, cache keys,
access history, memory pressure, and physical residency.  It usually
cannot determine, from those observations alone, whether an input fragment
may cross a tenant boundary, whether approximate reuse is acceptable,
whether a branch will resume soon, or whether an application-defined
version change should deliberately prevent a match.

The application often knows those things.  Today it communicates them
indirectly, if at all: by ordering prompt content, choosing from a small
set of provider-specific lifetimes, replaying requests to keep state warm,
or reaching below the inference API into an engine-specific control
surface.

This document defines a small contract across that information boundary.
The application supplies:

1. constraints that an implementation MUST NOT weaken; and
2. hints that an implementation MAY weigh, clip, or ignore.

The inference service retains control over execution identity, cache
representation, physical placement, admission, eviction, compression,
prefetch implementation, and scheduling.

The contract is an application-facing source language.  A trusted gateway
or control plane may authenticate, normalize, and lower it into
provider-specific cache controls or engine-level lifecycle hints.  Clients
do not manage physical block identifiers, cache pointers, or storage tiers.

The contract is attached to an input fragment.  A fragment might be a
message, content block, tool definition, retrieved document, or another
ordered unit defined by a host inference protocol.  This document does not
define a universal inference request format.  A host protocol adopting
this specification MUST define what constitutes a fragment and where the
objects defined here are carried.

Fragment order is the order in which fragment content appears in the model
input the service actually renders, not the order of the members that carry
the intents.  The two can differ: a template may render a later member ahead
of an earlier one.  Where they can differ, an implementation MUST establish
the rendered order or apply the effective policy of the whole request to
every fragment in it.  Composing in an order the model does not see would
report a boundary the input does not have.

## Requirements Language {#requirements-language}

{::boilerplate bcp14-tagged}

# Terminology {#terminology}

Application:
: The client or orchestration layer that constructs an inference request.

Trusted gateway:
: An authenticated intermediary that may normalize application cache
  intent, bind logical scopes to authorization state, and lower intent into
  an inference-service-specific mechanism.

Inference service:
: The system that executes the model and manages reusable input state.

Host protocol:
: An inference API or protocol that carries the data model defined here.

Fragment:
: An application-visible unit of ordered model input to which cache intent
  can be attached.

Cached input state:
: Reusable computation derived from model input.  A key-value attention
  cache is one example.

Dependent state:
: Cached input state whose value was computed using a fragment, directly or
  through other state.

Execution identity:
: Every input and execution property that must match for cached state to be
  computationally equivalent to fresh computation.  The inference service,
  not the application, is responsible for constructing this identity.

Application namespace:
: An application-supplied equality partition that is added to execution
  identity.  It can prevent a match but cannot make different input match.

Constraint:
: A requirement an implementation MUST NOT weaken.  An implementation may
  satisfy a constraint by declining cache use and performing fresh
  computation without cross-request retention.

Hint:
: Information that may improve cache management but that an implementation
  MAY clip or ignore.

Effective policy:
: The policy obtained after composing the intent of every fragment on which
  a piece of cached state depends.

# Architecture {#architecture}

The architecture has three logical layers:

Application:
: Supplies semantic constraints and future-use information that cannot be
  inferred reliably from cache telemetry alone.

Trusted gateway or control plane:
: Authenticates scopes, derives trusted internal coordinates, composes
  fragment policies, discovers capabilities, and lowers cache intent into
  provider- or engine-specific controls.

Inference service:
: Builds exact execution identity, resolves logical intent to current cache
  state, owns physical cache management, and reports outcomes.

These layers may be deployed in one process.  Their responsibilities remain
separate even when they are co-located.

A client-provided tenant, principal, session, group, namespace, or handle
is not authorization.  When a trusted gateway is present, it MUST derive
sharing boundaries from authenticated context and MUST strip or replace
untrusted values before forwarding engine-level coordinates.

# Scope and Non-Goals {#scope}

This document applies to reuse of input computation.  It is intended for
exact prefix caches, external stores of input state, and explicitly
opted-in approximate reuse of input state.

This document does not define:

* storage or reuse of complete model answers;
* semantic answer caches;
* model request or response logging;
* complete execution identity;
* physical cache formats;
* eviction algorithms;
* GPU, CPU, disk, or remote placement;
* compression formats;
* provider pricing or billing;
* secure media erasure; or
* context-edit operations such as removing a span while preserving or
  removing its prior influence.

Context editing and lifecycle operations such as prefetch, offload, and
release may be specified by separate extensions.

# Cache Intent Object {#model}

A fragment may contain a `cache_intent` object.  The object is JSON
{{RFC8259}}.  It contains a required version, an optional application
identifier, a required `constraints` object, and an optional `hints`
object.

~~~~ json
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
~~~~
{: title="Cache intent for a reusable fragment"}

An implementation MAY ignore or clip any hint.  It MUST NOT silently
weaken a constraint.

An implementation MAY apply a more restrictive policy.  For example, it
may:

* use `principal` sharing when `tenant` sharing is permitted;
* use exact reuse when approximate reuse is permitted;
* retain state for less than `max_age`; or
* decline to retain or reuse state.

If an implementation cannot enforce a constraint, it MUST either reject
the cache intent or bypass caching for the affected state.  It MUST NOT
proceed under a weaker constraint.  Fresh computation with no
cross-request retention is always a valid constraint-preserving fallback.

The absence of `cache_intent` leaves cache behavior to the host protocol.
This document does not alter existing requests that do not use the
extension.

## Version {#version}

`version` is a positive integer.  This document defines version `1`.

An implementation receiving an unsupported version MUST reject the cache
intent or use the constraint-preserving fallback defined in
Section {{fallback}}.

## Identifier {#id-member}

The optional `id` member is a string chosen by the application for
correlation with status reports.  It does not enter cache identity unless
it is separately supplied as `namespace`.

An application MUST NOT assume that `id` is globally unique.  A host
protocol SHOULD scope it to one request or session.

An `id` MUST be unique among the fragments of a single request.  Correlation
in Section {{reporting}} is positional, so a duplicate does not make a status
entry ambiguous, but indexing the status array by `id` is the obvious way to
read it and a duplicate silently loses an entry for a caller who does.

An `id` MUST contain at least one and at most 256 characters.  An
implementation receiving a longer value MUST reject the cache intent or use
the constraint-preserving fallback defined in Section {{fallback}}.

## Constraints and Hints {#constraint-hint-separation}

The separation between `constraints` and `hints` is normative.

If an implementation receives an unknown member in `constraints`, a
missing required constraint, an unknown top-level member in
`cache_intent`, or a duplicate JSON member name, it MUST reject the cache
intent or use the constraint-preserving fallback.

If an implementation receives an unknown member in `hints`, it MAY ignore
that member.  It MUST continue to enforce all understood constraints.

This rule allows hints to evolve without allowing an older implementation
to ignore a new security or correctness requirement.

# Constraints {#constraints}

The `constraints` object is REQUIRED.  It contains `retention`, `reuse`,
and `share_max`.  It MAY contain `namespace`.

## Retention {#retention}

`retention` is an object containing a required `mode` and an optional
`max_age`.

`mode` has one of two values:

`allow`:
: The service may retain cached input state for later requests.  This value
  does not require retention.

`forbid`:
: The service MUST NOT make cached input state derived from the fragment
  available to a later request.  This requirement also applies to dependent
  state unless the service establishes that the state is independent of
  the fragment.

`retention` governs reusable input state across requests.  It does not
prohibit temporary state required to execute the current request.  It does
not govern logs, abuse-monitoring records, or records outside the cache.

`max_age`, when present, is a non-negative integer number of seconds.  It
sets an upper bound on how long newly materialized state may remain
eligible for cross-request reuse.

State is materialized when it is computed from input a request supplied.
Moving it between storage tiers, re-encoding or recompressing it, and copying
it to another node do not restart the interval, because none of them computes
anything from newly supplied input.  A read MUST NOT restart it either.  An
implementation that restarted the interval on internal movement could hold
state indefinitely while claiming to honor a one-hour bound.

Recomputing the state for a later request that supplied the same input does
begin a new interval for the newly computed state.  That request carried the
input, so the bound starts again from its own materialization rather than
inheriting the expiry of state that is gone.

State is eligible for cross-request reuse while
`now < materialized_at + max_age` and MUST NOT be reused once
`now >= materialized_at + max_age`.  The comparison is stated because the
boundary instant is otherwise decided per implementation, and two
implementations that disagree about it disagree about whether a given entry
exists.

`max_age: 0` therefore permits no cross-request reuse at all.  It has the
same effect as `retention.mode: forbid` and is a valid way to express a
bound computed at runtime that happened to reach zero.

This document does not specify a secure-erasure deadline for the underlying
memory or media.

`max_age` is valid only when `mode` is `allow`.

The bound is relative rather than an absolute deadline because an absolute
one requires the application, the gateway, and the engine to agree on a
clock, and a retention bound is a safety property that must not weaken when
they disagree.  A skewed clock turns an absolute deadline into a longer
retention than the application authorized, while a relative bound is
evaluated entirely against the engine's own monotonic time.  HTTP carries
both forms and resolves the conflict by preferring the relative one
{{RFC9111}}; this document defines only the relative one.

## Reuse {#reuse}

`reuse` has one of three values:

`none`:
: The service MUST perform fresh input computation for the affected state
  on the current request.

`exact`:
: The service may reuse state only when it is computationally equivalent to
  fresh input computation under the same execution identity.

`approximate`:
: The service may use exact or approximate reuse.  Approximate reuse is
  permitted, not required.

Exact reuse is the default safety level.  Approximate reuse always requires
an explicit application opt-in.

`reuse` governs reads on the current request.  `retention` independently
governs whether newly materialized state may be made available to later
requests.  For example, a cache-warming request could use `reuse: none`
and `retention.mode: allow`.

## Maximum Sharing Boundary {#share-max}

`share_max` places an upper bound on the authenticated requests that may
observe or reuse cached state.  It has one of the following values:

`request`:
: Reuse is limited to the current request.

`session`:
: Reuse is limited to the same authenticated logical session.

`principal`:
: Reuse is limited to the same authenticated principal.

`tenant`:
: Reuse is limited to the same authenticated tenant, account, project, or
  equivalent administrative boundary.

`public`:
: Cross-tenant reuse is permitted.

The five scopes defined above form a total order under containment:

~~~~
request  <  session  <  principal  <  tenant  <  public
~~~~

Every authenticated request in a narrower scope is also in each wider one.
An implementation MUST use this ordering when composing `share_max`, so the
intersection of a set of these scopes is the narrowest member of the set.
The relation is stated here rather than left to the host protocol because
`session` and `principal` admit two readings.  A session belonging to one
principal sits inside it; a session shared between two principals is
incomparable with either.  The two readings send the same request down
opposite paths, one sharing state the other refuses to share.

A host protocol MUST define how authenticated sessions, principals, and
tenants are resolved, and MUST define the containment relationships of any
scope it adds beyond these five.  It MAY define additional scopes,
including named groups, in a future version or extension.  Named groups are
deliberately not in version 1: a group is a boundary only if some authority
can say who is in it, and this document defines no membership authority and
no way to authenticate one.  Adding the name without the authority would
produce a scope that looks like an authorization boundary and is not, which
Section {{security}} forbids for every other scope here.  Where an added
scope is incomparable with a defined one, the rule in Section
{{composition}} applies and the implementation bypasses reuse.

`session` is in the ladder because a multi-turn conversation is the shape
this contract exists to serve: the state a chat thread accumulates is
reusable across that thread's turns and usually across nothing else.  A host
protocol with no notion of a session simply never receives the value.

An implementation MAY use a narrower boundary than the one requested.  It
MUST NOT use a wider boundary.

`public` is an explicit maximum permission.  It MUST NOT be inferred from
frequency, popularity, content classification, or the absence of personal
data.

## Application Namespace {#namespace}

`namespace` is an optional opaque string supplied by the application.  An
implementation MUST include it in cache partitioning or cache identity so
that state associated with different namespaces cannot match.

The namespace is additional identity.  It does not replace model-,
tokenizer-, adapter-, multimodal-, rendering-, or execution-specific
identity known to the inference service.

A namespace can only prevent a match.  It cannot make different tokens or
incompatible execution state match, and it cannot widen `share_max`.

Namespace comparison is exact.  Implementations MUST NOT apply Unicode
normalization before comparison.  Applications SHOULD use short opaque
ASCII values and SHOULD NOT place user data or secrets in a namespace.

A namespace MUST contain at least one and at most 256 characters.  An
implementation receiving a longer value MUST reject the cache intent or use
the constraint-preserving fallback defined in Section {{fallback}}.  The
bound keeps cache metadata bounded as required by Section {{security}}.

# Hints {#hints}

The `hints` object is OPTIONAL.  This document defines one hint,
`reuse_within`.

There is deliberately no priority number.  A hint stating how much avoiding a
miss is worth is the obvious second one to define, and Section
{{approximate-reuse}} states the standard it fails: a bare numeric budget is
not interoperable unless the metric, baseline, measurement procedure, and
enforcement semantics are also defined.  A 0-to-100 priority has none of
those, so two implementations could both claim support, treat 80 and 30
identically or oppositely, and no caller could tell which.  An extension may
define priority together with the semantics that make the number mean
something.

## Expected Reuse Window {#reuse-within}

`reuse_within` is a non-negative integer number of seconds.  It states that
the application expects the fragment or its dependent state to be reused
within that interval after the current request completes.

It is not:

* a freshness lifetime;
* a minimum retention guarantee;
* a hard pin;
* a request for a storage tier; or
* an authorization boundary.

An implementation MAY use the value to influence admission, retention,
placement, routing, or prefetching.  It MAY ignore the value or evict the
state earlier under pressure.

A host protocol MAY define a maximum accepted value.  It MUST report or
document any clipping behavior.

# Conservative Composition {#composition}

Cached input state is often cumulative.  In a prefix cache, state for a
later fragment depends on the fragments before it.  A block may also span
more than one application fragment.  An implementation MUST therefore
apply constraints to every piece of cached state that depends on the
constrained fragment.

Composition over no contributing fragments has no result.  A prefix in
which no fragment carried a `cache_intent` has no effective policy, and its
cache behavior is left to the host protocol as described in Section
{{model}}.  An implementation MUST NOT treat the empty composition as the
widest policy: deriving `share_max: public` or `reuse: approximate` from the
absence of any statement would infer permissions the application never
granted, which Sections {{share-max}} and {{reuse}} both forbid.

For state derived from multiple fragments, the effective constraints are
the most restrictive combination of the contributing constraints:

* `retention.mode` is `forbid` if any contributing fragment forbids
  retention.
* If retention is allowed, the effective `max_age` is the smallest supplied
  bound.  An absent `max_age` contributes no upper bound.
* Effective reuse is `none` if any contributor requires `none`; otherwise
  it is `exact` if any contributor requires `exact`; otherwise it is
  `approximate`.
* The effective sharing boundary is the intersection of all contributing
  `share_max` boundaries.
* Every contributing namespace MUST be represented in effective cache
  identity by the effective namespace digest defined below.

The effective namespace digest is computed as follows.  For each
*contributing* fragment that supplied a `namespace`, in fragment order and
without removing duplicates, encode the namespace as UTF-8; emit the length of that encoding
in octets as a decimal ASCII integer, then a COLON (U+003A), then the
encoded bytes.  The digest is the SHA-256 of that concatenation, represented
as 64 lowercase hexadecimal characters.  When no fragment supplied a
namespace there is no digest, and namespace contributes nothing to identity.

The contributing set is the one named at the top of this section: the
fragments the state actually depends on, not every fragment in the request.
A prefix that covers only the first of two namespaced fragments digests one
namespace, not two.  Reading it the other way gives a different cache
identity for the same state, so the two implementations share nothing.

The construction is specified rather than described because two conforming
implementations must produce the same cache identity for the same request,
or nothing either of them caches is reusable by the other.  Requiring only
that the representation be deterministic and collision-resistant is not
enough: those are properties, and two implementations can satisfy both,
choose length-prefixed SHA-256, encode the length differently, and produce
different digests for the same input.  The length prefix is what makes the
encoding injective:
without it the namespace lists `["ab", "c"]` and `["a", "bc"]` produce
identical bytes.  Order is preserved because order is semantically
load-bearing elsewhere in this document.

If two sharing boundaries are incomparable and their intersection cannot
be represented, the implementation MUST bypass reuse for the affected
state.

An implementation MAY avoid propagation only when it can establish that
the state does not depend on the more restrictive fragment.

For example, a public tool schema placed after a tenant-scoped system
prompt does not produce public downstream state.  The downstream state
depends on both fragments and remains tenant-scoped.  By contrast, placing
the public schema first may permit the schema-only prefix to be shared
publicly while later state narrows to the tenant.

Hints are not constraints.  An implementation MAY aggregate them according
to local policy, but MUST NOT use a hint to weaken an effective constraint.

A gateway or planner MUST NOT reorder, delete, or otherwise alter
model-visible fragments solely to improve cache reuse unless the host
protocol or application has declared the alternative order semantically
equivalent.

# Processing Requirements {#processing}

## Retained State Carries Its Own Policy {#retained-policy}

Section {{security}} requires an implementation to enforce `share_max` as an
authorization boundary rather than as a caller-selected cache-key
convention.  An authorization boundary constrains who may read, so the
policy recorded when state was materialized governs later requests, and this
section states that in the model rather than leaving it to be inferred from
the security considerations.

An implementation that retains cached input state for a later request MUST
record the effective policy under which that state was materialized,
including its sharing boundary and its effective namespace digest.

A later request MAY reuse that state only when the requesting authenticated
context falls within the recorded sharing boundary and the requesting
effective namespace digest equals the recorded one.  The requesting
`share_max` bounds what that request may publish.  It does not grant access
to state retained under a narrower boundary.

State computed from reused state depends on it, so the effective sharing
boundary of the new state is the intersection of the recorded boundary and
the requesting effective boundary.  This is the rule in Section
{{composition}} applied across requests rather than within one: a later
fragment cannot relax what it inherited, and neither can a later request.

An implementation that partitions its cache by the recorded boundary
satisfies this requirement structurally, because state outside the boundary
is not addressable.  An implementation that keeps a single pool and filters
candidates on read MUST apply the check above.  Without it, a request
declaring `share_max: public` sits inside the boundary of every retained
entry, and `share_max` stops being a constraint at all: state stored under
`tenant` would be readable by naming a wider boundary, which is the
convention Section {{security}} forbids.

## Exact Reuse {#exact-reuse}

For `reuse: exact`, the inference service is responsible for ensuring that
every property affecting cached input state is represented in execution
identity.  Examples may include model revision, tokenization, rendering,
adapter configuration, positional encoding, multimodal inputs, cache
representation, and relevant execution settings.

The application namespace is additional identity.  It does not replace any
execution dependency known to the service.

Exact reuse concerns input computation.  It does not guarantee identical
sampled output when decoding is nondeterministic.

## Approximate Reuse {#approximate-reuse}

An implementation MUST NOT perform approximate reuse unless the effective
reuse constraint is `approximate`.

An implementation performing approximate reuse MUST enforce the effective
sharing boundary and namespace.  It MUST NOT search for or materialize
donor state outside those boundaries.

State produced through approximate reuse, and downstream state influenced
by it, MUST NOT be published as exact reusable state unless the service
recomputes or otherwise establishes exact equivalence.  Approximate state
MUST NOT pollute an exact cache namespace.

A host protocol supporting approximate reuse MUST report when it occurs.
It MAY define named and validated quality profiles in a separate
specification.  A bare numeric quality budget is not interoperable unless
the metric, baseline, measurement procedure, and enforcement semantics are
also defined.

## Constraint-Preserving Fallback {#fallback}

Fresh computation with no cross-request retention satisfies every
constraint in this document.

An implementation MUST NOT translate:

* `retention.mode: forbid` into a shorter retention interval;
* `reuse: none` into exact reuse;
* `reuse: exact` into approximate reuse;
* a narrow sharing boundary into a wider one; or
* one namespace into another.

Using exact reuse when approximation is permitted, using a narrower sharing
boundary, retaining for less time, or declining retention are all permitted
because they are more restrictive.

# Capability Discovery {#capabilities}

A host protocol adopting this document SHOULD expose a capability object
before an application relies on optional behavior.  This document defines
the following representation; the host protocol defines its transport.

~~~~ json
{
  "cache_intent_capabilities": {
    "versions": [1],
    "constraints": {
      "retention_modes": ["allow", "forbid"],
      "retention_max_age": true,
      "reuse": ["none", "exact"],
      "share_max": ["request", "session", "principal", "tenant"],
      "namespace": true
    },
    "hints": {
      "reuse_within_max": 3600
    },
    "status": true,
    "per_fragment_share": false,
    "on_unenforceable": "bypass"
  }
}
~~~~
{: title="Example capability object"}

`per_fragment_share`, when `false`, states that the implementation applies
one sharing boundary to the whole request rather than one per fragment.
Every constraint still holds on such an implementation, because narrowing is
always permitted, but the arrangement in Section {{example-public-first}}
stops sharing: a public prefix in front of tenant-scoped state is recomputed
for every tenant.  This document places no requirement on engines to support
a boundary that changes partway through the input, since it does not
prescribe engine internals.  It requires only that a caller be able to find
out, because the loss is invisible at the call site: the request succeeds,
every constraint is honored, and the only symptom is reuse that never
happens.  An implementation that omits the member makes no claim, which by
the rule below means it does not support per-fragment boundaries.

`versions` lists every `cache_intent` version the implementation accepts,
in any order.  It is a list rather than a single number so that an
implementation supporting more than one version can say so, and so that a
client can choose the highest version both sides understand without probing.
A version absent from the list is not supported, per the rule below.

`on_unenforceable` states which of the two permitted responses to an
unenforceable cache intent the implementation uses: `reject` or `bypass`.
It does not describe invalid cache intent; the host protocol defines whether
invalid intent is rejected or receives the constraint-preserving fallback.
The two responses are not interchangeable from a caller's seat, because one
returns an error and the other returns a completed inference.  Without this
member a client cannot write a portable handler, since the same request may be
answered either way by two conforming servers.  An implementation that
exposes a capability object MUST advertise `on_unenforceable`, and MUST
behave as advertised.  Advertising it only as a SHOULD would leave the
portability hole the member exists to close.

A capability object describes what the implementation can enforce or
observe.  It does not grant authorization.  A gateway MAY advertise a
narrower surface than the underlying engine.

An absent member means the implementation does not support what the member
describes.  It does not mean the implementation declined to say.  A client
reading an absent `retention_max_age` MUST treat `max_age` as unsupported
rather than send it and discover the answer from an error, and an
implementation that supports a member MUST advertise it.  The conservative
reading is the required one because the alternative makes the object
useless: a client that cannot distinguish "no" from "unstated" has to probe
for every constraint it wants, which is what capability discovery exists to
avoid.

An application MUST NOT infer support for a constraint merely because a
provider performs similar cache behavior internally.

# Status Reporting {#reporting}

A host protocol adopting this document SHOULD return a `cache_status`
array.  When the array is returned it MUST carry one entry per governed
fragment, in fragment order, whether or not that fragment supplied an `id`.
An entry repeats the `id` when one was supplied and omits the member
otherwise; correlation is positional, and `id` is a convenience for the
caller rather than the correlation mechanism.  Omitting entries for
fragments without an `id` would leave a caller unable to align the array
with the request it sent.

~~~~ json
{
  "cache_status": [
    {
      "id": "tools-v7",
      "outcome": "exact_hit",
      "effective_share": "tenant",
      "retention": "retained",
      "constraint_result": "satisfied",
      "hints": {
        "reuse_within": "clipped"
      }
    }
  ]
}
~~~~
{: title="Example cache status"}

`outcome` has one of the following values:

`bypass`:
: No reuse was attempted for the affected state.  The effective reuse
  constraint was `none`, or a constraint was unenforceable and the
  implementation bypassed rather than rejected, or the implementation
  declined for its own reasons.  Which of these applies is knowable from the
  request, so this document does not split the value.

`miss`:
: A lookup was performed and no eligible cached state was found.  An
  implementation MUST NOT report `miss` unless it looked.  `miss` is a claim
  about what the cache contained, and a caller reads it as evidence that the
  cache is working but cold; reporting it for a request that never performed
  a lookup makes a warming request indistinguishable from a failing one.
  This is the same requirement stated below for `unobserved`, applied to the
  other direction.

`exact_hit`:
: Exact cached state was reused.

`approximate_hit`:
: Approximate cached state was reused.

`unobserved`:
: The implementation attempted reuse but cannot determine what happened.  A
  gateway in front of an engine that does not report per-request cache
  outcomes is in this position.  An implementation MUST NOT report `miss`
  when it means `unobserved`, because a caller measuring hit rate cannot
  tell the difference and will conclude the cache is not working.

`retention` has one of the following values:

`forbidden`:
: Effective constraints prohibited cross-request retention.

`declined`:
: Retention was permitted but the implementation did not retain the state.

`retained`:
: The implementation retained the state for possible later reuse.

`unobserved`:
: Retention was permitted and the implementation cannot determine whether
  the state was retained.

`effective_share` is the sharing boundary the implementation actually used,
which is not necessarily the composed `share_max`.  An implementation that
narrows, as Section {{share-max}} permits, MUST report the boundary it
applied rather than the one it was permitted to use.  A caller uses this
field to verify its sharing model, and reporting the permission instead of
the practice would defeat that.

`constraint_result` is `satisfied`, and that is its only value.  A request
that cannot be served under its effective constraints is rejected or
bypassed, and bypassing satisfies them, so no status entry can describe a
constraint that was not met.  An implementation MUST include the member in
every entry it emits rather than signalling by absence, and a caller MUST
NOT read a missing member as a failure.  A host protocol MAY define a
separate error shape for rejected cache intent.

For each understood hint, the status value is `accepted`, `clipped`, or
`ignored`.  An implementation that advertises a hint in its capability
object MUST report that hint's status in every entry governed by an intent
carrying it.  An implementation MAY omit the status of a hint it does not
advertise.

Reporting is what makes hint support observable.  A hint MAY be ignored, so
an implementation that uses one and an implementation that discards it can
produce identical cache behavior, and without a status a caller cannot tell
them apart or learn that it is sending a value into a void.  `ignored` is a
useful answer: it tells the caller to stop paying for the field.

Where the capability object advertises a maximum for a hint, a value above
that maximum MUST be reported as `clipped` and a value at or below it MUST
NOT be.  This makes the hint's value, and not merely the implementation's
support for it, observable to a caller.  `reuse_within_max` is such a
maximum.  `reuse_within_max` is the only such maximum this document defines.

A service MUST NOT use status reporting to reveal cache activity outside
the caller's authorized observation boundary.  A host protocol MAY return
coarser status when detailed outcomes would create a timing or presence
side channel.

The `cache_status` object is analogous in purpose, but not syntax, to the
HTTP `Cache-Status` field defined by {{RFC9211}}.  The split between
constraints an intermediary must not weaken and hints it may ignore follows
the same instinct as the HTTP `Cache-Control` directives in {{RFC9111}},
though the constraints here are authorization boundaries rather than
freshness controls.

# Examples {#examples}

## Public Prefix Followed by Tenant State {#example-public-first}

A public tool schema is followed by a tenant-specific system prompt.

~~~~ json
[
  {
    "cache_intent": {
      "version": 1,
      "id": "public-tools",
      "constraints": {
        "retention": {"mode": "allow", "max_age": 86400},
        "reuse": "exact",
        "share_max": "public",
        "namespace": "tools:v7"
      }
    }
  },
  {
    "cache_intent": {
      "version": 1,
      "id": "tenant-system",
      "constraints": {
        "retention": {"mode": "allow", "max_age": 3600},
        "reuse": "exact",
        "share_max": "tenant",
        "namespace": "support-agent:v4"
      }
    }
  }
]
~~~~

The prefix ending after `public-tools` may be shared publicly.  State after
`tenant-system` is tenant-scoped and has an effective maximum age of one
hour.

## Public Fragment After Private State {#example-public-after-private}

If the order is reversed, state for the later public fragment depends on
the tenant fragment.  Its effective boundary remains `tenant`; the later
annotation does not widen it to `public`.

## No Cross-Request Retention {#example-no-retention}

~~~~ json
{
  "cache_intent": {
    "version": 1,
    "id": "medical-intake",
    "constraints": {
      "retention": {"mode": "forbid"},
      "reuse": "none",
      "share_max": "request"
    }
  }
}
~~~~

The service may use temporary KV state while executing the request but MUST
NOT make affected state available to later requests.

## Approximate Reuse Permitted {#example-approximate}

~~~~ json
{
  "cache_intent": {
    "version": 1,
    "id": "retrieved-document",
    "constraints": {
      "retention": {"mode": "allow", "max_age": 600},
      "reuse": "approximate",
      "share_max": "tenant",
      "namespace": "document:8841:v42"
    },
    "hints": {
      "reuse_within": 120
    }
  }
}
~~~~

The service may still compute from scratch or use an exact hit.  If it uses
approximate state, that state and dependent state cannot be committed as
exact reusable state without establishing exact equivalence.

# Security Considerations {#security}

Cache reuse can reveal whether another request previously supplied related
input.  Implementations MUST enforce `share_max` as an authorization
boundary, not merely as a caller-selected cache-key convention.  Timing,
status fields, error messages, billing data, and management responses can
all reveal cache presence and require the same boundary.

A caller-supplied session, principal, tenant, namespace, group, or handle is
not proof of authorization.  The service or trusted gateway MUST bind these
values to authenticated identity and policy.  A caller MUST NOT be able to
select another tenant merely by naming it.

`public` permits cross-tenant reuse and therefore requires an explicit
application declaration.  A service MUST NOT infer public sharing from
frequency, popularity, content classification, or the absence of personal
data.

Approximate reuse can change model behavior.  Exact reuse is therefore the
required default unless the application explicitly permits approximation.
Approximate state and all dependent state MUST remain distinguishable from
exact state as specified in Section {{approximate-reuse}}.

Long reuse windows, many namespaces, and large numbers of fragments can
consume cache and metadata capacity.  Services SHOULD apply
authentication, quotas, rate limits, bounded metadata, and pressure
overrides.  Hints MUST NOT prevent normal scheduling progress or cause an
avoidable out-of-memory failure.

Capabilities and status can create new side channels.  Implementations
SHOULD return only the detail needed by the authorized caller and MAY
coarsen or delay reports.

# Privacy Considerations {#privacy}

Namespaces and fragment identifiers can become durable metadata.
Applications SHOULD use opaque values rather than user content, prompt
excerpts, email addresses, or other identifying information.  Services
SHOULD avoid placing these values in high-cardinality logs and metrics.

`retention.mode: forbid` applies to reusable input state, not to service
logs or other records.  A host protocol and service privacy policy must
separately describe those records.

Systems performing approximate matching may derive embeddings or other
indexes from input.  Those derived representations are cached input state
for purposes of retention and sharing unless a host protocol defines
stricter treatment.

# IANA Considerations {#iana}

This document has no IANA actions.

--- back

# Informative Mapping to Existing Mechanisms {#mapping}

This appendix is non-normative.

A trusted gateway may lower the data model into mechanisms such as:

* provider cache breakpoints or explicit cache objects;
* authenticated cache partitions or salts for `share_max` and `namespace`;
* engine-level retention, prefetch, or eviction hints for `reuse_within`;
  and
* provider usage fields or gateway telemetry for `cache_status`.

The lowering is deliberately asymmetric.  An engine hint may be ignored
under pressure, but an application constraint may not be weakened.  When a
backend cannot enforce a constraint, the gateway must bypass caching or
reject the cache intent rather than translate the constraint into a soft
hint.

Lowering `share_max` onto a per-request cache partition is lossy in a way
that is worth stating plainly.  An engine that accepts one partition value
per request can enforce only the effective policy of the whole request,
which is the narrowest boundary any fragment asked for.  The composition
rules in Section {{composition}} then hold, because narrowing is always
permitted, but the public prefix in Section {{example-public-first}} stops
being shared: every tenant recomputes it.  Recovering that sharing requires
the engine to admit a partition boundary that changes partway through the
input, so that blocks before the boundary carry one value and blocks after
it carry another.  An implementation measured against a per-request
mechanism recovered no reuse over a blanket tenant-scoped partition.

This document complements engine-level work on session-aware KV cache
management.  It does not replace engine APIs or prescribe their internal
selectors, block identifiers, queues, or storage tiers.

# Interoperability Test Cases {#test-cases}

An implementation profile should include at least the following tests:

1. A public fragment followed by a tenant fragment produces a public
   prefix and tenant-scoped downstream state.
2. A tenant fragment followed by a public fragment does not widen
   downstream state.
3. `retention.mode: forbid` propagates to dependent state.
4. `reuse: exact` prevents approximate reuse even when another contributor
   permits it.
5. Approximate state and its downstream state never enter the exact cache
   namespace.
6. An unknown constraint causes rejection or fresh no-retention fallback.
7. An unknown hint is ignored without weakening constraints.
8. A caller-provided tenant identifier cannot cross the authenticated
   tenant boundary.
9. A namespace change causes a deliberate miss without changing model
   input.
10. A gateway does not reorder model-visible fragments without an explicit
    equivalence declaration.

# Open Issues for Draft -01 {#open-issues}

This section is editorial and is to be removed before publication.  It lists
what is undecided, not what was decided; a question settled in this revision
is answered in the section that carries the rule, not recorded here.

One question remains, and it is the one evidence cannot settle, because the
answer is a choice of people: which community is the long-term home.

What can be said is where it is not and what would make the question ripe.
The only chartered IETF working group on artificial intelligence is AI
Preferences (aipref), and it is not this: it standardizes how a content owner
expresses preferences about collection and processing for model development,
and its charter puts authenticating or authorising clients, and technical
enforcement of preferences, out of scope.  Every constraint here is an
authorization boundary that an implementation must enforce, so the two
documents are near-opposites in subject.

The nearest work is an expired individual draft in the IRTF Network
Management Research Group analyzing system and network requirements for LLM
inference services, which covers prefix caching and KV cache reuse as
requirements rather than as an application-facing data model.  A research
group is the right maturity for a document with no independent
implementations, so that is the venue for discussion in the near term.

Two things would make a chartered working group the right ask instead: a
second independent implementation that interoperates with the first, and an
engine that can express a sharing boundary partway through the input.  The
second matters because until one exists, the half of this model with the most
value, a public prefix shared across tenants ahead of private state, cannot
be demonstrated on any engine, and a standards body would be chartering work
whose payoff nobody has shown.

# Acknowledgements {#acknowledgements}
{: numbered="false"}

TBD.
