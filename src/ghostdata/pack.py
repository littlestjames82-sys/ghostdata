"""Pack writer and checker: manifest.json + PROMPT.md + sample file."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from datetime import datetime, timezone

from . import __version__
from .core import analyze, file_sha256, full_token_estimate
from .tokens import estimate_tokens, method as token_method

REDACTED = "[REDACTED]"


def render_sample(result: dict) -> str:
    fmt = result["format"]
    cols = result["columns"]
    rows = result["sample"]["_rows"]
    if fmt in ("csv", "tsv"):
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter="\t" if fmt == "tsv" else ",",
                            lineterminator="\n")
        writer.writerow(cols)
        for row in rows:
            writer.writerow([_cell(v) for v in row])
        return buf.getvalue()
    if fmt == "jsonl":
        lines = []
        for row in rows:
            if cols == ["value"]:
                lines.append(json.dumps(row[0]))
            else:
                lines.append(json.dumps(dict(zip(cols, row))))
        return "\n".join(lines) + ("\n" if lines else "")
    if fmt == "json-object":
        # The sample is the object itself (already redacted and
        # bounded by core.analyze), rebuilt in column order.
        data = dict(zip(cols, rows[0])) if rows else {}
        return json.dumps(data, indent=2) + "\n"
    # json array
    if cols == ["value"]:
        data = [row[0] for row in rows]
    else:
        data = [dict(zip(cols, row)) for row in rows]
    return json.dumps(data, indent=2) + "\n"


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def sample_ext(fmt: str) -> str:
    return {"csv": ".csv", "tsv": ".tsv",
            "jsonl": ".jsonl", "json": ".json",
            "json-object": ".json"}[fmt]


def build_pack(path: str, out_dir: str, sample_size: int = 60,
               redact: bool = True, fmt: str = None) -> dict:
    path = os.path.abspath(path)
    if os.path.abspath(out_dir) == path:
        raise ValueError("output directory must not be the source file")
    result = analyze(path, sample_size=sample_size, redact=redact, fmt=fmt)
    sample_text = render_sample(result)
    base = os.path.splitext(os.path.basename(path))[0]
    sample_name = "%s.sample%s" % (base, sample_ext(result["format"]))

    os.makedirs(os.path.join(out_dir, "files"), exist_ok=True)
    sample_path = os.path.join(out_dir, "files", sample_name)
    with open(sample_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(sample_text)

    sample_tokens = estimate_tokens(sample_text)
    full_tokens = full_token_estimate(path)
    is_object = result["format"] == "json-object"
    truncation = result["sample"].get("_truncation")
    if is_object:
        strategy_text = ("whole object: the file is a single record, "
                         "sampled in full and bounded structurally")
    elif result["sample"]["strategy"].get("all_rows"):
        strategy_text = "all rows (file smaller than sample size)"
    else:
        strategy_text = "head + tail + evenly spaced middle"
    manifest = {
        "tool": "ghostdata",
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "file": os.path.basename(path),
            "format": result["format"],
            "bytes": os.path.getsize(path),
            "sha256": file_sha256(path),
        },
        "counts": result["counts"],
        "columns": result["schema"],
        "redaction": {
            "enabled": redact,
            "columns": result["redacted_columns"],
            "nested": result.get("redacted_nested", []),
        },
        "sample": {
            "file": "files/" + sample_name,
            "rows": result["sample"]["rows"],
            "size_requested": result["sample"]["size_requested"],
            "strategy": strategy_text,
            "strategy_counts": result["sample"]["strategy"],
            "row_indices": result["sample"]["row_indices"],
            "truncated": bool(truncation and truncation["truncated"]),
            "sha256": hashlib.sha256(
                sample_text.encode("utf-8")).hexdigest(),
        },
        "tokens": {
            "sample_estimate": sample_tokens,
            "full_file_estimate": full_tokens,
            "method": token_method(),
            "note": "estimates, not counts; full-file figure is a chunked "
                    "sum over the streamed file",
        },
    }
    if result.get("structure") is not None:
        # Top-level JSON objects carry their shape facts: key counts
        # and container depth, so the schema's honesty about nesting
        # is on the record.
        manifest["structure"] = result["structure"]
    if truncation is not None:
        manifest["sample"]["truncation"] = truncation
    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(out_dir, "PROMPT.md"), "w",
              encoding="utf-8") as fh:
        fh.write(render_prompt(manifest, sample_text))
    return manifest


def render_prompt(manifest: dict, sample_text: str) -> str:
    src = manifest["source"]
    counts = manifest["counts"]
    lines = ["# Data pack: %s" % src["file"], ""]
    if src["format"] == "json-object":
        structure = manifest.get("structure", {})
        lines.append(
            "Format: json-object. The file's top level is a single "
            "JSON object, treated as one record: its %d keys are the "
            "schema below (%d keys in total counting nested objects, "
            "maximum container depth %d)." % (
                structure.get("top_level_keys", 0),
                structure.get("total_keys", 0),
                structure.get("max_depth", 0)))
        lines.append(
            "Sample: the object itself, redacted and structurally "
            "bounded. It is the whole record, not a row sample.")
        truncation = manifest["sample"].get("truncation") or {}
        if manifest["sample"].get("truncated"):
            lines.append(
                "Sample truncation: the object exceeded the sample "
                "bounds (%d key(s) and %d item(s) dropped, %d value(s) "
                "replaced at the depth limit, %d string(s) truncated). "
                "Details and limits are recorded in manifest.json." % (
                    truncation.get("keys_dropped", 0),
                    truncation.get("items_dropped", 0),
                    truncation.get("depth_truncations", 0),
                    truncation.get("strings_truncated", 0)))
    else:
        lines.append(
            "Format: %s. Rows parsed: %d of %d total (%d malformed, "
            "counted and reported in manifest.json)." % (
                src["format"], counts["rows_parsed"],
                counts["rows_total"], counts["rows_malformed"]))
        lines.append(
            "Sample: %d rows (head + tail + evenly spaced middle) of "
            "%d parsed rows. The sample is a sample, not the file." % (
                manifest["sample"]["rows"], counts["rows_parsed"]))
    lines += [
        "Token estimates (%s): sample %d, full file %d." % (
            manifest["tokens"]["method"],
            manifest["tokens"]["sample_estimate"],
            manifest["tokens"]["full_file_estimate"]),
        "",
        "## Schema (types inferred, not declared)",
        "",
        "| column | type | null rate | distinct | notes |",
        "|---|---|---|---|---|",
    ]
    for col in manifest["columns"]:
        notes = []
        if col.get("redacted"):
            notes.append("REDACTED: " + col["redaction_reason"])
        if col.get("distinct_capped"):
            notes.append("distinct count capped, lower bound")
        if "min" in col and col["min"] is not None:
            notes.append("min %s, max %s" % (col["min"], col["max"]))
        lines.append("| %s | %s | %s | %s%s | %s |" % (
            col["name"], col["type"], col["null_rate"],
            col["distinct_count"],
            "+" if col.get("distinct_capped") else "",
            "; ".join(notes)))
    if manifest["redaction"]["columns"]:
        lines += ["", "Redacted columns are shown as %s in the sample. "
                  "Reasons are recorded in manifest.json." % REDACTED]
    if manifest["redaction"].get("nested"):
        lines += ["", "Values nested inside object or array values "
                  "were also redacted where a key name or a value "
                  "shape matched; each is recorded by path in "
                  "manifest.json."]
    if not manifest["redaction"]["enabled"]:
        lines += ["", "Redaction was disabled for this pack by an "
                  "explicit flag."]
    lines += ["", "## Sample (%s)" % manifest["sample"]["file"], "",
              "```", sample_text.rstrip("\n"), "```", ""]
    return "\n".join(lines)


def check_pack(pack_dir: str) -> dict:
    errors = []
    manifest_path = os.path.join(pack_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return {"ok": False, "errors": ["manifest.json not found"]}
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except Exception as exc:
        return {"ok": False, "errors": ["manifest.json unreadable: %s" % exc]}
    sample_rel = manifest.get("sample", {}).get("file", "")
    sample_path = os.path.join(pack_dir, sample_rel)
    if not os.path.isfile(sample_path):
        errors.append("sample file missing: %s" % sample_rel)
    else:
        digest = hashlib.sha256()
        with open(sample_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
        if digest.hexdigest() != manifest["sample"].get("sha256"):
            errors.append("sample file sha256 does not match manifest")
    counts = manifest.get("counts", {})
    if counts.get("rows_total") != (
            counts.get("rows_parsed", 0) + counts.get("rows_malformed", 0)):
        errors.append("counts inconsistent: total != parsed + malformed")
    if manifest.get("sample", {}).get("rows", 0) > counts.get(
            "rows_parsed", 0):
        errors.append("sample has more rows than the file parsed")
    return {"ok": not errors, "errors": errors,
            "files_checked": 1 if not errors else 0}
