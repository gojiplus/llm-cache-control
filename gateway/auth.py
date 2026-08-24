"""Authenticated scope binding.

The draft is blunt about this: "A client-provided tenant, principal,
session, group, or handle is not authorization."  So the gateway derives
every partition from the authenticated context and never from the request
body.  Caller-supplied coordinates are stripped before anything downstream
sees them.
"""

from dataclasses import dataclass
from typing import Any, Dict, Tuple

# Fields a caller might send hoping the gateway treats them as scope.
UNTRUSTED_SCOPE_FIELDS = (
    "tenant",
    "principal",
    "session",
    "group",
    "handle",
    "cache_partition",
    "cache_salt",
)


@dataclass(frozen=True)
class AuthContext:
    """What the gateway proved about the caller, not what the caller claimed."""

    tenant: str
    principal: str
    session: str


def strip_untrusted_scope(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Tuple[str, ...]]:
    """Remove caller-supplied scope coordinates.

    Returns the cleaned payload and the names that were stripped, so the
    gateway can report the attempt rather than silently discarding it.
    """
    stripped = tuple(f for f in UNTRUSTED_SCOPE_FIELDS if f in payload)
    cleaned = {k: v for k, v in payload.items() if k not in UNTRUSTED_SCOPE_FIELDS}
    return cleaned, stripped


def partition_for(share_max: str, auth: AuthContext, request_id: str) -> str:
    """Map an authorized sharing boundary onto a physical cache partition.

    Every branch except `public` reads from the authenticated context, which
    is what makes a caller unable to select another tenant by naming it.
    """
    if share_max == "public":
        return "public"
    if share_max == "tenant":
        return f"tenant:{auth.tenant}"
    if share_max == "principal":
        return f"tenant:{auth.tenant}/principal:{auth.principal}"
    if share_max == "session":
        return f"tenant:{auth.tenant}/principal:{auth.principal}/session:{auth.session}"
    if share_max == "request":
        return f"request:{request_id}"
    raise ValueError(f"unknown share_max: {share_max!r}")
