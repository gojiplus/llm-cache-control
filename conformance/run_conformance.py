"""Conformance suite for draft-sood-llm-cache-control-01.

Three checks, in increasing order of what they actually prove:

1. Every JSON example in the draft and readme validates against a schema.
   Objects with no schema are named as unvalidated rather than skipped.
2. Every test vector runs.  Composition vectors are computed by the
   reference implementation in compose.py and compared to their expected
   effective policy.
3. Negative cases: documents the draft requires an implementation to
   reject, and the one it requires an implementation to accept.  Without
   this half the suite could not fail, so it is what makes the rest
   evidence.

Exit status is 0 only when every case passes and every vector kind is
recognized.
"""

import json
import pathlib
import re
import sys

from gateway.compose import IncomparableScopes, compose
from gateway.identity import namespace_identity
from jsonschema import Draft202012Validator

from conformance import behavioral

ROOT = pathlib.Path(__file__).resolve().parent.parent

SCHEMAS = {
    "cache_intent": "llm-cache-control.schema.json",
    "cache_intent_capabilities": "llm-cache-capabilities.schema.json",
    "cache_status": "llm-cache-status.schema.json",
}

# Draft appendix "Interoperability Test Cases" lists ten tests.  These three
# constrain implementation behavior rather than document content, so no data
# vector can express them.  They are covered in section 5 against the
# reference gateway instead.
BEHAVIORAL_ONLY = {
    5: "approximate state and its downstream state never enter the exact cache namespace",
    8: "a caller-provided tenant identifier cannot cross the authenticated tenant boundary",
    10: "a gateway does not reorder model-visible fragments without an equivalence declaration",
}

# Requirements a JSON Schema cannot structurally express.  Also covered in
# section 5, since they need an implementation to test.
PROSE_ONLY = [
    "duplicate JSON member names must be rejected",
    "namespace comparison is exact",
    "Unicode normalization must not be applied before comparison",
]

failures = []
notes = []


def record(ok, label, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def load_validators():
    out = {}
    for key, name in SCHEMAS.items():
        schema = json.loads((ROOT / name).read_text())
        Draft202012Validator.check_schema(schema)
        out[key] = Draft202012Validator(schema)
    return out


def json_blocks(path):
    text = path.read_text()
    blocks = re.findall(r"~~~~ json\n(.*?)\n~~~~", text, re.S)
    blocks += re.findall(r"```json\n(.*?)\n```", text, re.S)
    return [json.loads(b) for b in blocks]


def check_examples(validators):
    print("\n1. Schema validation of every JSON example")
    for name in ("readme.md", "draft-sood-llm-cache-control-01.md"):
        path = ROOT / name
        print(f"\n {name}")
        for i, obj in enumerate(json_blocks(path)):
            items = obj if isinstance(obj, list) else [obj]
            for j, item in enumerate(items):
                tag = f"block {i}" + (f"[{j}]" if isinstance(obj, list) else "")
                key = next(iter(item))
                validator = validators.get(key)
                if validator is None:
                    notes.append(f"{name} {tag}: no schema for '{key}'")
                    record(True, f"{tag} ({key})", "UNVALIDATED, no schema for this object")
                    continue
                errors = sorted(validator.iter_errors(item), key=lambda e: list(e.path))
                record(
                    not errors,
                    f"{tag} ({key})",
                    "" if not errors else f"{list(errors[0].path)} {errors[0].message[:80]}",
                )


def subset_matches(expected, actual):
    """Compare only the keys the vector actually constrains."""
    for key, want in expected.items():
        if key not in actual:
            return False, f"{key} missing from computed policy"
        got = actual[key]
        if isinstance(want, dict):
            ok, why = subset_matches(want, got)
            if not ok:
                return False, f"{key}.{why}"
        elif got != want:
            return False, f"{key}: expected {want!r}, computed {got!r}"
    return True, ""


def run_composition(vector, validators):
    frags = vector["fragments"]
    for frag in frags:
        doc = {"cache_intent": {"version": 1, "id": frag["id"], "constraints": frag["constraints"]}}
        errors = list(validators["cache_intent"].iter_errors(doc))
        record(
            not errors,
            f"{vector['name']}/{frag['id']} well-formed",
            "" if not errors else errors[0].message[:80],
        )

    unasserted = []
    for key, expected in vector["expected"].items():
        idx = int(key.rsplit("_", 1)[1])
        actual = compose([f["constraints"] for f in frags[: idx + 1]])
        ok, why = subset_matches(expected, actual)
        record(ok, f"{vector['name']}/{key}", why)
        for field in ("retention", "reuse", "share_max"):
            if field not in expected:
                unasserted.append(f"{key}.{field}")
    if unasserted:
        notes.append(f"{vector['name']}: not asserted by the vector: {', '.join(unasserted)}")


def run_identity(vector):
    if "request_1_namespaces" in vector:
        a = namespace_identity(vector["request_1_namespaces"])
        b = namespace_identity(vector["request_2_namespaces"])
    else:
        a = namespace_identity([vector["request_1_namespace"]])
        b = namespace_identity([vector["request_2_namespace"]])
    should_match = vector["expected"]["cache_match"]
    record(
        (a == b) == should_match,
        vector["name"],
        f"identities {'match' if a == b else 'differ'}, expected match={should_match}",
    )


def run_fail_closed(vector, validators):
    doc = {"cache_intent": {"version": 1, "constraints": vector["constraints"]}}
    errors = list(validators["cache_intent"].iter_errors(doc))
    record(
        bool(errors),
        vector["name"],
        "rejected as required" if errors else "ACCEPTED but the draft requires rejection",
    )


def run_hint_tolerance(vector, validators):
    doc = {
        "cache_intent": {
            "version": 1,
            "constraints": vector["constraints"],
            "hints": vector["hints"],
        }
    }
    errors = list(validators["cache_intent"].iter_errors(doc))
    record(
        not errors,
        vector["name"],
        "accepted, unknown hint tolerated" if not errors else errors[0].message[:80],
    )
    # compose() takes constraints only, so the meaningful assertion is that no
    # hint can reach the effective policy: the composed keys must stay within
    # the constraint-derived set even when an unknown hint is present.
    allowed = {"retention", "reuse", "share_max", "namespace_identity"}
    policy = compose([vector["constraints"]])
    leaked = set(policy) - allowed
    record(
        not leaked,
        f"{vector['name']}/constraints_unchanged",
        "" if not leaked else f"hint-derived keys leaked into policy: {sorted(leaked)}",
    )


def run_digest(vector):
    """The effective namespace digest is byte exact, not merely deterministic.

    These golden values are what make two implementations mutually
    cache-compatible. Everything else in this suite can pass while two
    gateways share nothing.
    """
    for case in vector["cases"]:
        computed = namespace_identity(case["namespaces"])
        record(
            computed == case["digest"],
            f"{vector['name']}: {case['namespaces']}",
            "" if computed == case["digest"] else f"expected {case['digest']}, computed {computed}",
        )


def check_vectors(validators):
    print("\n2. Test vectors")
    data = json.loads((ROOT / "llm-cache-control-test-vectors.json").read_text())
    dispatch = {
        "digest": run_digest,
        "composition": lambda v: run_composition(v, validators),
        "identity": run_identity,
        "fail_closed": lambda v: run_fail_closed(v, validators),
        "hint_tolerance": lambda v: run_hint_tolerance(v, validators),
    }
    covered = set()
    for vector in data["vectors"]:
        kind = vector.get("kind")
        handler = dispatch.get(kind)
        if handler is None:
            record(False, vector["name"], f"unknown vector kind {kind!r}, not run")
            continue
        handler(vector)
        if vector.get("draft_test"):
            covered.add(vector["draft_test"])
    return covered


def check_negatives(validators):
    print("\n3. Negative cases the draft requires be rejected")
    base = {"retention": {"mode": "allow"}, "reuse": "exact", "share_max": "tenant"}

    def intent(constraints=None, **top):
        c = dict(base)
        c.update(constraints or {})
        d = {"version": 1, "constraints": c}
        d.update(top)
        return {"cache_intent": d}

    cases = [
        ("unknown constraint member", intent({"future_constraint": "value"})),
        (
            "max_age with retention.mode forbid",
            intent({"retention": {"mode": "forbid", "max_age": 60}}),
        ),
        ("share_max off the ladder", intent({"share_max": "everyone"})),
        ("reuse value not defined", intent({"reuse": "fuzzy"})),
        ("negative max_age", intent({"retention": {"mode": "allow", "max_age": -1}})),
        (
            "negative reuse_within",
            {"cache_intent": {"version": 1, "constraints": base, "hints": {"reuse_within": -1}}},
        ),
        ("unsupported version", {"cache_intent": {"version": 2, "constraints": base}}),
        (
            "missing share_max",
            {
                "cache_intent": {
                    "version": 1,
                    "constraints": {"retention": {"mode": "allow"}, "reuse": "exact"},
                }
            },
        ),
        ("unknown top-level member", intent(extra=1)),
        ("namespace over 256 characters", intent({"namespace": "x" * 257})),
        ("empty namespace", intent({"namespace": ""})),
    ]
    for label, doc in cases:
        record(bool(list(validators["cache_intent"].iter_errors(doc))), f"rejects: {label}")

    doc = {"cache_intent": {"version": 1, "constraints": base, "hints": {"future_hint": 17}}}
    record(not list(validators["cache_intent"].iter_errors(doc)), "accepts: unknown hint member")

    try:
        intersect = compose(
            [
                {"retention": {"mode": "allow"}, "reuse": "exact", "share_max": "group:eng"},
                {"retention": {"mode": "allow"}, "reuse": "exact", "share_max": "tenant"},
            ]
        )
        record(False, "bypasses: incomparable sharing boundaries", f"returned {intersect}")
    except IncomparableScopes:
        record(True, "bypasses: incomparable sharing boundaries")


def check_behavioral():
    print("\n4. Behavioral conformance against the reference gateway")
    print("   (draft tests 5, 8, 10 and the three schema-inexpressible rules,")
    print("    plus the lowering matrix that proves constraints are never downgraded)")
    for case in behavioral.ALL:
        print(f"\n {case.__name__}")
        try:
            case(record)
        except Exception as exc:
            # A case that raises must be reported and the suite must carry
            # on.  Aborting here would hide every case after it, which is
            # how a suite comes to report less than it knows.
            record(False, f"{case.__name__} raised", f"{type(exc).__name__}: {exc}")


def report_coverage(covered):
    print("\n5. Coverage against the draft's ten interoperability tests")
    for n in range(1, 11):
        if n in covered:
            print(f"  vector       test {n}")
        elif n in BEHAVIORAL_ONLY:
            print(f"  gateway      test {n}: {BEHAVIORAL_ONLY[n]}")
        else:
            print(f"  UNCOVERED    test {n}")
            failures.append(f"draft test {n} has no coverage")
    print("\n  Schema cannot express these; covered against the gateway in section 4:")
    for item in PROSE_ONLY:
        print(f"    - {item}")
    print("\n  Still open, and not a policy question:")
    print("    - no live inference engine executes tokens behind the gateway;")
    print("      the backend seam is gateway/backends.py and SaltOnlyBackend")
    print("      models the vLLM cache_salt shape a real binding would use.")


def main():
    validators = load_validators()
    check_examples(validators)
    covered = check_vectors(validators)
    check_negatives(validators)
    check_behavioral()
    report_coverage(covered)

    if notes:
        print("\nNotes")
        for note in notes:
            print(f"  {note}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} case(s)")
        for f in failures:
            print(f"  {f}")
        return 1
    print("All conformance cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
