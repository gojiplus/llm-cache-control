"""Reference implementation of the conservative composition rules.

The effective namespace digest lives in identity.py and is imported, not
reimplemented. Two copies of it agreed by luck for one commit and would
have drifted silently: the gateway keys its cache with one and the
conformance suite tested the other.

Implements Section "Conservative Composition" of
draft-sood-llm-cache-control-01.  Written to be read alongside the draft:
each rule below quotes the requirement it implements.

Standard library only, so the conformance suite has no dependency the
specification itself does not need.
"""

from typing import Dict, List, Optional, Sequence

from .identity import namespace_identity

# Containment order for the five scopes version 1 defines, narrowest first.
# The draft permits a host protocol to define additional scopes, including
# named groups, which makes this a partial order.  See intersect_share_max.
SHARE_LADDER: Sequence[str] = ("request", "session", "principal", "tenant", "public")

# Restrictiveness order for reuse, most restrictive first.
REUSE_ORDER: Sequence[str] = ("none", "exact", "approximate")


class IncomparableScopes(Exception):
    """Two sharing boundaries have no representable intersection.

    The draft requires the implementation to bypass reuse for the affected
    state rather than pick one, so this is an exception and not a value.
    """


def intersect_share_max(scopes: Sequence[str]) -> str:
    """ "The effective sharing boundary is the intersection of all
    contributing share_max boundaries."

    Over the version 1 ladder the scopes are totally ordered, so the
    intersection is the narrowest one.  A scope outside the ladder cannot be
    ordered against the others without host-protocol knowledge, so it raises
    rather than silently collapsing to a minimum.
    """
    unknown = [s for s in scopes if s not in SHARE_LADDER]
    if unknown:
        raise IncomparableScopes(f"scopes outside the version 1 ladder: {sorted(set(unknown))}")
    return min(scopes, key=SHARE_LADDER.index)


def compose_reuse(values: Sequence[str]) -> str:
    """ "Effective reuse is none if any contributor requires none; otherwise
    exact if any contributor requires exact; otherwise approximate."
    """
    return min(values, key=REUSE_ORDER.index)


def compose_max_age(ages: Sequence[Optional[int]]) -> Optional[int]:
    """ "If retention is allowed, the effective max_age is the smallest
    supplied bound.  An absent max_age contributes no upper bound."

    Absent means absent, not infinity and not zero: a fragment with no
    max_age must not tighten the result, so it is dropped before the min.
    """
    supplied = [a for a in ages if a is not None]
    return min(supplied) if supplied else None


def compose(fragments: List[Dict]) -> Dict:
    """Compose the constraints of an ordered list of contributing fragments.

    Each fragment is the `constraints` object of one cache intent.  Returns
    the effective policy for state that depends on all of them.
    """
    if not fragments:
        raise ValueError("composition requires at least one fragment")

    retentions = [f["retention"] for f in fragments]
    # "retention.mode is forbid if any contributing fragment forbids."
    forbidden = any(r["mode"] == "forbid" for r in retentions)

    effective: Dict = {"retention": {"mode": "forbid" if forbidden else "allow"}}
    if not forbidden:
        max_age = compose_max_age([r.get("max_age") for r in retentions])
        if max_age is not None:
            effective["retention"]["max_age"] = max_age

    effective["reuse"] = compose_reuse([f["reuse"] for f in fragments])
    effective["share_max"] = intersect_share_max([f["share_max"] for f in fragments])

    identity = namespace_identity([f["namespace"] for f in fragments if "namespace" in f])
    if identity is not None:
        effective["namespace_identity"] = identity
    return effective
