"""Behavioral conformance: the requirements no data vector can express.

Draft appendix tests 5, 8, and 10 constrain what an implementation does, not
what a document contains, so they need an implementation to test.  The same
goes for the three requirements JSON Schema cannot express.  All six run
against the reference gateway.

Also here: the lowering matrix.  The draft's central safety property is that
a backend which cannot enforce a constraint must cause a bypass or a
rejection, never a quiet downgrade.  A gateway with one all-capable backend
cannot demonstrate that, so the matrix runs every constraint against
backends of deliberately differing capability.
"""

import json

from gateway import AuthContext, Fragment, Gateway
from gateway.backends import InMemoryBackend, NullBackend, SaltOnlyBackend
from gateway.errors import DuplicateMemberName, ReorderNotPermitted, UnenforceableConstraint
from gateway.identity import is_normalization_collision, namespaces_equal
from gateway.parse import loads_strict
from gateway.vllm_backend import VLLMBackend

ACME = AuthContext(tenant="acme", principal="alice", session="s1")
OTHER = AuthContext(tenant="other-corp", principal="mallory", session="s2")


def intent(**constraints):
    return json.dumps({"cache_intent": {"version": 1, "constraints": constraints}})


def allow(**over):
    c = {"retention": {"mode": "allow"}, "reuse": "exact", "share_max": "tenant"}
    c.update(over)
    return c


def test_5_approximate_never_enters_exact_namespace(record):
    """Draft test 5.

    The sequence that matters: exact state is created, a later request with
    *different* input reuses it approximately, and the state that request
    produces must not then be servable to anyone asking for exact reuse.
    """
    backend = InMemoryBackend()
    g = Gateway(backend=backend)
    ns = "doc:v1"

    seed = Fragment("a", "DOC-A", intent(**allow(namespace=ns)), approx_class="docs")
    first = g.handle([seed], ACME, now=0.0, request_id="r1")
    record(first.statuses[0].outcome == "miss", "test 5: the seeding request computes fresh")

    borrower = Fragment(
        "b", "DOC-B", intent(**allow(reuse="approximate", namespace=ns)), approx_class="docs"
    )
    second = g.handle([borrower], ACME, now=1.0, request_id="r2")
    record(
        second.statuses[0].outcome == "approximate_hit",
        "test 5: different input reuses the seeded state approximately",
        f"outcome was {second.statuses[0].outcome}",
    )
    record(
        second.statuses[0].exact is False,
        "test 5: state derived from an approximate hit is marked not exact",
    )

    exact_asker = Fragment("b", "DOC-B", intent(**allow(reuse="exact", namespace=ns)))
    third = g.handle([exact_asker], ACME, now=2.0, request_id="r3")
    record(
        third.statuses[0].outcome == "miss",
        "test 5: an exact request for the same input does NOT get the approximate state",
        f"outcome was {third.statuses[0].outcome}",
    )

    # And the seeded exact state is still exactly reusable, so the isolation
    # above is not just a blanket cache failure.
    fourth = g.handle([seed], ACME, now=3.0, request_id="r4")
    record(
        fourth.statuses[0].outcome == "exact_hit",
        "test 5: untainted exact state remains exactly reusable",
        f"outcome was {fourth.statuses[0].outcome}",
    )


def test_8_caller_cannot_cross_tenant_boundary(record):
    """Draft test 8."""
    backend = InMemoryBackend()
    g = Gateway(backend=backend)
    frag = Fragment("sys", "SYSTEM", intent(**allow(share_max="tenant", namespace="agent:v1")))

    g.handle([frag], ACME, now=0.0, request_id="r1")
    # A different authenticated tenant sends byte-identical input while
    # claiming, in the request body, to be the first tenant.
    payload = {"tenant": "acme", "principal": "alice", "cache_partition": "tenant:acme"}
    result = g.handle([frag], OTHER, now=1.0, request_id="r2", request_payload=payload)
    record(
        result.statuses[0].outcome == "miss",
        "test 8: a caller naming another tenant does not reach its state",
        f"outcome was {result.statuses[0].outcome}",
    )
    record(
        set(payload) == set(result.stripped_scope_fields),
        "test 8: caller-supplied scope coordinates are stripped",
        f"stripped {result.stripped_scope_fields}",
    )
    # And the original tenant still hits, so the isolation is not just a
    # blanket cache failure.
    again = g.handle([frag], ACME, now=2.0, request_id="r3")
    record(
        again.statuses[0].outcome == "exact_hit",
        "test 8: the authorized tenant still reuses its own state",
        f"outcome was {again.statuses[0].outcome}",
    )


def test_10_no_reordering_without_equivalence(record):
    """Draft test 10."""
    g = Gateway()
    frags = [
        Fragment("sys", "SYSTEM", intent(**allow(share_max="tenant"))),
        Fragment("tools", "TOOLS", intent(**allow(share_max="public"))),
    ]
    result = g.handle(frags, ACME, now=0.0)
    record(
        result.order == ["sys", "tools"],
        "test 10: fragment order is preserved by default",
        f"order was {result.order}",
    )
    try:
        g.reorder_for_reuse(frags)
        record(False, "test 10: reordering without a declaration is refused")
    except ReorderNotPermitted:
        record(True, "test 10: reordering without a declaration is refused")
    declared = g.handle(frags, ACME, now=1.0, request_id="r2", equivalence_declared=True)
    record(
        declared.order == ["tools", "sys"],
        "test 10: reordering happens only behind an explicit declaration",
        f"order was {declared.order}",
    )


def test_prose_duplicate_member_names(record):
    """Rejection of duplicate JSON member names."""
    text = '{"cache_intent": {"version": 1, "constraints": {"share_max": "tenant", '
    text += '"share_max": "public", "retention": {"mode": "allow"}, "reuse": "exact"}}}'
    record(
        json.loads(text)["cache_intent"]["constraints"]["share_max"] == "public",
        "prose: stdlib json silently keeps the last duplicate, which is the hazard",
    )
    try:
        loads_strict(text)
        record(False, "prose: duplicate member names are rejected")
    except DuplicateMemberName:
        record(True, "prose: duplicate member names are rejected")


def test_prose_exact_namespace_comparison(record):
    """Exact comparison, and no Unicode normalization before it."""
    nfc = "café:v1"
    nfd = "café:v1"
    record(not namespaces_equal(nfc, nfd), "prose: namespace comparison is exact")
    record(
        is_normalization_collision(nfc, nfd),
        "prose: the two test values really do collide under NFC, so the check is live",
    )
    backend = InMemoryBackend()
    g = Gateway(backend=backend)
    g.handle([Fragment("f", "X", intent(**allow(namespace=nfc)))], ACME, now=0.0, request_id="r1")
    result = g.handle(
        [Fragment("f", "X", intent(**allow(namespace=nfd)))], ACME, now=1.0, request_id="r2"
    )
    record(
        result.statuses[0].outcome == "miss",
        "prose: normalization-equivalent namespaces do not match",
        f"outcome was {result.statuses[0].outcome}",
    )


def lowering_matrix(record):
    """A backend that cannot enforce a constraint must bypass or reject."""
    cases = [
        ("max_age", allow(retention={"mode": "allow", "max_age": 600})),
        ("approximate reuse", allow(reuse="approximate")),
    ]
    for label, constraints in cases:
        frag = Fragment("f", "X", intent(**constraints))

        bypassing = Gateway(backend=SaltOnlyBackend(), on_unenforceable="bypass")
        result = bypassing.handle([frag], ACME, now=0.0)
        record(
            result.statuses[0].outcome == "bypass",
            f"lowering: salt-only backend bypasses on {label}",
            f"outcome was {result.statuses[0].outcome}",
        )
        record(
            result.statuses[0].retention == "declined",
            f"lowering: bypass on {label} retains nothing",
        )

        rejecting = Gateway(backend=SaltOnlyBackend(), on_unenforceable="reject")
        try:
            rejecting.handle([frag], ACME, now=0.0)
            record(False, f"lowering: rejecting gateway raises on {label}")
        except UnenforceableConstraint:
            record(True, f"lowering: rejecting gateway raises on {label}")

        capable = Gateway(backend=InMemoryBackend())
        ok = capable.handle([frag], ACME, now=0.0)
        record(
            ok.statuses[0].outcome != "bypass",
            f"lowering: a capable backend does not bypass on {label}",
            "otherwise the bypass above would prove nothing",
        )

    # The fallback backend satisfies everything by caching nothing.
    for label, constraints in cases + [("forbid", allow(retention={"mode": "forbid"}))]:
        frag = Fragment("f", "X", intent(**constraints))
        result = Gateway(backend=NullBackend()).handle([frag], ACME, now=0.0)
        record(
            result.statuses[0].outcome == "miss",
            f"lowering: null backend is constraint-preserving for {label}",
            f"outcome was {result.statuses[0].outcome}",
        )

    # The vLLM backend must bypass exactly where the salt-only model says it
    # will, since salt-only exists to predict what a real engine can enforce.
    vllm = VLLMBackend("http://127.0.0.1:0", "test-model")
    record(
        vllm.capabilities() == SaltOnlyBackend().capabilities(),
        "lowering: the vLLM backend's capabilities match the salt-only model of it",
    )
    for label, constraints in cases:
        frag = Fragment("f", "X", intent(**constraints))
        result = Gateway(backend=vllm).handle([frag], ACME, now=0.0)
        record(
            result.statuses[0].outcome == "bypass",
            f"lowering: vLLM backend bypasses on {label} without contacting the server",
            f"outcome was {result.statuses[0].outcome}",
        )

    # A gateway must never advertise more than its backend can do.
    for backend in (InMemoryBackend(), SaltOnlyBackend(), NullBackend()):
        g = Gateway(backend=backend)
        record(
            g.capabilities == backend.capabilities(),
            f"lowering: {backend.name} gateway advertises exactly its backend's capabilities",
        )


def test_max_age_is_not_extended_by_reads(record):
    """A read must not push the expiry out."""
    backend = InMemoryBackend()
    g = Gateway(backend=backend)
    frag = Fragment("f", "X", intent(**allow(retention={"mode": "allow", "max_age": 100})))
    g.handle([frag], ACME, now=0.0, request_id="r1")
    hit = g.handle([frag], ACME, now=50.0, request_id="r2")
    record(hit.statuses[0].outcome == "exact_hit", "retention: state is reusable inside max_age")
    expired = g.handle([frag], ACME, now=101.0, request_id="r3")
    record(
        expired.statuses[0].outcome == "miss",
        "retention: a read at t=50 did not extend the bound past t=100",
        f"outcome was {expired.statuses[0].outcome}",
    )


def test_emitted_status_validates_against_the_schema(record):
    """The gateway's own output must satisfy the status schema.

    Closes the loop: the schemas describe documents this repository can
    actually produce, not just documents it can parse.
    """
    import json as _json
    import pathlib as _pathlib

    from jsonschema import Draft202012Validator

    root = _pathlib.Path(__file__).resolve().parent.parent
    validator = Draft202012Validator(
        _json.loads((root / "llm-cache-status.schema.json").read_text())
    )
    g = Gateway()
    frag = Fragment("f", "X", intent(**allow(retention={"mode": "allow", "max_age": 3600})))
    doc = g.handle([frag], ACME, now=0.0).as_cache_status()
    errors = list(validator.iter_errors(doc))
    record(
        not errors,
        "round trip: emitted cache_status validates against llm-cache-status.schema.json",
        "" if not errors else errors[0].message[:80],
    )


def test_retained_state_keeps_its_own_sharing_boundary(record):
    """Section "Retained State Carries Its Own Policy".

    A request cannot reach state retained under a narrower boundary by
    declaring a wider one.  Without this, `share_max` is advisory: declaring
    `tenant` would buy nothing, because any later request could name `public`
    and read it anyway.
    """
    backend = InMemoryBackend()
    g = Gateway(backend=backend)
    content = "SHARED-PREFIX"

    writer = Fragment("w", content, intent(**allow(share_max="tenant")))
    g.handle([writer], ACME, now=0.0, request_id="r1")

    # Same tenant, same bytes, but asking for a wider boundary than the one
    # the state was retained under.
    widener = Fragment("x", content, intent(**allow(share_max="public")))
    wide = g.handle([widener], ACME, now=1.0, request_id="r2")
    record(
        wide.statuses[0].outcome != "exact_hit",
        "retained policy: share_max: public does not reach state retained under tenant",
        f"outcome was {wide.statuses[0].outcome}",
    )

    # Control: the tenant-scoped state really is there and really is
    # reusable, so the line above is the boundary and not an empty cache.
    same = g.handle([writer], ACME, now=2.0, request_id="r3")
    record(
        same.statuses[0].outcome == "exact_hit",
        "retained policy: the same boundary still reuses the state (control)",
        f"outcome was {same.statuses[0].outcome}",
    )


def test_status_has_one_entry_per_governed_fragment(record):
    """Section "Status Reporting": correlation is positional.

    A caller aligns the array with the request it sent.  Dropping entries for
    fragments that supplied no `id` would break that alignment silently.
    """
    g = Gateway()
    frags = [
        Fragment("first", "A", intent(**allow())),
        Fragment(None, "B", intent(**allow())),
        Fragment("third", "C", intent(**allow())),
    ]
    result = g.handle(frags, ACME, now=0.0)
    record(
        len(result.statuses) == 3,
        "status: one entry per governed fragment, including one with no id",
        f"got {len(result.statuses)} entries for 3 fragments",
    )
    record(
        [s.id for s in result.statuses] == ["first", None, "third"],
        "status: entries stay in fragment order so positional correlation works",
        f"ids were {[s.id for s in result.statuses]}",
    )
    entries = result.as_cache_status()["cache_status"]
    record(
        "id" not in entries[1],
        "status: the entry for a fragment with no id omits the member rather than inventing one",
    )


def test_absent_capability_member_means_unsupported(record):
    """Section "Capability Discovery": absent means no, not unstated.

    The gateway advertises exactly what its backend can enforce, and refuses
    what it did not advertise.  If absence meant "unstated", a client would
    have to probe for every constraint, which is what discovery exists to
    avoid.
    """
    g = Gateway(backend=SaltOnlyBackend(), on_unenforceable="reject")
    caps = g.capabilities
    record(
        caps["retention_max_age"] is False,
        "capabilities: the salt-only backend does not advertise max_age",
    )
    frag = Fragment("f", "X", intent(**allow(retention={"mode": "allow", "max_age": 60})))
    try:
        g.handle([frag], ACME, now=0.0)
        record(False, "capabilities: an unadvertised constraint is refused, not attempted")
    except UnenforceableConstraint:
        record(True, "capabilities: an unadvertised constraint is refused, not attempted")

    # Control: a constraint the backend does advertise goes through, so the
    # refusal above is the capability check and not a backend that fails at
    # everything.
    ok = Fragment("f", "X", intent(**allow()))
    record(
        g.handle([ok], ACME, now=0.0).statuses[0].outcome in ("miss", "exact_hit"),
        "capabilities: an advertised constraint is served (control)",
    )


def test_emitted_capabilities_validate_against_the_schema(record):
    """The gateway's own capability document must satisfy the schema.

    The status round trip already exists; this is its counterpart, and it is
    the check that catches a schema change the emitter did not follow.
    """
    import json as _json
    import pathlib as _pathlib

    from gateway.binding_openai import OpenAIChatBinding
    from jsonschema import Draft202012Validator

    root = _pathlib.Path(__file__).resolve().parent.parent
    validator = Draft202012Validator(
        _json.loads((root / "llm-cache-capabilities.schema.json").read_text())
    )
    doc = OpenAIChatBinding(Gateway()).capabilities_document()
    errors = list(validator.iter_errors(doc))
    record(
        not errors,
        "round trip: emitted capabilities validate against llm-cache-capabilities.schema.json",
        "" if not errors else errors[0].message[:90],
    )

    # on_unenforceable is required, not advisory: a document without it must
    # not validate, or the portability hole the member closes reopens.
    stripped = _json.loads(_json.dumps(doc))
    del stripped["cache_intent_capabilities"]["on_unenforceable"]
    rejected = bool(list(validator.iter_errors(stripped)))
    record(
        rejected,
        "capabilities: a document omitting on_unenforceable is rejected",
        "rejected as required" if rejected else "ACCEPTED but the draft requires it",
    )


def test_advertised_hints_are_reported(record):
    """Section "Status Reporting": advertising a hint obliges reporting it.

    A hint MAY be ignored, so cache behavior cannot distinguish an
    implementation that used the value from one that discarded it.  The
    status can, and clipping against the advertised maximum makes the value
    observable and not merely the support.
    """
    g = Gateway(backend=InMemoryBackend())
    limit = g.capabilities["hints"]["reuse_within_max"]

    def status_for(value):
        doc = json.dumps(
            {
                "cache_intent": {
                    "version": 1,
                    "constraints": allow(),
                    "hints": {"reuse_within": value},
                }
            }
        )
        return g.handle([Fragment("f", "C", doc)], ACME, now=0.0).statuses[0].hints

    under = status_for(limit - 1)
    over = status_for(limit + 1)
    record(
        under.get("reuse_within") is not None,
        "hints: an advertised hint gets a status",
        f"status was {under.get('reuse_within')!r}",
    )
    record(
        over.get("reuse_within") == "clipped" and under.get("reuse_within") != "clipped",
        "hints: a value above the advertised maximum is reported clipped, one below is not",
        f"over={over.get('reuse_within')!r} under={under.get('reuse_within')!r}",
    )

    # A hint this version does not define is ignored, not reported, and does
    # not weaken anything.  `priority` is such a hint: it was considered for
    # version 1 and left out, so it exercises the forward-compatibility path
    # exactly as a hint invented after this document would.
    doc = json.dumps(
        {"cache_intent": {"version": 1, "constraints": allow(), "hints": {"priority": 90}}}
    )
    result = g.handle([Fragment("f", "C", doc)], ACME, now=0.0).statuses[0]
    record(
        "priority" not in result.hints,
        "hints: an undefined hint is ignored rather than reported",
        f"hints were {result.hints!r}",
    )
    record(
        result.effective_share == "tenant" and result.constraint_result == "satisfied",
        "hints: an undefined hint does not weaken the constraints alongside it",
    )


def test_per_fragment_share_is_advertised_honestly(record):
    """Section "Capability Discovery": the per-request-partition gap is discoverable.

    A single-partition engine honors every constraint and silently stops
    sharing the public prefix.  The only symptom is reuse that never happens,
    so a caller has to be able to ask.
    """
    record(
        InMemoryBackend().capabilities()["per_fragment_share"] is True,
        "capabilities: a backend with per-fragment partitions says so",
    )
    record(
        SaltOnlyBackend().capabilities()["per_fragment_share"] is False,
        "capabilities: the single-salt backend that models vLLM says it cannot",
    )


ALL = [
    test_5_approximate_never_enters_exact_namespace,
    test_8_caller_cannot_cross_tenant_boundary,
    test_10_no_reordering_without_equivalence,
    test_prose_duplicate_member_names,
    test_prose_exact_namespace_comparison,
    test_max_age_is_not_extended_by_reads,
    test_emitted_status_validates_against_the_schema,
    test_retained_state_keeps_its_own_sharing_boundary,
    test_status_has_one_entry_per_governed_fragment,
    test_absent_capability_member_means_unsupported,
    test_emitted_capabilities_validate_against_the_schema,
    test_advertised_hints_are_reported,
    test_per_fragment_share_is_advertised_honestly,
    lowering_matrix,
]
