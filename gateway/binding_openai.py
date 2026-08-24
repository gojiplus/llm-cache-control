"""Implementation of binding-openai-chat.md.

Turns an OpenAI-shaped chat request into the draft's fragment model, applies
the gateway's policy, and produces the upstream request plus the status to
return.

The binding decisions this file makes, all of them from the binding
document rather than invented here: a fragment is one `tools` element or one
`messages` element, tools precede messages, `cache_intent` rides on the
fragment, scopes come from authenticated context, and `cache_status` is
returned top level.
"""

import copy
import json
import time
from typing import Callable, Dict, List, Optional, Tuple

from .auth import UNTRUSTED_SCOPE_FIELDS, AuthContext
from .errors import CacheIntentError, UnsupportedVersion
from .gateway import Fragment, Gateway
from .parse import loads_strict

ERROR_CODES = {
    "cache_intent_invalid": 400,
    "cache_intent_unsupported_version": 400,
    "cache_intent_unenforceable": 400,
}


def _fragment_content(item: Dict, kind: str) -> str:
    """The model-visible text of a fragment.

    Stands in for rendered tokens.  It only needs to be stable and to differ
    when the rendered input differs, since the engine owns real execution
    identity.
    """
    visible = copy.deepcopy(item)
    visible.pop("cache_intent", None)
    for field in UNTRUSTED_SCOPE_FIELDS:
        visible.pop(field, None)
    return kind + ":" + json.dumps(visible, sort_keys=True, separators=(",", ":"))


class ContentPartIntent(CacheIntentError):
    """A cache_intent was found on a content part, which is not a fragment.

    Binding section 1 requires rejection.  Ignoring it would leave the
    application believing a constraint is in force when nothing enforces it,
    which is worse than either honoring or refusing.
    """


class InvalidBinding(CacheIntentError):
    """The host request cannot be interpreted as this binding's fragment model."""


def _reject_content_part_intents(message: Dict, index: int) -> None:
    content = message.get("content")
    if not isinstance(content, list):
        return
    for part_index, part in enumerate(content):
        if isinstance(part, dict) and "cache_intent" in part:
            raise ContentPartIntent(
                f"cache_intent on messages[{index}].content[{part_index}]: "
                "a content part is not a fragment in this binding version"
            )


def extract_fragments(request: Dict) -> List[Fragment]:
    """Fragment order is every tools element, then every messages element."""
    if not isinstance(request, dict):
        raise InvalidBinding("the request body must be a JSON object")
    fragments = []
    tools = request.get("tools") or []
    messages = request.get("messages") or []
    if not isinstance(tools, list):
        raise InvalidBinding("tools must be an array")
    if not isinstance(messages, list):
        raise InvalidBinding("messages must be an array")
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise InvalidBinding(f"tools[{index}] must be an object")
        has_intent = "cache_intent" in tool
        intent = tool.get("cache_intent")
        fragments.append(
            Fragment(
                id=(
                    intent.get("id", f"tool[{index}]")
                    if isinstance(intent, dict)
                    else f"tool[{index}]"
                ),
                content=_fragment_content(tool, "tool"),
                intent_json=json.dumps({"cache_intent": intent}) if has_intent else None,
            )
        )
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise InvalidBinding(f"messages[{index}] must be an object")
        _reject_content_part_intents(message, index)
        has_intent = "cache_intent" in message
        intent = message.get("cache_intent")
        fragments.append(
            Fragment(
                id=(
                    intent.get("id", f"message[{index}]")
                    if isinstance(intent, dict)
                    else f"message[{index}]"
                ),
                content=_fragment_content(message, "message"),
                intent_json=json.dumps({"cache_intent": intent}) if has_intent else None,
            )
        )
    return fragments


def strip_binding_members(request: Dict) -> Tuple[Dict, List[str]]:
    """Remove everything upstream must not see.

    `cache_intent` is gateway facing: leaving it in the body changes the
    rendered prompt on servers that serialize unknown message members.
    Caller-supplied scope coordinates are removed because they are not
    authorization and must not reach the engine.
    """
    cleaned = copy.deepcopy(request)
    stripped = []
    for field in UNTRUSTED_SCOPE_FIELDS:
        if field in cleaned:
            del cleaned[field]
            stripped.append(field)
    for key in ("tools", "messages"):
        for item in cleaned.get(key) or []:
            if "cache_intent" in item:
                del item["cache_intent"]
            for field in UNTRUSTED_SCOPE_FIELDS:
                if field in item:
                    del item[field]
                    stripped.append(f"{key}.{field}")
    return cleaned, stripped


class OpenAIChatBinding:
    """Applies the cache control contract to an OpenAI chat request."""

    def __init__(self, gateway: Gateway, clock: Callable[[], float] = time.monotonic):
        self.gateway = gateway
        self.clock = clock

    def capabilities_document(self) -> Dict:
        caps = self.gateway.capabilities
        return {
            "cache_intent_capabilities": {
                "versions": [1],
                "constraints": {
                    "retention_modes": sorted(caps["retention_modes"]),
                    "retention_max_age": caps["retention_max_age"],
                    "reuse": sorted(caps["reuse"]),
                    "share_max": sorted(caps["share_max"]),
                    "namespace": caps["namespace"],
                },
                # Hints come from the backend rather than a literal, because
                # advertising one obliges the implementation to report its
                # status.  A hardcoded claim here could outlive the backend
                # that justified it.
                "hints": dict(caps.get("hints", {})),
                "status": True,
                "per_fragment_share": caps.get("per_fragment_share", False),
                "on_unenforceable": self.gateway.on_unenforceable,
            }
        }

    def prepare(
        self, body: str, auth: AuthContext, request_id: str
    ) -> Tuple[Optional[Dict], Optional[Dict], Optional[str]]:
        """Parse and apply policy.

        Returns (upstream_request, result, error).  Exactly one of result and
        error is set.  The upstream request is None when the intent was
        rejected.
        """
        try:
            request = loads_strict(body)
        except CacheIntentError as exc:
            return None, None, self._error("cache_intent_invalid", str(exc))
        except json.JSONDecodeError as exc:
            return None, None, self._error("cache_intent_invalid", f"malformed JSON: {exc}")

        try:
            fragments = extract_fragments(request)
        except CacheIntentError as exc:
            return None, None, self._error("cache_intent_invalid", str(exc))
        try:
            result = self.gateway.handle(fragments, auth, now=self.clock(), request_id=request_id)
        except UnsupportedVersion as exc:
            return None, None, self._error("cache_intent_unsupported_version", str(exc))
        except CacheIntentError as exc:
            return None, None, self._error("cache_intent_unenforceable", str(exc))
        except Exception as exc:  # schema validation and anything else
            return None, None, self._error("cache_intent_invalid", str(exc))

        upstream, _ = strip_binding_members(request)
        result.cache_salt = self._lower_request_salt(result, auth, request_id)
        if result.cache_salt is not None:
            upstream["cache_salt"] = result.cache_salt
        return upstream, result, None

    def salt_for_request(self, result, auth: AuthContext, request_id: str) -> Optional[str]:
        """Return the salt chosen while preparing this request.

        The arguments remain part of the public binding API, but lowering is
        performed once in `prepare` so status and the upstream request cannot
        disagree about the boundary actually used.
        """
        return result.cache_salt

    def _lower_request_salt(self, result, auth: AuthContext, request_id: str) -> Optional[str]:
        """Lower all effective policies onto one fail-closed request salt.

        Section 7 of the binding: an engine taking one partition per request
        can honor only the effective policy of the whole request, so this is
        the narrowest boundary any fragment reached. If any effective policy
        requires fresh computation or gateway fallback, the whole request is
        narrowed to its unique request partition.
        """
        backend = self.gateway.backend
        if not hasattr(backend, "salt_for"):
            return None
        governed = [s for s in result.statuses if s.effective_reuse is not None]
        if not governed:
            return None
        ladder = ["request", "session", "principal", "tenant", "public"]
        must_bypass = any(
            s.outcome == "bypass"
            or s.effective_reuse == "none"
            or s.effective_retention_mode == "forbid"
            for s in governed
        )
        narrowest = (
            "request"
            if must_bypass
            else min((s.effective_share for s in governed), key=ladder.index)
        )
        namespace = next(
            (s.namespace_identity for s in reversed(governed) if s.namespace_identity is not None),
            None,
        )
        from .auth import partition_for

        if narrowest == "request":
            for status in governed:
                status.outcome = "bypass"
                status.effective_share = "request"
                status.retention = (
                    "forbidden" if status.effective_retention_mode == "forbid" else "declined"
                )
        else:
            for status in governed:
                status.effective_share = narrowest

        return backend.salt_for(partition_for(narrowest, auth, request_id), namespace)

    @staticmethod
    def _error(code: str, message: str) -> str:
        return json.dumps({"error": {"code": code, "message": message, "type": "invalid_request"}})

    @staticmethod
    def attach_status(response: Dict, result) -> Dict:
        response = dict(response)
        response["cache_status"] = result.as_cache_status()["cache_status"]
        return response
