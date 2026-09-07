# Sample data goes here

Empty on purpose. Market data is not ours to redistribute and is too large for
git regardless — everything in this directory except this file is ignored.

## LOBSTER is no longer the source

The free LOBSTER samples this repo was originally built against are gone. The
sample page now requires proof of purchase of *Trades, Quotes and Prices* plus
an institutional academic e-mail address, and reserves the right to decline
anyone else. `lobster.py` still reads the format and its tests still pass, but
you cannot get the data without clearing that gate.

## Databento is

[Databento](https://databento.com) gives new accounts $125 of credit, which is
far more than this needs. The reason it replaces LOBSTER cleanly is that one
symbol-window can be requested under two schemas:

  `mbo`     every order message — the input
  `mbp-10`  the top ten levels  — an independent answer key

That pairing is the whole validation strategy, and it is the thing LOBSTER was
originally chosen for.

## What to request

Dataset `XNAS.ITCH` (Nasdaq TotalView-ITCH), one symbol, a **thirty-minute
window**, CSV encoding. Then repeat the identical request with the other
schema. The two must cover the same symbol and the same start/end nanosecond,
or the validator compares different slices of the day and reports nonsense —
check `metadata.json` in each download, the `query` blocks should differ only
in `schema`.

A worked example, and the file this repo was developed against:

    symbol   AAPL
    start    2026-09-02T14:30:00Z
    end      2026-09-02T15:00:00Z
    schemas  mbo, then mbp-10

That is 455k messages and 176k book updates, about 140 MB uncompressed, and
cost under five cents of the credit.

**Keep the window small.** MBO is billed by volume and a full day of a liquid
name is gigabytes. Thirty minutes is ample to validate a reconstruction, and
nothing stops you pulling more once it works.

## Unzipping

Downloads arrive as a job directory holding a `.csv.zst` plus three JSON
files. Copy the CSVs here and decompress:

    zstd -d data/xnas-itch-*.mbo.csv.zst
    zstd -d data/xnas-itch-*.mbp-10.csv.zst

## What a mid-session window costs you

A window starting at 14:30 begins with the book already full, and no snapshot
precedes the first message. Roughly 0.3% of cancels and fills in the example
above refer to orders placed before it opens, which cannot be matched.

The reconstruction therefore seeds itself from the first `mbp-10` row rather
than starting empty, and is validated on the top ten levels only — the depth
the answer key can actually speak to. Starting from the session open would
remove the orphans but not the depth limit, and costs considerably more data.
