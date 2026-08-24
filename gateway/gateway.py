"""The reference gateway.

Request path: parse strictly, validate, strip untrusted scope, compose
constraints across fragments, resolve each prefix against the cache under
the authenticated partition, and report status.

What this is not: a binding to a real inference engine.  Everything here is
decided before a token is computed, which is why an in-memory cache is
sufficient to exercise it.  Cache reads and writes are the seam.
"""

import json
import pathlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from jsonschema import Draft202012Validator

from .auth import AuthContext, partition_for, strip_untrusted_scope
from .backends import CacheBackend, InMemoryBackend
from .compose import compose
from .errors import DuplicateFragmentId, ReorderNotPermitted, UnenforceableConstraint
from .identity import cache_key
from .parse import parse_cache_intent

ROOT = pathlib.Path(__file__).resolve().parent.parent


@dataclass
class Fragment:
    """One ordered unit of model input carrying cache intent.

    `content` stands in for the fragment's model-visible tokens.
    """

    id: str
    content: str
    intent_json: Optional[str] = None
    approx_class: Optional[str] = None
    """Fragments sharing an approx_class are similar enough to donate state.

    The draft puts approximate-reuse quality metrics out of scope and says a
    bare numeric budget is not interoperable, so similarity is modeled as an
    explicit donor class rather than an invented distance measure.
    """


@dataclass
class FragmentStatus:
    id: str
    outcome: str
    effective_share: str
    retention: str
    constraint_result: str = "satisfied"
    exact: bool = True
    effective_reuse: Optional[str] = None
    effective_retention_mode: Optional[str] = None
    namespace_identity: Optional[str] = None
    hints: Dict[str, str] = field(default_factory=dict)


@dataclass
class Result:
    statuses: List[FragmentStatus] = field(default_factory=list)
    stripped_scope_fields: tuple = ()
    order: List[str] = field(default_factory=list)
    cache_salt: Optional[str] = None

    def as_cache_status(self) -> Dict:
        """One entry per governed fragment, in fragment order.

        `id` is omitted when the fragment supplied none.  Emitting it as null
        would both break the status schema, which requires a non-empty
        string, and imply the application named something it did not.
        Correlation is positional; `id` is a convenience on top of it.
        """
        entries = []
        for s in self.statuses:
            entry = {
                "outcome": s.outcome,
                "effective_share": s.effective_share,
                "retention": s.retention,
                "constraint_result": s.constraint_result,
            }
            if s.hints:
                entry["hints"] = dict(s.hints)
            if s.id is not None:
                entry = {"id": s.id, **entry}
            entries.append(entry)
        return {"cache_status": entries}


class Gateway:
    """Lowers application cache intent onto a backend, without weakening it.

    `on_unenforceable` selects which of the two permitted responses to an
    unenforceable constraint this gateway uses.  The draft allows either
    "bypass" or "reject".  It allows nothing else, so there is no third
    value and no downgrade path.
    """

    def __init__(self, backend: Optional[CacheBackend] = None, on_unenforceable: str = "bypass"):
        if on_unenforceable not in ("bypass", "reject"):
            raise ValueError("on_unenforceable must be 'bypass' or 'reject'")
        schema = json.loads((ROOT / "llm-cache-control.schema.json").read_text())
        self.validator = Draft202012Validator(schema)
        self.backend = backend or InMemoryBackend()
        self.on_unenforceable = on_unenforceable

    @property
    def capabilities(self) -> Dict:
        """Capabilities come from the backend, never from the gateway.

        A gateway that advertised more than its backend can do would be
        promising enforcement it cannot deliver.
        """
        return self.backend.capabilities()

    def plan_order(
        self, fragments: Sequence[Fragment], equivalence_declared: bool = False
    ) -> List[Fragment]:
        """Return the order fragments will be sent to the engine.

        A gateway can raise its hit rate by hoisting public fragments ahead
        of tenant-scoped ones.  The draft forbids that unless the host
        protocol or application declared the alternative order semantically
        equivalent, so the default path returns the input order untouched
        and the optimization is reachable only behind an explicit flag.
        """
        if not equivalence_declared:
            return list(fragments)
        return sorted(fragments, key=lambda f: self._share_rank(f))

    def reorder_for_reuse(self, fragments: Sequence[Fragment]) -> List[Fragment]:
        """Reorder without an equivalence declaration. Always refuses."""
        raise ReorderNotPermitted(
            "reordering model-visible fragments for cache reuse requires an "
            "explicit semantic-equivalence declaration"
        )

    def _share_rank(self, fragment: Fragment) -> int:
        ladder = ["public", "tenant", "principal", "session", "request"]
        if fragment.intent_json is None:
            return len(ladder)
        intent = parse_cache_intent(fragment.intent_json, self.validator)
        return ladder.index(intent["constraints"]["share_max"])

    def _check_enforceable(self, constraints: Dict) -> None:
        """Raise if the backend cannot enforce every supplied constraint.

        Called on the *effective* (composed) constraints, not the fragment's
        own, because composition can produce a policy stricter than anything
        a single fragment asked for.
        """
        caps = self.capabilities
        if constraints["retention"]["mode"] not in caps["retention_modes"]:
            raise UnenforceableConstraint("retention mode not supported")
        if "max_age" in constraints["retention"] and not caps["retention_max_age"]:
            raise UnenforceableConstraint("max_age not supported")
        if constraints["reuse"] not in caps["reuse"]:
            raise UnenforceableConstraint(f"reuse {constraints['reuse']!r} not supported")
        if constraints["share_max"] not in caps["share_max"]:
            raise UnenforceableConstraint(f"share_max {constraints['share_max']!r} not supported")
        if "namespace_identity" in constraints and not caps["namespace"]:
            raise UnenforceableConstraint("namespace not supported")

    def handle(
        self,
        fragments: Sequence[Fragment],
        auth: AuthContext,
        now: float,
        request_id: str = "req-1",
        request_payload: Optional[Dict] = None,
        equivalence_declared: bool = False,
    ) -> Result:
        ordered = self.plan_order(fragments, equivalence_declared)

        # Section "Identifier": ids are unique within one request.  Checked
        # before any work so a malformed request fails in microseconds rather
        # than after the expensive part.
        supplied = [f.id for f in ordered if f.id is not None]
        if len(supplied) != len(set(supplied)):
            dupes = sorted({i for i in supplied if supplied.count(i) > 1})
            raise DuplicateFragmentId(f"fragment ids repeat within one request: {dupes}")

        result = Result(order=[f.id for f in ordered])

        if request_payload is not None:
            _, stripped = strip_untrusted_scope(request_payload)
            result.stripped_scope_fields = stripped

        constraints_so_far: List[Dict] = []
        namespaces_so_far: List[str] = []
        contents_so_far: List[str] = []
        # Once approximate state has been read, every prefix that depends on
        # it is approximate too, and must not be published as exact.
        tainted_approximate = False

        for fragment in ordered:
            contents_so_far.append(fragment.content)
            fragment_hints: Dict[str, str] = {}
            if fragment.intent_json is None:
                if not constraints_so_far:
                    # Nothing to inherit yet, so no policy exists under which
                    # this state could be reused.
                    result.statuses.append(
                        FragmentStatus(fragment.id, "bypass", "request", "declined")
                    )
                    continue
                # A fragment supplying no constraints of its own still
                # inherits the effective policy of everything before it,
                # because the state it produces depends on those fragments.
                # Resetting to `request` would be safe but wrong: it reports
                # a boundary narrower than the one enforced, and declines
                # retention the intent allowed.
            else:
                intent = parse_cache_intent(fragment.intent_json, self.validator)
                constraints = intent["constraints"]

                constraints_so_far.append(constraints)
                fragment_hints = self._hint_status(intent.get("hints", {}))
                if "namespace" in constraints:
                    namespaces_so_far.append(constraints["namespace"])

            effective = compose(constraints_so_far)

            # The lowering asymmetry lives here.  If the backend cannot
            # enforce the effective policy, the only permitted responses are
            # bypass and reject.  There is no branch that proceeds with a
            # weakened constraint, which is the property this whole design
            # exists to guarantee.
            try:
                self._check_enforceable(effective)
            except UnenforceableConstraint:
                if self.on_unenforceable == "reject":
                    raise
                result.statuses.append(
                    FragmentStatus(
                        id=fragment.id,
                        outcome="bypass",
                        effective_share=effective["share_max"],
                        retention=(
                            "forbidden"
                            if effective["retention"]["mode"] == "forbid"
                            else "declined"
                        ),
                        constraint_result="satisfied",
                        effective_reuse=effective["reuse"],
                        effective_retention_mode=effective["retention"]["mode"],
                        namespace_identity=effective.get("namespace_identity"),
                        hints=fragment_hints,
                    )
                )
                continue

            partition = partition_for(effective["share_max"], auth, request_id)

            outcome, used_approximate = self._resolve(
                effective,
                contents_so_far,
                namespaces_so_far,
                partition,
                now,
                fragment.approx_class,
            )
            if used_approximate:
                tainted_approximate = True

            if outcome == "unobserved":
                retention = (
                    "forbidden" if effective["retention"]["mode"] == "forbid" else "unobserved"
                )
            elif outcome == "exact_hit":
                # "A read or refresh MUST NOT extend eligibility past this
                # bound."  Re-storing on a hit would reset materialized_at
                # and roll max_age forward on every request, which is the
                # exact failure the requirement names.
                retention = "retained"
            else:
                retention = self._materialize(
                    effective,
                    contents_so_far,
                    namespaces_so_far,
                    partition,
                    now,
                    exact=not tainted_approximate,
                    approx_class=fragment.approx_class,
                )
            result.statuses.append(
                FragmentStatus(
                    id=fragment.id,
                    outcome=outcome,
                    effective_share=effective["share_max"],
                    retention=retention,
                    exact=not tainted_approximate,
                    effective_reuse=effective["reuse"],
                    effective_retention_mode=effective["retention"]["mode"],
                    namespace_identity=effective.get("namespace_identity"),
                    hints=fragment_hints,
                )
            )
        return result

    def _hint_status(self, hints: Dict) -> Dict[str, str]:
        """Status for every hint this implementation advertises.

        Section "Status Reporting": advertising a hint obliges the
        implementation to report it, because a hint MAY be ignored and
        without a status a caller cannot tell a implementation that used the
        value from one that discarded it.  A hint the backend does not
        advertise gets no entry, which the capability rules already say means
        unsupported.

        `clipped` is reported exactly when the value exceeds the advertised
        maximum.  That is what makes the value observable and not just the
        support: a caller that sends 7200 against a 3600 maximum is told, and
        a caller that sends 1800 is told something different.
        """
        advertised = self.capabilities.get("hints", {})
        out: Dict[str, str] = {}
        if "reuse_within" in hints and "reuse_within_max" in advertised:
            limit = advertised["reuse_within_max"]
            out["reuse_within"] = "clipped" if hints["reuse_within"] > limit else "accepted"
        return out

    def _execution(self) -> Dict:
        return {"model": "reference-model-1", "tokenizer_rev": "1"}

    def _resolve(self, effective, contents, namespaces, partition, now, approx_class):
        """Look for reusable state. Returns (outcome, used_approximate)."""
        if not getattr(self.backend, "observes_outcomes", True):
            # Reporting `miss` here would be a claim the gateway cannot
            # support, and a caller measuring hit rate could not tell.
            return "unobserved", False

        if effective["reuse"] == "none":
            # `miss` is a claim that a lookup happened and found nothing.  No
            # lookup happens here, and a warming request (reuse: none with
            # retention: allow) is a normal shape, so reporting `miss` would
            # make a healthy cache look cold to anyone measuring hit rate.
            return "bypass", False

        exact_key = cache_key(self._execution(), contents, namespaces, partition, exact=True)
        if self.backend.get(exact_key, now) is not None:
            return "exact_hit", False

        if effective["reuse"] == "approximate" and approx_class is not None:
            # A donor is state for *different* input in the same similarity
            # class, which is what makes the reuse approximate.
            donor_key = cache_key(
                self._execution(), [approx_class], namespaces, partition, exact=False
            )
            if self.backend.get(donor_key, now) is not None:
                return "approximate_hit", True

        return "miss", False

    def _materialize(
        self, effective, contents, namespaces, partition, now, exact, approx_class=None
    ):
        """Store newly computed state if retention permits. Returns status."""
        if effective["retention"]["mode"] == "forbid":
            return "forbidden"
        if effective["share_max"] == "request":
            # Reuse is limited to the current request, so nothing is
            # published for a later one.
            return "declined"
        key = cache_key(self._execution(), contents, namespaces, partition, exact=exact)
        self.backend.put(key, now, effective["retention"].get("max_age"), exact=exact)
        if approx_class is not None:
            # State is additionally addressable by similarity class so it can
            # donate to a later approximate request.  Exact state may be
            # matched approximately; what the draft forbids is the reverse,
            # publishing approximately-derived state as exact.  The donor
            # index therefore lives entirely in the approximate keyspace.
            donor_key = cache_key(
                self._execution(), [approx_class], namespaces, partition, exact=False
            )
            self.backend.put(donor_key, now, effective["retention"].get("max_age"), exact=False)
        return "retained"
