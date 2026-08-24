"""Unit tests for gateway internals.

The conformance suite proves the draft's requirements hold end to end.
These cover the pieces underneath it, where a bug would make the
conformance result a coincidence.
"""

import json

import pytest
from gateway import AuthContext, Fragment, Gateway
from gateway.auth import partition_for, strip_untrusted_scope
from gateway.backends import InMemoryBackend, NullBackend, SaltOnlyBackend
from gateway.compose import IncomparableScopes, compose
from gateway.errors import (
    DuplicateFragmentId,
    DuplicateMemberName,
    UnenforceableConstraint,
    UnsupportedVersion,
)
from gateway.identity import cache_key, namespace_identity, namespaces_equal
from gateway.parse import loads_strict, parse_cache_intent

AUTH = AuthContext(tenant="acme", principal="alice", session="s1")


def intent(**c):
    return json.dumps({"cache_intent": {"version": 1, "constraints": c}})


BASE = {"retention": {"mode": "allow"}, "reuse": "exact", "share_max": "tenant"}


class TestPartitions:
    def test_each_scope_maps_to_a_distinct_partition(self):
        seen = {
            partition_for(scope, AUTH, "r1")
            for scope in ("request", "session", "principal", "tenant", "public")
        }
        assert len(seen) == 5

    def test_narrower_scopes_nest_inside_wider_ones(self):
        tenant = partition_for("tenant", AUTH, "r1")
        principal = partition_for("principal", AUTH, "r1")
        session = partition_for("session", AUTH, "r1")
        assert principal.startswith(tenant)
        assert session.startswith(principal)

    def test_public_is_shared_across_tenants(self):
        other = AuthContext(tenant="other", principal="bob", session="s2")
        assert partition_for("public", AUTH, "r1") == partition_for("public", other, "r2")

    def test_tenant_partition_differs_per_tenant(self):
        other = AuthContext(tenant="other", principal="bob", session="s2")
        assert partition_for("tenant", AUTH, "r1") != partition_for("tenant", other, "r2")

    def test_request_scope_differs_per_request(self):
        assert partition_for("request", AUTH, "r1") != partition_for("request", AUTH, "r2")

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError):
            partition_for("everyone", AUTH, "r1")


class TestStripUntrustedScope:
    def test_strips_every_known_coordinate(self):
        cleaned, stripped = strip_untrusted_scope(
            {"tenant": "x", "principal": "y", "session": "z", "prompt": "keep"}
        )
        assert cleaned == {"prompt": "keep"}
        assert set(stripped) == {"tenant", "principal", "session"}

    def test_leaves_unrelated_fields_alone(self):
        cleaned, stripped = strip_untrusted_scope({"prompt": "hello"})
        assert cleaned == {"prompt": "hello"}
        assert stripped == ()


class TestParse:
    def test_duplicate_member_rejected_at_any_depth(self):
        with pytest.raises(DuplicateMemberName):
            loads_strict('{"a": {"b": 1, "b": 2}}')

    def test_unsupported_version_rejected(self):
        doc = json.dumps({"cache_intent": {"version": 2, "constraints": BASE}})
        with pytest.raises(UnsupportedVersion):
            parse_cache_intent(doc, Gateway().validator)

    def test_valid_document_round_trips(self):
        parsed = parse_cache_intent(intent(**BASE), Gateway().validator)
        assert parsed["constraints"]["share_max"] == "tenant"

    def test_missing_version_is_invalid_not_unsupported(self):
        from jsonschema import ValidationError

        doc = json.dumps({"cache_intent": {"constraints": BASE}})
        with pytest.raises(ValidationError):
            parse_cache_intent(doc, Gateway().validator)


class TestIdentity:
    def test_namespace_comparison_is_codepoint_exact(self):
        assert not namespaces_equal("café", "café")
        assert namespaces_equal("café", "café")

    def test_length_prefix_prevents_concatenation_collision(self):
        assert namespace_identity(["ab", "c"]) != namespace_identity(["a", "bc"])

    def test_namespace_order_matters(self):
        assert namespace_identity(["a", "b"]) != namespace_identity(["b", "a"])

    def test_no_namespaces_yields_no_identity(self):
        assert namespace_identity([]) is None

    def test_exactness_partitions_the_keyspace(self):
        ex = {"model": "m", "tokenizer_rev": "1"}
        assert cache_key(ex, ["c"], ["n"], "p", exact=True) != cache_key(
            ex, ["c"], ["n"], "p", exact=False
        )

    def test_partition_is_part_of_the_key(self):
        ex = {"model": "m", "tokenizer_rev": "1"}
        assert cache_key(ex, ["c"], ["n"], "tenant:a", True) != cache_key(
            ex, ["c"], ["n"], "tenant:b", True
        )


class TestCompose:
    def test_forbid_beats_allow(self):
        out = compose([{**BASE, "retention": {"mode": "forbid"}}, BASE])
        assert out["retention"]["mode"] == "forbid"

    def test_forbid_drops_max_age(self):
        out = compose([{**BASE, "retention": {"mode": "forbid"}}])
        assert "max_age" not in out["retention"]

    def test_smallest_max_age_wins(self):
        out = compose(
            [
                {**BASE, "retention": {"mode": "allow", "max_age": 100}},
                {**BASE, "retention": {"mode": "allow", "max_age": 50}},
            ]
        )
        assert out["retention"]["max_age"] == 50

    def test_absent_max_age_contributes_no_bound(self):
        out = compose([{**BASE, "retention": {"mode": "allow", "max_age": 100}}, BASE])
        assert out["retention"]["max_age"] == 100

    def test_reuse_takes_the_most_restrictive(self):
        assert compose([{**BASE, "reuse": "approximate"}, {**BASE, "reuse": "none"}])["reuse"] == (
            "none"
        )

    def test_share_max_intersects_to_the_narrowest(self):
        out = compose([{**BASE, "share_max": "public"}, {**BASE, "share_max": "session"}])
        assert out["share_max"] == "session"

    def test_incomparable_scopes_raise_rather_than_guess(self):
        with pytest.raises(IncomparableScopes):
            compose([{**BASE, "share_max": "group:eng"}, BASE])

    def test_empty_composition_is_an_error(self):
        with pytest.raises(ValueError):
            compose([])


class TestBackends:
    @pytest.mark.parametrize(
        "backend", [InMemoryBackend(), SaltOnlyBackend(), NullBackend()], ids=lambda b: b.name
    )
    def test_capabilities_declare_every_field_the_gateway_checks(self, backend):
        caps = backend.capabilities()
        assert set(caps) == {
            "retention_modes",
            "retention_max_age",
            "reuse",
            "share_max",
            "namespace",
            "hints",
            "per_fragment_share",
        }

    def test_null_backend_never_returns_state(self):
        b = NullBackend()
        b.put("k", 0.0, None, True)
        assert b.get("k", 0.0) is None

    def test_salt_lowering_separates_partitions(self):
        b = SaltOnlyBackend()
        assert b.salt_for("tenant:a", "ns1") != b.salt_for("tenant:b", "ns1")
        assert b.salt_for("tenant:a", "ns1") != b.salt_for("tenant:a", "ns2")

    def test_in_memory_expiry_is_not_extended_by_reads(self):
        b = InMemoryBackend()
        b.put("k", 0.0, 10, True)
        assert b.get("k", 5.0) is not None
        assert b.get("k", 11.0) is None

    def test_namespace_capability_is_checked_after_composition(self):
        class NoNamespaceBackend(InMemoryBackend):
            def capabilities(self):
                caps = super().capabilities()
                caps["namespace"] = False
                return caps

        frag = Fragment("f", "X", intent(**{**BASE, "namespace": "agent:v1"}))
        with pytest.raises(UnenforceableConstraint):
            Gateway(backend=NoNamespaceBackend(), on_unenforceable="reject").handle(
                [frag], AUTH, now=0.0
            )


class TestGatewayEndToEnd:
    def test_forbid_retains_nothing(self):
        b = InMemoryBackend()
        g = Gateway(backend=b)
        frag = Fragment("f", "X", intent(**{**BASE, "retention": {"mode": "forbid"}}))
        result = g.handle([frag], AUTH, now=0.0)
        assert result.statuses[0].retention == "forbidden"
        assert len(b.keys()) == 0

    def test_request_scope_publishes_nothing_for_later_requests(self):
        b = InMemoryBackend()
        g = Gateway(backend=b)
        frag = Fragment("f", "X", intent(**{**BASE, "share_max": "request"}))
        assert g.handle([frag], AUTH, now=0.0).statuses[0].retention == "declined"
        assert len(b.keys()) == 0

    def test_reuse_none_bypasses_the_lookup_but_still_retains(self):
        """`miss` claims a lookup happened. Under reuse: none none does.

        The draft reserves `miss` for a lookup that found nothing, so a
        warming request (reuse: none with retention: allow) reports `bypass`.
        Reporting `miss` would make a healthy cache look cold to a caller
        measuring hit rate, which is the error the draft already forbids for
        `unobserved`.
        """
        g = Gateway()
        frag = Fragment("f", "X", intent(**{**BASE, "reuse": "none"}))
        g.handle([frag], AUTH, now=0.0, request_id="r1")
        second = g.handle([frag], AUTH, now=1.0, request_id="r2")
        assert second.statuses[0].outcome == "bypass"
        assert second.statuses[0].retention == "retained"

        # Control: the state really was retained and really is reusable, so
        # the bypass above is the reuse constraint and not a dead cache.
        reader = Fragment("f", "X", intent(**BASE))
        third = g.handle([reader], AUTH, now=2.0, request_id="r3")
        assert third.statuses[0].outcome == "exact_hit"

    def test_expiry_boundary_instant_is_exclusive(self):
        """`now >= materialized_at + max_age` means the bound instant is dead.

        The draft fixes the comparison because two implementations that
        disagree about the boundary instant disagree about whether an entry
        exists, and neither can reuse the other's cache at the edge.
        """
        from gateway.cache import InMemoryCache

        c = InMemoryCache()
        c.put("k", now=0.0, max_age=10, exact=True)
        assert c.get("k", now=9.999) is not None
        assert c.get("k", now=10.0) is None

    def test_max_age_zero_permits_no_cross_request_reuse(self):
        """max_age: 0 is legal in the schema and means what forbid means."""
        g = Gateway()
        frag = Fragment("f", "X", intent(**{**BASE, "retention": {"mode": "allow", "max_age": 0}}))
        g.handle([frag], AUTH, now=0.0, request_id="r1")
        second = g.handle([frag], AUTH, now=0.001, request_id="r2")
        assert second.statuses[0].outcome == "miss"

        # Control: the same sequence with a real bound does hit, so the miss
        # above is the zero and not a cache that never stores anything.
        g2 = Gateway()
        ok = Fragment("f", "X", intent(**{**BASE, "retention": {"mode": "allow", "max_age": 60}}))
        g2.handle([ok], AUTH, now=0.0, request_id="r1")
        assert g2.handle([ok], AUTH, now=0.001, request_id="r2").statuses[0].outcome == "exact_hit"

    def test_duplicate_fragment_ids_are_rejected(self):
        """Section "Identifier": ids are unique within one request."""
        g = Gateway()
        dupe = [Fragment("same", "A", intent(**BASE)), Fragment("same", "B", intent(**BASE))]
        with pytest.raises(DuplicateFragmentId):
            g.handle(dupe, AUTH, now=0.0)

        # Control: distinct ids over the same contents are fine.
        fine = [Fragment("a", "A", intent(**BASE)), Fragment("b", "B", intent(**BASE))]
        assert len(g.handle(fine, AUTH, now=0.0).statuses) == 2

    def test_fragment_without_intent_is_bypassed(self):
        g = Gateway()
        result = g.handle([Fragment("f", "X", None)], AUTH, now=0.0)
        assert result.statuses[0].outcome == "bypass"

    def test_status_object_matches_the_draft_shape(self):
        g = Gateway()
        result = g.handle([Fragment("f", "X", intent(**BASE))], AUTH, now=0.0)
        entry = result.as_cache_status()["cache_status"][0]
        assert set(entry) == {
            "id",
            "outcome",
            "effective_share",
            "retention",
            "constraint_result",
        }

    def test_on_unenforceable_rejects_bad_value(self):
        with pytest.raises(ValueError):
            Gateway(on_unenforceable="downgrade")


class TestFragmentInheritance:
    """A fragment with no cache_intent inherits what precedes it.

    Binding section 2 and the draft's composition model both require this:
    the state such a fragment produces depends on the earlier fragments, so
    it carries their effective policy. Reporting `request` instead would be
    safe but wrong, and would decline retention the intent allowed.
    """

    def test_bare_fragment_inherits_the_preceding_boundary(self):
        g = Gateway()
        frags = [
            Fragment("sys", "SYSTEM", intent(**{**BASE, "share_max": "tenant"})),
            Fragment("user", "USER", None),
        ]
        statuses = {s.id: s for s in g.handle(frags, AUTH, now=0.0).statuses}
        assert statuses["user"].effective_share == "tenant"
        assert statuses["user"].outcome != "bypass"

    def test_bare_fragment_inherits_forbid(self):
        b = InMemoryBackend()
        g = Gateway(backend=b)
        frags = [
            Fragment("sys", "SYSTEM", intent(**{**BASE, "retention": {"mode": "forbid"}})),
            Fragment("user", "USER", None),
        ]
        statuses = {s.id: s for s in g.handle(frags, AUTH, now=0.0).statuses}
        assert statuses["user"].retention == "forbidden"
        assert len(b.keys()) == 0

    def test_bare_fragment_inherits_the_narrowest_of_several(self):
        g = Gateway()
        frags = [
            Fragment("a", "A", intent(**{**BASE, "share_max": "public"})),
            Fragment("b", "B", intent(**{**BASE, "share_max": "session"})),
            Fragment("c", "C", None),
        ]
        statuses = {s.id: s for s in g.handle(frags, AUTH, now=0.0).statuses}
        assert statuses["c"].effective_share == "session"

    def test_leading_bare_fragment_has_nothing_to_inherit(self):
        g = Gateway()
        frags = [Fragment("user", "USER", None), Fragment("sys", "S", intent(**BASE))]
        statuses = {s.id: s for s in g.handle(frags, AUTH, now=0.0).statuses}
        assert statuses["user"].outcome == "bypass"
        assert statuses["user"].effective_share == "request"
