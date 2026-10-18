"""Streaming readers for CSV, TSV, JSON arrays, JSONL, and top-level
JSON objects.

Every reader is re-openable (two passes: stats, then sample) and never
holds the file whole. Rows are yielded aligned to a fixed column list.
Malformed rows are yielded as Malformed records with line numbers,
counted by the caller, never silently skipped.

The one exception to "never holds the file whole" is a top-level JSON
object: there is no way to know its keys without parsing it, so
JsonObjectReader parses it whole under an explicit size guard
(MAX_OBJECT_BYTES). Config-shaped JSON is small by nature; the guard
keeps the exception bounded and stated rather than silent.
"""
from __future__ import annotations

import csv
import io
import json
import os

CHUNK = 65536

# Top-level JSON objects are parsed whole (see module docstring). This
# is the stated guard: 16 MiB is far above any real config file and
# bounds the parsed structure a single object can make us hold.
MAX_OBJECT_BYTES = 16 * 1024 * 1024


class DataError(Exception):
    pass


class Malformed:
    def __init__(self, line, reason):
        self.line = line
        self.reason = reason

    def __repr__(self):
        return "Malformed(line=%r, reason=%r)" % (self.line, self.reason)


def detect_format(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return "csv"
    if ext in (".tsv", ".tab"):
        return "tsv"
    if ext in (".jsonl", ".ndjson"):
        return "jsonl"
    if ext == ".json":
        return "json"
    # Sniff: first non-whitespace character decides JSON shape,
    # otherwise guess the delimiter from the first line.
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        head = fh.read(4096)
    stripped = head.lstrip()
    if stripped.startswith("["):
        return "json"
    if stripped.startswith("{"):
        return "jsonl"
    first_line = head.splitlines()[0] if head.splitlines() else ""
    return "tsv" if first_line.count("\t") > first_line.count(",") else "csv"


class DelimitedReader:
    def __init__(self, path, fmt, token_sink=None):
        self.path = path
        self.format = fmt
        self.delimiter = "\t" if fmt == "tsv" else ","
        self.token_sink = token_sink
        self._columns = None

    def _tap(self, fh):
        sink = self.token_sink
        for line in fh:
            if sink is not None:
                sink.feed(line)
            yield line

    def columns(self):
        if self._columns is not None:
            return self._columns
        with open(self.path, "r", encoding="utf-8", errors="replace",
                  newline="") as fh:
            reader = csv.reader(fh, delimiter=self.delimiter)
            for row in reader:
                if row:
                    self._columns = _dedupe_names(
                        [c.strip() or "col_%d" % (i + 1)
                         for i, c in enumerate(row)])
                    return self._columns
        raise DataError("empty file: no header row")

    def rows(self):
        """Yield (values list) or Malformed, one per data row."""
        cols = self.columns()
        with open(self.path, "r", encoding="utf-8", errors="replace",
                  newline="") as fh:
            reader = csv.reader(self._tap(fh), delimiter=self.delimiter)
            header_seen = False
            for row in reader:
                if not header_seen:
                    header_seen = True
                    continue
                if not row:
                    yield Malformed(reader.line_num, "blank")
                    continue
                if len(row) != len(cols):
                    yield Malformed(
                        reader.line_num,
                        "expected %d fields, found %d" % (len(cols), len(row)))
                    continue
                yield row


class JsonlReader:
    format = "jsonl"

    def __init__(self, path, token_sink=None):
        self.path = path
        self.token_sink = token_sink
        self._columns = None
        self.value_mode = False

    def _scan(self, collect_columns):
        fields = []
        seen = set()
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if self.token_sink is not None:
                    self.token_sink.feed(line)
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    for key in item:
                        if key not in seen:
                            seen.add(key)
                            fields.append(key)
                else:
                    self.value_mode = True
        return fields

    def columns(self):
        if self._columns is None:
            fields = self._scan(collect_columns=True)
            if self.value_mode and not fields:
                self._columns = ["value"]
            elif self.value_mode:
                self._columns = fields  # mixed: non-dicts are malformed
            else:
                self._columns = fields
            if not self._columns:
                raise DataError("no records found")
        return self._columns

    def rows(self):
        cols = self.columns()
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                if self.token_sink is not None:
                    self.token_sink.feed(line)
                if not line.strip():
                    yield Malformed(line_no, "blank")
                    continue
                try:
                    item = json.loads(line)
                except Exception as exc:
                    yield Malformed(line_no, "invalid json: %s" % exc)
                    continue
                if isinstance(item, dict):
                    yield [item.get(c) for c in cols]
                elif self.value_mode and len(cols) == 1 and cols[0] == "value":
                    yield [item]
                else:
                    yield Malformed(line_no, "record is not a json object")


class JsonArrayReader:
    format = "json"

    def __init__(self, path, token_sink=None):
        self.path = path
        self.token_sink = token_sink
        self._columns = None
        self.value_mode = False
        self.truncated = False
        self.truncation_note = None

    def _items(self):
        """Yield parsed array elements; set self.truncated on a bad one."""
        self.truncated = False
        self.truncation_note = None
        decoder = json.JSONDecoder()
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            buf = ""
            pos = 0
            eof = False

            def fill():
                nonlocal buf, pos, eof
                if eof:
                    return False
                chunk = fh.read(CHUNK)
                if not chunk:
                    eof = True
                    return False
                if self.token_sink is not None:
                    self.token_sink.feed(chunk)
                if pos > CHUNK:
                    buf = buf[pos:]
                    pos = 0
                buf += chunk
                return True

            fill()
            while pos < len(buf) and buf[pos] in " \t\r\n":
                pos += 1
            if pos >= len(buf) or buf[pos] != "[":
                raise DataError("top level is not a json array")
            pos += 1
            index = 0
            expect_value = True
            while True:
                while True:
                    while pos < len(buf) and buf[pos] in " \t\r\n,":
                        if buf[pos] == ",":
                            if not expect_value and index > 0:
                                expect_value = True
                        pos += 1
                    if pos < len(buf):
                        break
                    if not fill():
                        break
                if pos >= len(buf):
                    if index == 0:
                        return
                    self.truncated = True
                    self.truncation_note = (
                        "array ended without closing bracket after %d elements"
                        % index)
                    return
                if buf[pos] == "]":
                    return
                try:
                    item, end = decoder.raw_decode(buf, pos)
                except Exception as exc:
                    if fill():
                        continue
                    self.truncated = True
                    self.truncation_note = (
                        "element %d could not be parsed (%s); "
                        "rest of file uncounted" % (index + 1, exc))
                    return
                pos = end
                index += 1
                expect_value = False
                yield item

    def columns(self):
        if self._columns is None:
            fields = []
            seen = set()
            for item in self._items():
                if isinstance(item, dict):
                    for key in item:
                        if key not in seen:
                            seen.add(key)
                            fields.append(key)
                else:
                    self.value_mode = True
            if self.value_mode and not fields:
                self._columns = ["value"]
            else:
                self._columns = fields
            if not self._columns:
                raise DataError("no records found in json array")
        return self._columns

    def rows(self):
        cols = self.columns()
        idx = 0
        for item in self._items():
            idx += 1
            if isinstance(item, dict):
                yield [item.get(c) for c in cols]
            elif self.value_mode and cols == ["value"]:
                yield [item]
            else:
                yield Malformed(idx, "element is not a json object")
        if self.truncated:
            yield Malformed(idx + 1, self.truncation_note or "truncated array")


def _depth(value) -> int:
    """Container depth: a scalar is 0, an empty container is 1, and a
    container is 1 plus the deepest child depth."""
    if isinstance(value, dict):
        if not value:
            return 1
        return 1 + max(_depth(v) for v in value.values())
    if isinstance(value, list):
        if not value:
            return 1
        return 1 + max(_depth(v) for v in value)
    return 0


def _count_keys(value) -> int:
    total = 0
    if isinstance(value, dict):
        total += len(value)
        for v in value.values():
            total += _count_keys(v)
    elif isinstance(value, list):
        for v in value:
            total += _count_keys(v)
    return total


class JsonObjectReader:
    """A JSON file whose top level is an object (config-shaped JSON).

    The object is a single record: its keys are the column list and its
    one row is the values. Nested values stay nested (typed object or
    array by the inference pass, never flattened), exactly as nested
    values inside array records are treated.

    Unlike the streaming readers this parses the file whole, under the
    MAX_OBJECT_BYTES guard, because an object's key set cannot be known
    incrementally. The parse is cached, so the stats and sample passes
    share it.
    """

    format = "json-object"
    top_level = "object"
    object_mode = True

    def __init__(self, path, token_sink=None):
        self.path = path
        self.token_sink = token_sink
        self.truncated = False
        self.truncation_note = None
        self._data = None
        self._columns = None
        self.structure = None

    def _load(self):
        if self._data is not None:
            return self._data
        size = os.path.getsize(self.path)
        if size > MAX_OBJECT_BYTES:
            raise DataError(
                "json object file is %d bytes; top-level objects are "
                "parsed whole under a %d byte (16 MiB) guard, so this "
                "file is refused. Array-shaped JSON streams and has no "
                "such limit" % (size, MAX_OBJECT_BYTES))
        parts = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                if self.token_sink is not None:
                    self.token_sink.feed(chunk)
                parts.append(chunk)
        text = "".join(parts)
        if not text.strip():
            raise DataError("empty file: no json value found")
        try:
            data = json.loads(text)
        except Exception as exc:
            raise DataError("invalid json: %s" % exc)
        if not isinstance(data, dict):
            raise DataError("top level is not a json object")
        self._data = data
        self._columns = list(data.keys())
        self.structure = {
            "top_level_keys": len(self._columns),
            "total_keys": _count_keys(data),
            "max_depth": _depth(data),
        }
        return data

    def columns(self):
        self._load()
        if not self._columns:
            raise DataError("json object has no keys: nothing to describe")
        return self._columns

    def rows(self):
        cols = self.columns()
        data = self._load()
        yield [data.get(c) for c in cols]


def peek_json_shape(path: str) -> str:
    """Return the first non-whitespace character of a JSON file, or
    an empty string for an empty or whitespace-only file."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            chunk = fh.read(4096)
            if not chunk:
                return ""
            for ch in chunk:
                if ch not in " \t\r\n":
                    return ch


def open_reader(path, fmt=None, token_sink=None):
    fmt = fmt or detect_format(path)
    if fmt in ("csv", "tsv"):
        return DelimitedReader(path, fmt, token_sink=token_sink)
    if fmt == "jsonl":
        return JsonlReader(path, token_sink=token_sink)
    if fmt == "json-object":
        return JsonObjectReader(path, token_sink=token_sink)
    if fmt == "json":
        # "json" names the family; the top-level shape picks the
        # reader. Arrays stream; objects parse whole under a guard;
        # a bare scalar is not a dataset and is refused by name.
        shape = peek_json_shape(path)
        if shape == "[":
            return JsonArrayReader(path, token_sink=token_sink)
        if shape == "{":
            return JsonObjectReader(path, token_sink=token_sink)
        if shape == "":
            raise DataError("empty file: no json value found")
        raise DataError(
            "top level is a json scalar, not an array or object: a "
            "lone number, string, boolean, or null has no fields to "
            "describe and no rows to sample")
    raise DataError("unsupported format: %s" % fmt)


def _dedupe_names(names):
    seen = {}
    out = []
    for name in names:
        if name in seen:
            seen[name] += 1
            out.append("%s#%d" % (name, seen[name]))
        else:
            seen[name] = 1
            out.append(name)
    return out
