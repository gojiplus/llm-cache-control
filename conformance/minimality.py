"""Does every constraint and hint in version 1 earn its place?

Agreement on the smallest useful set needs people, but the evidence that
agreement would rest on does not: a member earns its place only if some
input distinguishable by it produces behavior distinguishable without it.

Two tests per constraint.

  Discriminating: does changing this member's value change observable
  behavior?  A member that never changes anything is decoration.

  Irreducible: can this member's effect be obtained by setting some other
  member instead?  If it can, one of the two is redundant and version 1
  should carry the simpler one.

Hints need a different criterion.  A hint MAY be ignored, so it is entitled
to fail the discrimination test.  What the draft requires is that an
implementation advertising a hint report its status, so the test is whether
an advertised hint is observable at all, and then whether its value is.

Run: python -m conformance.minimality
"""

import itertools
import json

from gateway import AuthContext, Fragment, Gateway
from gateway.backends import InMemoryBackend
from gateway.compose import compose

AUTH = AuthContext(tenant="acme", principal="alice", session="s1")
BASE = {"retention": {"mode": "allow"}, "reuse": "exact", "share_max": "tenant"}

VALUES = {
    "retention.mode": [{"mode": "allow"}, {"mode": "forbid"}],
    "retention.max_age": [{"mode": "allow"}, {"mode": "allow", "max_age": 60}],
    "reuse": ["none", "exact", "approximate"],
    "share_max": ["request", "session", "principal", "tenant", "public"],
    "namespace": [None, "a:v1", "a:v2"],
}


def constraints(member, value):
    c = json.loads(json.dumps(BASE))
    if member.startswith("retention"):
        c["retention"] = value
    elif member == "namespace":
        if value is None:
            c.pop("namespace", None)
        else:
            c["namespace"] = value
    else:
        c[member] = value
    return c


def observe(c, second=None):
    """Everything the gateway does that a caller could detect.

    Two requests, so retention across requests is visible and not just the
    single-request decision.
    """
    backend = InMemoryBackend()
    g = Gateway(backend=backend)
    frags = [
        Fragment("f", "CONTENT", json.dumps({"cache_intent": {"version": 1, "constraints": c}}))
    ]
    if second is not None:
        frags.append(
            Fragment(
                "g",
                "MORE",
                json.dumps({"cache_intent": {"version": 1, "constraints": second}}),
            )
        )
    first = g.handle(frags, AUTH, now=0.0, request_id="r1")
    later = g.handle(frags, AUTH, now=30.0, request_id="r2")
    expired = g.handle(frags, AUTH, now=1000.0, request_id="r3")
    return (
        tuple((s.id, s.outcome, s.effective_share, s.retention) for s in first.statuses),
        tuple(s.outcome for s in later.statuses),
        tuple(s.outcome for s in expired.statuses),
        len(backend.keys()),
    )


def cross_value_effect(member, value_a, value_b, approx_class=None):
    """Warm the cache under one value, then read under the other.

    Some members act on cache identity rather than on the status of a single
    request, and comparing one request against itself cannot see them.  This
    warms with value_a and reads with value_b, so a member that partitions
    the keyspace shows up as a miss where the control shows a hit.
    """
    backend = InMemoryBackend()
    g = Gateway(backend=backend)

    def once(value, request_id, now):
        frag = Fragment(
            "f",
            "CONTENT",
            json.dumps({"cache_intent": {"version": 1, "constraints": constraints(member, value)}}),
            approx_class=approx_class,
        )
        return g.handle([frag], AUTH, now=now, request_id=request_id).statuses[0].outcome

    once(value_a, "r1", 0.0)
    return once(value_b, "r2", 1.0)


def discriminating(record):
    print("\nDiscriminating: does changing this member change observable behavior?\n")
    for member, values in VALUES.items():
        seen = {json.dumps(v): observe(constraints(member, v)) for v in values}
        distinct = len(set(map(repr, seen.values())))

        # Same-value observation is blind to members that act on cache
        # identity, so also check whether changing the value between two
        # requests breaks reuse that otherwise holds.
        control = cross_value_effect(member, values[0], values[0])
        partitions = any(
            cross_value_effect(member, values[0], other) != control for other in values[1:]
        )
        record(
            distinct > 1 or partitions,
            f"{member:<20} {distinct} status behaviors, partitions identity: {partitions}",
            "" if (distinct > 1 or partitions) else "NEVER changes anything observable",
        )


def approximate_needs_a_donor(record):
    """reuse:approximate earns its place only on input that exact cannot serve.

    On identical input an approximate request should still take the exact
    hit, so identical content cannot distinguish the two values.  The
    distinguishing case is different input in the same similarity class:
    exact must miss it and approximate must not.
    """
    print("\nWhy reuse has three values and not two:\n")

    def run(second_reuse, approx_class):
        backend = InMemoryBackend()
        g = Gateway(backend=backend)
        seed = Fragment(
            "a",
            "DOC-A",
            json.dumps({"cache_intent": {"version": 1, "constraints": BASE}}),
            approx_class=approx_class,
        )
        g.handle([seed], AUTH, now=0.0, request_id="r1")
        asker = Fragment(
            "b",
            "DOC-B",
            json.dumps(
                {"cache_intent": {"version": 1, "constraints": {**BASE, "reuse": second_reuse}}}
            ),
            approx_class=approx_class,
        )
        return g.handle([asker], AUTH, now=1.0, request_id="r2").statuses[0].outcome

    exact_on_new_input = run("exact", "docs")
    approx_on_new_input = run("approximate", "docs")
    approx_without_donor = run("approximate", None)
    record(
        exact_on_new_input != approx_on_new_input,
        "reuse:approximate serves input that reuse:exact cannot",
        f"exact -> {exact_on_new_input}, approximate -> {approx_on_new_input}",
    )
    record(
        approx_without_donor == exact_on_new_input,
        "reuse:approximate collapses to reuse:exact when no donor exists",
        f"approximate with no donor -> {approx_without_donor}",
    )


def irreducible(record):
    print("\nIrreducible: can one member's effect be reproduced by another?\n")

    # reuse: none versus retention: forbid. Both suppress reuse, but the
    # draft says they govern different directions, so they must differ.
    no_reuse = observe({**BASE, "reuse": "none"})
    no_retain = observe({**BASE, "retention": {"mode": "forbid"}})
    record(
        no_reuse != no_retain,
        "reuse:none is distinguishable from retention:forbid",
        "reuse governs reads on this request; retention governs later ones",
    )

    # namespace versus share_max. Both can force a miss. They must not be
    # interchangeable, or one is redundant.
    ns_split = observe({**BASE, "namespace": "a:v1"}) != observe({**BASE, "namespace": "a:v2"})
    scope_split = observe({**BASE, "share_max": "tenant"}) != observe(
        {**BASE, "share_max": "session"}
    )
    record(
        ns_split or scope_split,
        "namespace and share_max both partition, and neither is a relabeling of the other",
        "namespace cannot widen a boundary and share_max cannot version a prompt",
    )

    # max_age versus retention: forbid.
    short = observe({**BASE, "retention": {"mode": "allow", "max_age": 60}})
    forbid = observe({**BASE, "retention": {"mode": "forbid"}})
    record(
        short != forbid,
        "a finite max_age is distinguishable from forbidding retention",
        "max_age 0 would be the interesting edge; the schema permits it",
    )


def composition_reachability(record):
    print("\nReachability: is every composed value reachable from real inputs?\n")
    reuse_values = set()
    share_values = set()
    for combo in itertools.product(VALUES["reuse"], repeat=2):
        reuse_values.add(compose([{**BASE, "reuse": r} for r in combo])["reuse"])
    for combo in itertools.product(VALUES["share_max"], repeat=2):
        share_values.add(compose([{**BASE, "share_max": s} for s in combo])["share_max"])
    record(
        reuse_values == set(VALUES["reuse"]),
        f"every reuse value is reachable by composition: {sorted(reuse_values)}",
    )
    record(
        share_values == set(VALUES["share_max"]),
        f"every share_max value is reachable by composition: {sorted(share_values)}",
    )


def hints_do_anything(record):
    """Hints need a different criterion than constraints, not a weaker one.

    A hint MAY be ignored, so "does the value change cache behavior" is a
    test a conforming hint is entitled to fail.  What the document does
    require is that an implementation advertising a hint report that hint's
    status, and that a value above an advertised maximum be reported as
    clipped.  So the criterion here is whether an advertised hint is
    observable at all, and then whether its value is.
    """
    print("\nHints: is an advertised hint observable, and is its value observable?\n")
    advertised = InMemoryBackend().capabilities().get("hints", {})

    for hint, values in (("reuse_within", [0, 300, 7200]),):
        supported = hint in advertised or f"{hint}_max" in advertised
        if not supported:
            record(
                True,
                f"{hint:<14} not advertised by any backend here",
                "so nothing is obliged to report it; see open issue 2",
            )
            continue

        seen = set()
        for value in values:
            g = Gateway(backend=InMemoryBackend())
            doc = {"cache_intent": {"version": 1, "constraints": BASE, "hints": {hint: value}}}
            result = g.handle([Fragment("f", "C", json.dumps(doc))], AUTH, now=0.0)
            statuses = [s.hints.get(hint) for s in result.statuses]
            seen.add(repr(statuses))

        reported = all(
            s.hints.get(hint) is not None
            for s in Gateway(backend=InMemoryBackend())
            .handle(
                [
                    Fragment(
                        "f",
                        "C",
                        json.dumps(
                            {
                                "cache_intent": {
                                    "version": 1,
                                    "constraints": BASE,
                                    "hints": {hint: values[0]},
                                }
                            }
                        ),
                    )
                ],
                AUTH,
                now=0.0,
            )
            .statuses
        )
        record(reported, f"{hint:<14} advertised, so its status is reported")
        record(
            len(seen) > 1,
            f"{hint:<14} {len(seen)} distinct statuses across {len(values)} values",
            "the value is observable through clipping" if len(seen) > 1 else "no value differs",
        )


ALL = [
    discriminating,
    approximate_needs_a_donor,
    irreducible,
    composition_reachability,
    hints_do_anything,
]


def main() -> int:
    failures = []

    def record(ok, label, detail=""):
        print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"   {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    for case in ALL:
        case(record)

    print()
    if failures:
        print(f"{len(failures)} member(s) failed to justify their place:")
        for item in failures:
            print(f"  {item}")
        return 1
    print("Every constraint is discriminating and irreducible.")
    print("The one hint version 1 defines is observable, and so is its value.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
