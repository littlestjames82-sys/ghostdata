"""Sensitive column detection: by name and by value shape.

A column is redacted when its name matches a sensitive pattern, or when
enough of its values match a sensitive shape (email, phone, payment
card with a Luhn check, SSN). Shape checks run on string values, and
on native JSON integers for the card shape only. Detection stores
counters only, never the values themselves.
"""
from __future__ import annotations

import re

NAME_PATTERNS = [
    (re.compile(r"pass(word|wd|phrase)?", re.I), "column name matches password"),
    (re.compile(r"secret", re.I), "column name matches secret"),
    (re.compile(r"token", re.I), "column name matches token"),
    (re.compile(r"api[-_]?key|apikey", re.I), "column name matches api key"),
    (re.compile(r"credential", re.I), "column name matches credential"),
    (re.compile(r"auth[-_]?code|otp|pin[-_]?code", re.I), "column name matches auth code"),
    (re.compile(r"e[-_]?mail", re.I), "column name matches email"),
    (re.compile(r"phone|mobile|tel(ephone)?", re.I), "column name matches phone"),
    (re.compile(r"ssn|social[-_]?security", re.I), "column name matches ssn"),
    (re.compile(r"card", re.I), "column name matches card"),
    (re.compile(r"credit", re.I), "column name matches credit"),
    (re.compile(r"cvv|cvc", re.I), "column name matches cvv"),
    (re.compile(r"iban|bank[-_]?account", re.I), "column name matches bank account"),
    (re.compile(r"private[-_]?key", re.I), "column name matches private key"),
]

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
_SSN = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_PHONE_CHARS = re.compile(r"^\+?[\d\s().-]+$")
_CARD_CHARS = re.compile(r"^[\d\s-]+$")


def _luhn_ok(digits: str) -> bool:
    total = 0
    alt = False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def shape_of(value: str):
    """Return the sensitive shape a string value matches, or None."""
    v = value.strip()
    if not v or len(v) > 254:
        return None
    if _EMAIL.match(v):
        return "email"
    if _SSN.match(v):
        return "ssn"
    digits = re.sub(r"\D", "", v)
    if _CARD_CHARS.match(v) and 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "card"
    # Phone requires at least one separator or a leading +: bare digit
    # strings are numeric data (ids, squares, counters) far more often
    # than they are phone numbers, and redacting numeric columns on a
    # digit count alone was a measured false positive. Name-based
    # detection still covers columns named phone/mobile/tel.
    if not v.isdigit() and _PHONE_CHARS.match(v) and 10 <= len(digits) <= 15:
        letters = re.sub(r"[\d\s().+-]", "", v)
        if not letters:
            return "phone"
    return None


def shape_of_int(value: int):
    """Return the sensitive shape a native JSON integer matches, or None.

    Only the card shape can apply. A bare integer carries no
    separators, so the email and SSN shapes cannot match, and phone
    detection deliberately requires separators (see shape_of). Card
    numbers stored as JSON numbers do occur, and the Luhn check keeps
    the false-positive rate at the same level as the string path.
    Negative values are never card numbers.
    """
    if value < 0:
        return None
    digits = str(value)
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "card"
    return None


def name_reason(column_name: str):
    for pattern, reason in NAME_PATTERNS:
        if pattern.search(column_name or ""):
            return reason
    return None


REDACTED = "[REDACTED]"


def redact_nested(value, path=""):
    """Redact sensitive values inside a nested object or array value.

    Returns (new_value, hits), where hits is a list of (path, reason)
    for every replacement, with paths rooted at ``path`` (a key name
    extends the path with ".key", a list index with "[i]"). The rules
    are the column rules applied at any depth: a key whose name matches
    a sensitive name pattern has its whole value replaced; a string
    matching a sensitive shape is replaced; a native integer matching
    the card shape (via shape_of_int) is replaced. Anything else is
    returned structurally identical, so a value with no sensitive
    content round-trips unchanged.
    """
    hits = []
    if isinstance(value, dict):
        out = {}
        for key, sub in value.items():
            sub_path = "%s.%s" % (path, key) if path else str(key)
            reason = name_reason(str(key))
            if reason:
                out[key] = REDACTED
                hits.append((sub_path, reason))
                continue
            new_sub, sub_hits = redact_nested(sub, sub_path)
            out[key] = new_sub
            hits.extend(sub_hits)
        return out, hits
    if isinstance(value, (list, tuple)):
        out = []
        for i, sub in enumerate(value):
            new_sub, sub_hits = redact_nested(sub, "%s[%d]" % (path, i))
            out.append(new_sub)
            hits.extend(sub_hits)
        return out, hits
    if isinstance(value, str):
        shape = shape_of(value)
        if shape:
            return REDACTED, [(path, "value matches %s shape" % shape)]
        return value, hits
    if isinstance(value, int) and not isinstance(value, bool):
        shape = shape_of_int(value)
        if shape:
            return REDACTED, [(path, "value matches %s shape" % shape)]
        return value, hits
    return value, hits


# A column is shape-redacted when at least this share of its checked
# string values match one shape, with a small absolute floor so one
# stray look-alike in a huge column does not trigger it alone.
SHAPE_MIN_HITS = 2
SHAPE_MIN_RATIO = 0.2


def shape_reason(shape_hits: dict, strings_checked: int):
    if strings_checked <= 0:
        return None
    for shape, hits in sorted(shape_hits.items()):
        if hits >= SHAPE_MIN_HITS and hits / strings_checked >= SHAPE_MIN_RATIO:
            return "values match %s shape (%d of %d checked)" % (
                shape, hits, strings_checked)
        if hits == strings_checked and hits >= 1:
            return "values match %s shape (%d of %d checked)" % (
                shape, hits, strings_checked)
    return None
