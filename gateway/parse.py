"""Strict request parsing.

The only job here that a JSON Schema cannot do: reject duplicate member
names.  `json.loads` silently keeps the last occurrence, which would let a
caller smuggle a second `share_max` past a validator that only ever sees the
survivor.
"""

import json
from typing import Any, Dict, List

from jsonschema import Draft202012Validator

from .errors import DuplicateMemberName, UnsupportedVersion

SUPPORTED_VERSION = 1


def _reject_duplicates(pairs: List[tuple]) -> Dict[str, Any]:
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise DuplicateMemberName(f"duplicate JSON member name: {key!r}")
        seen.add(key)
    return dict(pairs)


def loads_strict(text: str) -> Any:
    """Parse JSON, rejecting any object that repeats a member name."""
    return json.loads(text, object_pairs_hook=_reject_duplicates)


def parse_cache_intent(text: str, validator: Draft202012Validator) -> Dict[str, Any]:
    """Parse and validate one cache intent document.

    Raises DuplicateMemberName, UnsupportedVersion, or jsonschema's
    ValidationError.  Every one of those is a reject, never a downgrade.
    """
    doc = loads_strict(text)
    if isinstance(doc, dict):
        intent = doc.get("cache_intent")
        if isinstance(intent, dict):
            version = intent.get("version")
            if isinstance(version, int) and not isinstance(version, bool):
                if version != SUPPORTED_VERSION:
                    raise UnsupportedVersion(f"unsupported cache intent version: {version!r}")
    validator.validate(doc)
    return doc["cache_intent"]
