# GhostData

Structured data slicer for GhostSlice. Point it at a data file that is
too big to hand an agent whole, a 40 MB CSV export or a JSONL log with
200,000 events, and it gives back a small pack instead: the schema, the
true row counts, and a bounded, fair sample, with sensitive columns
redacted.

GhostSlice governs what an agent reads from code, GhostDocs what it
reads from documents, GhostData what it reads from datasets.

## What it does

- Infers the schema by streaming the file: field names, inferred
  types, null counts and rates, distinct counts, numeric ranges, and
  string length ranges.
- Builds a sample pack: head rows, tail rows, and evenly spaced rows
  from the middle, so the sample is not just the first screen of a
  sorted file. The sample size and strategy are stated in the pack.
- Counts malformed rows and reports the first offending line numbers.
  Nothing is silently skipped.
- Redacts sensitive columns by default, by column name and by value
  shape, and records each redaction with its reason.
- Never modifies the source file. It only reads the source and writes
  the pack.

Supported formats: CSV, TSV, JSON arrays, JSONL (newline JSON), and
JSON files whose top level is a single object (the config shape: a
`settings.json`, a `package.json`, another tool's manifest). An
object is treated as one record: its keys are the schema and the
sample is the object itself, bounded as described below.
Flat text files only. No databases, no spreadsheets, no binary
formats.

## Commands

Print the inferred schema as JSON:

    ghostdata schema data.csv

Build a pack (manifest.json, PROMPT.md, and the sample file under
files/):

    ghostdata pack data.csv -o data-pack
    ghostdata pack events.jsonl -o events-pack --sample 100

Verify a pack against its manifest (sample hash and count
consistency):

    ghostdata check data-pack

Run the stdio MCP server (tools: data_schema and data_pack, summary
payloads, sample rows not returned by default):

    ghostdata mcp

Options that matter:

- `--sample N` sets the total sample size. Default is 60.
- `--no-redact` turns redaction off. Use it only on files you own and
  trust. The manifest records that redaction was disabled.
- `--format csv|tsv|json|jsonl|json-object` overrides format
  detection. Plain `json` dispatches on the file's top-level shape:
  an array streams, an object is read as one record.

## Redaction rules

A column is redacted when its name matches a sensitive pattern
(password, token, secret, api key, email, phone, ssn, card, credential
and kin), or when a stated share of its values matches a sensitive
shape: an email address, a phone number, an SSN, or a payment card
number that passes a Luhn check.

Redacted values appear in the sample as [REDACTED]. The manifest names
each redacted column and gives the reason only, name match or shape
match. No sensitive value is printed, sampled, or stored, and min and
max are suppressed for redacted columns.

The same rules apply inside nested values: a key with a sensitive
name, or a string or number with a sensitive shape, at any depth
inside an object or array value is redacted in the sample and
recorded in the manifest by its path (for example
`database.password`).

## The pack

- `manifest.json`: source file name, format, byte size and sha256;
  row counts (total, parsed, malformed, with example line numbers);
  the per-column schema; the redaction record (columns, plus nested
  values by path); the sample description (file, rows, strategy, row
  indices, sha256, and for object samples the truncation record);
  and token estimates for the sample and the full file with the
  estimator method stamped. Object packs also record the object's
  structure: top-level key count, total key count, and maximum
  container depth.
- `PROMPT.md`: the same story in words an agent can read, schema
  table plus the sample in its native format.
- `files/<name>.sample.<ext>`: the sample itself.

Token figures are estimates from the GhostSlice estimator when it is
available, or from a documented local fallback with its own method
string. The method is always stamped, so the two are never confused.

## Honest limits

- Types are inferred, never declared, and labelled that way. A column
  of numeric strings infers as numbers; a date in an unusual format
  infers as a string. A digit string with a leading zero (a ZIP code,
  a padded id) infers as a string, because the zeros are part of the
  code, not a quantity.
- Distinct counts are capped at 10,000 values per column. Past the
  cap the count is reported as capped, a lower bound, never as exact.
- Shape detection can miss obfuscated sensitive values and can
  over-redact a look-alike column. The manifest shows the reason so a
  human can judge. Shape checks run on string values, and on native
  JSON integers for the card shape only (a bare number has no
  separators, so the email, phone, and SSN shapes cannot apply).
- A malformed element in the middle of a JSON array ends the parse;
  it is reported as malformed and the rest of the file is uncounted.
  That is stated in the manifest, not hidden.
- Files with no recognised extension are sniffed from their first
  bytes and treated as delimited text or JSON. A binary file with a
  data extension would be read as text with replacement characters;
  this tool is for flat text data only.
- The sample bound is exact, including a sample of 1 row (the head
  row). The test suite pins this at several bounds.
- Nested values are typed object or array and are not flattened or
  distinct-counted.
- A top-level JSON object is parsed whole, because its key set
  cannot be known incrementally. To keep that bounded, an object
  file over 16 MiB is refused with the guard stated in the error;
  array-shaped JSON streams and has no such limit. The object sample
  is bounded structurally (container depth 6, 50 keys per nested
  level, 20 items per array, 1000 characters per string; the top
  level's own keys are never dropped, they are the schema), and any
  truncation the bounds cause is counted and stated in the manifest
  and in PROMPT.md.
- A JSON file whose top level is a scalar (a bare number, string,
  boolean, or null) is refused: it has no fields to describe and no
  rows to sample. Sniffing a file with no recognised extension still
  treats a leading { as JSONL, so pass an extensionless object file
  with --format json or --format json-object.

## Development

Zero dependencies, Python standard library only, Python 3.11 and up.

    python3 -m pytest tests

Layout: `src/ghostdata/` is the core (Agent A), `tests/`,
`examples/`, and this README are the test and docs surface (Agent B).
See `PLAN.md` for the design and `REPORT_B.md` for the test record.
