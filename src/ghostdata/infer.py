"""Schema inference over streamed values.

Types are inferred, never declared, and labelled that way in output.
Distinct counting is capped: past the cap the count is a lower bound
and the column is flagged distinct_capped.
"""
from __future__ import annotations

import re

from . import sensitive

DISTINCT_CAP = 10000
NULL_TOKENS = {"", "null", "none", "na", "n/a", "nil", "~"}
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


def is_null(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in NULL_TOKENS:
        return True
    return False


def classify(value):
    """Return (type_name, numeric_value_or_None) for a non-null value."""
    if isinstance(value, bool):
        return "boolean", None
    if isinstance(value, int):
        return "integer", value
    if isinstance(value, float):
        return "float", value
    if isinstance(value, dict):
        return "object", None
    if isinstance(value, (list, tuple)):
        return "array", None
    if isinstance(value, str):
        s = value.strip()
        if s.lower() in ("true", "false"):
            return "boolean", None
        if _INT.match(s):
            digits = s.lstrip("+-")
            if len(digits) > 1 and digits.startswith("0"):
                # A leading zero makes a digit string a code, not a
                # quantity (ZIP codes, padded ids): int() would drop
                # the zeros and invent a numeric range the data never
                # had. A lone "0" still classifies as integer.
                return "string", None
            try:
                return "integer", int(s)
            except ValueError:
                pass
        if _FLOAT.match(s):
            try:
                return "float", float(s)
            except ValueError:
                pass
        if _DATE.match(s):
            return "date", None
        return "string", None
    return "string", None


class ColumnStats:
    def __init__(self, name: str):
        self.name = name
        self.non_null = 0
        self.nulls = 0
        self.types = {}
        self.distinct = set()
        self.distinct_capped = False
        self.num_min = None
        self.num_max = None
        self.len_min = None
        self.len_max = None
        self.strings_checked = 0
        self.shape_checked = 0
        self.shape_hits = {}

    def add(self, value) -> None:
        if is_null(value):
            self.nulls += 1
            return
        self.non_null += 1
        kind, numeric = classify(value)
        self.types[kind] = self.types.get(kind, 0) + 1
        key = _distinct_key(value)
        if key is not None:
            if not self.distinct_capped:
                if len(self.distinct) < DISTINCT_CAP:
                    self.distinct.add(key)
                else:
                    self.distinct_capped = True
        if numeric is not None:
            self.num_min = numeric if self.num_min is None else min(self.num_min, numeric)
            self.num_max = numeric if self.num_max is None else max(self.num_max, numeric)
        if isinstance(value, str):
            self.strings_checked += 1
            self.shape_checked += 1
            ln = len(value)
            self.len_min = ln if self.len_min is None else min(self.len_min, ln)
            self.len_max = ln if self.len_max is None else max(self.len_max, ln)
            shape = sensitive.shape_of(value)
            if shape:
                self.shape_hits[shape] = self.shape_hits.get(shape, 0) + 1
        elif isinstance(value, int) and not isinstance(value, bool):
            # Native JSON numbers get the one shape check that can
            # apply to them (card, via sensitive.shape_of_int), so a
            # card number stored as a JSON number is not exempt from
            # redaction just because it was never a string.
            self.shape_checked += 1
            shape = sensitive.shape_of_int(value)
            if shape:
                self.shape_hits[shape] = self.shape_hits.get(shape, 0) + 1

    @property
    def total(self) -> int:
        return self.non_null + self.nulls

    def inferred_type(self) -> str:
        if not self.types:
            return "null"
        kinds = set(self.types)
        if kinds == {"integer", "float"}:
            return "float"
        if len(kinds) == 1:
            return next(iter(kinds))
        return "mixed"

    def redaction(self):
        """Return the redaction reason for this column, or None."""
        reason = sensitive.name_reason(self.name)
        if reason:
            return reason
        return sensitive.shape_reason(self.shape_hits, self.shape_checked)

    def to_dict(self, redacted_reason=None) -> dict:
        total = self.total
        out = {
            "name": self.name,
            "type": self.inferred_type(),
            "types_seen": dict(sorted(self.types.items())),
            "null_count": self.nulls,
            "null_rate": round(self.nulls / total, 6) if total else 0.0,
            "distinct_count": len(self.distinct),
            "distinct_capped": self.distinct_capped,
        }
        if redacted_reason:
            out["redacted"] = True
            out["redaction_reason"] = redacted_reason
            out["min"] = None
            out["max"] = None
        else:
            out["redacted"] = False
            if self.num_min is not None:
                out["min"] = self.num_min
                out["max"] = self.num_max
            if self.len_min is not None:
                out["min_length"] = self.len_min
                out["max_length"] = self.len_max
        return out


def _distinct_key(value):
    try:
        if isinstance(value, (dict, list, tuple)):
            return None  # nested values are not distinct-counted
        return (type(value).__name__, value)
    except Exception:
        return None
