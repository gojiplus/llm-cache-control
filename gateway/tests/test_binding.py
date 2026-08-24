"""Tests for binding-openai-chat.md.

Each test names the section of the binding it enforces, so a change to the
document has a visible consequence here.
"""

import json

import pytest
from gateway import AuthContext, Gateway
from gateway.backends import InMemoryBackend, SaltOnlyBackend
from gateway.binding_openai import OpenAIChatBinding, extract_fragments, strip_binding_members

AUTH = AuthContext(tenant="acme", principal="alice", session="s1")
BASE = {"retention": {"mode": "allow"}, "reuse": "exact", "share_max": "tenant"}


def intent(id_, **over):
    c = dict(BASE)
    c.update(over)
    return {"version": 1, "id": id_, "constraints": c}


def request(**over):
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(over)
    return body


class TestSection1Fragments:
    def test_tools_precede_messages(self):
        req = request(
            tools=[{"type": "function", "function": {"name": "search"}}],
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        )
        ids = [f.id for f in extract_fragments(req)]
        assert ids == ["tool[0]", "message[0]", "message[1]"]

    def test_fragment_id_comes_from_the_intent_when_supplied(self):
        req = request(messages=[{"role": "system", "content": "s", "cache_intent": intent("sys")}])
        assert [f.id for f in extract_fragments(req)] == ["sys"]

    def test_top_level_members_are_not_fragments(self):
        assert len(extract_fragments(request(temperature=0.7, max_tokens=5))) == 1

    def test_absent_arrays_are_tolerated(self):
        assert extract_fragments({"model": "m"}) == []

    def test_differing_content_yields_differing_fragments(self):
        a = extract_fragments(request(messages=[{"role": "user", "content": "one"}]))[0]
        b = extract_fragments(request(messages=[{"role": "user", "content": "two"}]))[0]
        assert a.content != b.content

    def test_all_model_visible_message_members_enter_fragment_identity(self):
        a = extract_fragments(
            request(messages=[{"role": "user", "name": "alice", "content": "same"}])
        )[0]
        b = extract_fragments(
            request(messages=[{"role": "user", "name": "bob", "content": "same"}])
        )[0]
        assert a.content != b.content


class TestSection2Stripping:
    def test_cache_intent_is_removed_before_forwarding(self):
        req = request(messages=[{"role": "system", "content": "s", "cache_intent": intent("sys")}])
        cleaned, _ = strip_binding_members(req)
        assert "cache_intent" not in cleaned["messages"][0]

    def test_stripping_does_not_mutate_the_caller_request(self):
        req = request(messages=[{"role": "system", "content": "s", "cache_intent": intent("sys")}])
        strip_binding_members(req)
        assert "cache_intent" in req["messages"][0]

    def test_model_visible_content_survives(self):
        req = request(messages=[{"role": "system", "content": "keep me"}])
        cleaned, _ = strip_binding_members(req)
        assert cleaned["messages"][0]["content"] == "keep me"

    def test_caller_supplied_cache_salt_is_removed(self):
        cleaned, stripped = strip_binding_members(request(cache_salt="caller-selected"))
        assert "cache_salt" not in cleaned
        assert "cache_salt" in stripped


class TestSection3Scopes:
    def test_body_supplied_scope_is_stripped(self):
        req = request(tenant="other-corp", cache_partition="tenant:other-corp")
        cleaned, stripped = strip_binding_members(req)
        assert "tenant" not in cleaned and "cache_partition" not in cleaned
        assert set(stripped) == {"tenant", "cache_partition"}

    def test_body_supplied_scope_on_a_message_is_stripped(self):
        req = request(messages=[{"role": "user", "content": "u", "tenant": "other-corp"}])
        cleaned, stripped = strip_binding_members(req)
        assert "tenant" not in cleaned["messages"][0]
        assert "messages.tenant" in stripped

    def test_two_tenants_get_different_partitions_for_identical_bodies(self):
        binding = OpenAIChatBinding(Gateway(backend=SaltOnlyBackend()))
        body = json.dumps(
            request(messages=[{"role": "system", "content": "s", "cache_intent": intent("sys")}])
        )
        other = AuthContext(tenant="other-corp", principal="bob", session="s2")
        _, r1, _ = binding.prepare(body, AUTH, "r1")
        _, r2, _ = binding.prepare(body, other, "r2")
        assert binding.salt_for_request(r1, AUTH, "r1") != binding.salt_for_request(r2, other, "r2")


class TestSection4Status:
    def test_status_is_attached_top_level(self):
        binding = OpenAIChatBinding(Gateway(backend=InMemoryBackend()))
        body = json.dumps(
            request(messages=[{"role": "system", "content": "s", "cache_intent": intent("sys")}])
        )
        _, result, _ = binding.prepare(body, AUTH, "r1")
        response = binding.attach_status({"choices": [], "usage": {}}, result)
        assert response["cache_status"][0]["id"] == "sys"

    def test_status_validates_against_the_status_schema(self):
        import pathlib

        from jsonschema import Draft202012Validator

        root = pathlib.Path(__file__).resolve().parent.parent.parent
        validator = Draft202012Validator(
            json.loads((root / "llm-cache-status.schema.json").read_text())
        )
        binding = OpenAIChatBinding(Gateway(backend=InMemoryBackend()))
        body = json.dumps(
            request(messages=[{"role": "system", "content": "s", "cache_intent": intent("sys")}])
        )
        _, result, _ = binding.prepare(body, AUTH, "r1")
        doc = binding.attach_status({}, result)
        assert not list(validator.iter_errors({"cache_status": doc["cache_status"]}))


class TestSection5Capabilities:
    def test_capabilities_validate_against_the_capabilities_schema(self):
        import pathlib

        from jsonschema import Draft202012Validator

        root = pathlib.Path(__file__).resolve().parent.parent.parent
        validator = Draft202012Validator(
            json.loads((root / "llm-cache-capabilities.schema.json").read_text())
        )
        for backend in (InMemoryBackend(), SaltOnlyBackend()):
            doc = OpenAIChatBinding(Gateway(backend=backend)).capabilities_document()
            assert not list(validator.iter_errors(doc)), backend.name

    def test_capabilities_report_the_backend_not_the_ideal(self):
        doc = OpenAIChatBinding(Gateway(backend=SaltOnlyBackend())).capabilities_document()
        constraints = doc["cache_intent_capabilities"]["constraints"]
        assert constraints["retention_max_age"] is False
        assert "approximate" not in constraints["reuse"]


class TestSection6Errors:
    @pytest.mark.parametrize(
        "body,code",
        [
            ('{"model":"m","messages":[],"model":"dup"}', "cache_intent_invalid"),
            ("{not json", "cache_intent_invalid"),
            (
                json.dumps(
                    request(
                        messages=[
                            {
                                "role": "u",
                                "content": "x",
                                "cache_intent": {"version": 9, "constraints": BASE},
                            }
                        ]
                    )
                ),
                "cache_intent_unsupported_version",
            ),
            (
                json.dumps(
                    request(
                        messages=[
                            {
                                "role": "u",
                                "content": "x",
                                "cache_intent": {
                                    "version": 1,
                                    "constraints": {**BASE, "future": "x"},
                                },
                            }
                        ]
                    )
                ),
                "cache_intent_invalid",
            ),
            (
                json.dumps(
                    request(
                        messages=[
                            {
                                "role": "u",
                                "content": "x",
                                "cache_intent": {"constraints": BASE},
                            }
                        ]
                    )
                ),
                "cache_intent_invalid",
            ),
        ],
    )
    def test_error_codes(self, body, code):
        binding = OpenAIChatBinding(Gateway(backend=InMemoryBackend()))
        upstream, result, error = binding.prepare(body, AUTH, "r1")
        assert upstream is None and result is None
        assert json.loads(error)["error"]["code"] == code

    def test_rejecting_gateway_reports_unenforceable(self):
        binding = OpenAIChatBinding(Gateway(backend=SaltOnlyBackend(), on_unenforceable="reject"))
        body = json.dumps(
            request(
                messages=[
                    {
                        "role": "system",
                        "content": "s",
                        "cache_intent": intent("sys", retention={"mode": "allow", "max_age": 600}),
                    }
                ]
            )
        )
        _, _, error = binding.prepare(body, AUTH, "r1")
        assert json.loads(error)["error"]["code"] == "cache_intent_unenforceable"

    @pytest.mark.parametrize("invalid", [{}, None, False, [], ""])
    def test_present_but_invalid_cache_intent_is_not_treated_as_absent(self, invalid):
        binding = OpenAIChatBinding(Gateway(backend=InMemoryBackend()))
        body = json.dumps(
            request(messages=[{"role": "user", "content": "x", "cache_intent": invalid}])
        )
        upstream, result, error = binding.prepare(body, AUTH, "r1")
        assert upstream is None and result is None
        assert json.loads(error)["error"]["code"] == "cache_intent_invalid"

    @pytest.mark.parametrize("body", ["[]", '"text"', "null"])
    def test_non_object_request_is_rejected_cleanly(self, body):
        binding = OpenAIChatBinding(Gateway(backend=InMemoryBackend()))
        upstream, result, error = binding.prepare(body, AUTH, "r1")
        assert upstream is None and result is None
        assert json.loads(error)["error"]["code"] == "cache_intent_invalid"


class TestSection7PerRequestPartition:
    def test_one_request_carries_the_narrowest_boundary(self):
        """The public prefix cannot keep its own boundary on such an engine."""
        binding = OpenAIChatBinding(Gateway(backend=SaltOnlyBackend()))
        body = json.dumps(
            request(
                messages=[
                    {
                        "role": "system",
                        "content": "tools",
                        "cache_intent": intent("tools", share_max="public"),
                    },
                    {
                        "role": "system",
                        "content": "private",
                        "cache_intent": intent("sys", share_max="tenant"),
                    },
                ]
            )
        )
        _, result, _ = binding.prepare(body, AUTH, "r1")
        shares = [s.effective_share for s in result.statuses]
        assert shares == ["tenant", "tenant"]

        public_only = json.dumps(
            request(
                messages=[
                    {
                        "role": "system",
                        "content": "tools",
                        "cache_intent": intent("tools", share_max="public"),
                    }
                ]
            )
        )
        _, public_result, _ = binding.prepare(public_only, AUTH, "r2")
        # The mixed request is lowered onto the tenant partition, so its
        # public prefix cannot match the partition a public-only request uses.
        assert binding.salt_for_request(result, AUTH, "r1") != binding.salt_for_request(
            public_result, AUTH, "r2"
        )

    def test_namespace_digest_enters_the_actual_request_salt(self):
        binding = OpenAIChatBinding(Gateway(backend=SaltOnlyBackend()))

        def prepare(namespace, request_id):
            body = json.dumps(
                request(
                    messages=[
                        {
                            "role": "system",
                            "content": "same",
                            "cache_intent": intent("sys", namespace=namespace),
                        }
                    ]
                )
            )
            _, result, error = binding.prepare(body, AUTH, request_id)
            assert error is None
            return binding.salt_for_request(result, AUTH, request_id)

        assert prepare("agent:v1", "r1") != prepare("agent:v2", "r2")

    @pytest.mark.parametrize(
        "over",
        [
            {"reuse": "none"},
            {"retention": {"mode": "forbid"}},
            {"retention": {"mode": "allow", "max_age": 60}},
        ],
    )
    def test_fresh_computation_and_fallback_use_unique_request_salts(self, over):
        binding = OpenAIChatBinding(Gateway(backend=SaltOnlyBackend()))
        body = json.dumps(
            request(
                messages=[
                    {
                        "role": "system",
                        "content": "same",
                        "cache_intent": intent("sys", **over),
                    }
                ]
            )
        )
        _, first, first_error = binding.prepare(body, AUTH, "r1")
        _, second, second_error = binding.prepare(body, AUTH, "r2")
        assert first_error is None and second_error is None
        assert first.statuses[0].outcome == "bypass"
        assert first.statuses[0].effective_share == "request"
        assert binding.salt_for_request(first, AUTH, "r1") != binding.salt_for_request(
            second, AUTH, "r2"
        )

    def test_caller_salt_cannot_survive_a_policy_fallback(self):
        binding = OpenAIChatBinding(Gateway(backend=SaltOnlyBackend()))
        body = json.dumps(
            request(
                cache_salt="caller-selected",
                messages=[
                    {
                        "role": "system",
                        "content": "same",
                        "cache_intent": intent("sys", retention={"mode": "allow", "max_age": 60}),
                    }
                ],
            )
        )
        upstream, result, error = binding.prepare(body, AUTH, "r1")
        assert error is None
        derived = binding.salt_for_request(result, AUTH, "r1")
        assert upstream["cache_salt"] == derived
        assert derived != "caller-selected"


class TestBindingClock:
    def test_max_age_uses_the_binding_clock(self):
        now = [0.0]
        binding = OpenAIChatBinding(Gateway(backend=InMemoryBackend()), clock=lambda: now[0])
        body = json.dumps(
            request(
                messages=[
                    {
                        "role": "system",
                        "content": "same",
                        "cache_intent": intent("sys", retention={"mode": "allow", "max_age": 1}),
                    }
                ]
            )
        )
        _, first, _ = binding.prepare(body, AUTH, "r1")
        now[0] = 2.0
        _, second, _ = binding.prepare(body, AUTH, "r2")
        assert first.statuses[0].outcome == "miss"
        assert second.statuses[0].outcome == "miss"
