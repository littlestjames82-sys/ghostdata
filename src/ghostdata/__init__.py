"""GhostData: structured data slicer for GhostSlice.

Reads CSV, TSV, JSON array, and JSONL files as a stream, infers a schema,
and writes a Slice-style pack: manifest.json, PROMPT.md, and a bounded
sample file (head + tail + evenly spaced rows). Sensitive columns are
redacted in samples by default. Source files are never modified.
"""

__version__ = "0.1.0"
