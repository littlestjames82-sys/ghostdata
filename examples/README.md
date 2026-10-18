# GhostData examples

Two small data files and the packs GhostData builds from them. Every
value in these files is fake and made up for the example.

## customers.csv and customers-pack/

A six-row CSV export. The `email` column is redacted by name, so the
pack shows the shape of the data without a single address:

    python3 -m ghostdata pack examples/customers.csv -o examples/customers-pack
    python3 -m ghostdata check examples/customers-pack

Open `customers-pack/PROMPT.md` to see the schema table and the
sample as an agent would receive it, and `customers-pack/manifest.json`
for the full counts, the redaction record, and the sample hash.

## events.jsonl and events-pack/

A five-line JSONL event log. The `api_token` column is redacted by
name; the token values in the source are example-only strings and
none of them appear anywhere in the pack:

    python3 -m ghostdata pack examples/events.jsonl -o examples/events-pack
    python3 -m ghostdata check examples/events-pack

## Try the schema command

    python3 -m ghostdata schema examples/customers.csv

That prints the inferred schema as JSON without writing a pack.
