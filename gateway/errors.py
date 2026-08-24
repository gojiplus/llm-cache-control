"""Errors the gateway raises when it cannot proceed under the draft's rules.

Each maps to a requirement, so the type carries the reason and callers do
not have to parse messages.
"""


class CacheIntentError(Exception):
    """Base class. The gateway rejected the cache intent."""


class DuplicateMemberName(CacheIntentError):
    """A JSON object carried the same member name twice.

    Section "Constraints and Hints" requires rejection.  json.loads keeps
    the last occurrence silently, so this must be caught at parse time.
    """


class UnsupportedVersion(CacheIntentError):
    """The cache intent declares a version this gateway does not implement."""


class DuplicateFragmentId(CacheIntentError):
    """Two fragments in one request declared the same `id`.

    Section "Identifier" requires uniqueness within a request.  Status
    correlation is positional, so a duplicate is not ambiguous to an
    implementation, but a caller indexing the status array by `id` silently
    loses an entry.
    """


class UnenforceableConstraint(CacheIntentError):
    """The backend cannot enforce a constraint the application supplied.

    The gateway must bypass caching or reject rather than proceed under a
    weaker constraint.
    """


class ReorderNotPermitted(Exception):
    """A caller asked to reorder model-visible fragments for cache reuse.

    Section "Conservative Composition" forbids this unless the host protocol
    or application has declared the alternative order semantically
    equivalent.
    """
