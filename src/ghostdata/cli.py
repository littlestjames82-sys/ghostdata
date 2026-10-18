"""GhostData CLI: pack, schema, check, mcp."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .core import DEFAULT_SAMPLE, analyze
from .pack import build_pack, check_pack
from .readers import DataError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ghostdata",
        description="Slice structured data files (CSV, TSV, JSON array, "
                    "top-level JSON object, JSONL) into a schema plus "
                    "bounded sample pack.")
    parser.add_argument("--version", action="version",
                        version="ghostdata " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_pack = sub.add_parser("pack", help="build a data pack")
    p_pack.add_argument("file")
    p_pack.add_argument("-o", "--out", required=True,
                        help="pack output directory")
    p_pack.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                        help="sample size in rows (default 60)")
    p_pack.add_argument("--no-redact", action="store_true",
                        help="disable sensitive-column redaction "
                             "(recorded in the manifest)")
    p_pack.add_argument("--format",
                        choices=["csv", "tsv", "json", "json-object",
                                 "jsonl"],
                        default=None, help="override format detection")

    p_schema = sub.add_parser("schema", help="print inferred schema as json")
    p_schema.add_argument("file")
    p_schema.add_argument("--no-redact", action="store_true")
    p_schema.add_argument("--format",
                          choices=["csv", "tsv", "json", "json-object",
                                   "jsonl"],
                          default=None)

    p_check = sub.add_parser("check", help="verify a pack against its manifest")
    p_check.add_argument("pack_dir")

    sub.add_parser("mcp", help="run the stdio MCP server")

    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            manifest = build_pack(
                args.file, args.out, sample_size=args.sample,
                redact=not args.no_redact, fmt=args.format)
            counts = manifest["counts"]
            print("GhostData pack: %s" % os.path.abspath(args.out))
            print("  format %s, rows parsed %d of %d (%d malformed)" % (
                manifest["source"]["format"], counts["rows_parsed"],
                counts["rows_total"], counts["rows_malformed"]))
            print("  sample %d rows, tokens sample %d vs full file %d "
                  "(estimates)" % (
                      manifest["sample"]["rows"],
                      manifest["tokens"]["sample_estimate"],
                      manifest["tokens"]["full_file_estimate"]))
            for col in manifest["redaction"]["columns"]:
                print("  redacted: %s (%s)" % (col["name"], col["reason"]))
            for col in manifest["redaction"].get("nested", []):
                print("  redacted (nested): %s (%s)"
                      % (col["name"], col["reason"]))
            if manifest["sample"].get("truncated"):
                print("  sample truncated to bounds (details in "
                      "manifest)")
            if not manifest["redaction"]["enabled"]:
                print("  redaction disabled by --no-redact (recorded "
                      "in manifest)")
            return 0
        if args.command == "schema":
            result = analyze(args.file, redact=not args.no_redact,
                             fmt=args.format)
            out = {
                "format": result["format"],
                "counts": result["counts"],
                "columns": result["schema"],
                "redacted_columns": result["redacted_columns"],
                "redacted_nested": result["redacted_nested"],
            }
            if result.get("structure") is not None:
                out["structure"] = result["structure"]
            print(json.dumps(out, indent=2))
            return 0
        if args.command == "check":
            result = check_pack(args.pack_dir)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        if args.command == "mcp":
            from .mcp_server import serve
            serve()
            return 0
    except (DataError, ValueError, OSError) as exc:
        print("ghostdata: %s" % exc, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
