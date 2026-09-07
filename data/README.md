# Sample data goes here

Empty on purpose. LOBSTER sample files are not ours to redistribute, and they
are too large for git regardless — everything in this directory except this
file is ignored.

## What to download

From [lobsterdata.com/info/DataSamples.php](https://lobsterdata.com/info/DataSamples.php),
take any one ticker-day. The free samples cover 2012-06-21 for a handful of
Nasdaq names at several depths.

**Pick the 10-level export.** Depth 1 is too shallow to say anything about
queues below the touch, and 50 is a much larger download that buys nothing the
validator or the fill simulator currently use.

**Pick a liquid name first.** AAPL or MSFT give a busy book with deep queues,
which is where queue position actually bites. INTC and the thinner names are
useful later as a contrast — a strategy that survives on AAPL and dies on a
wide-spread name is telling you something — but start where the effect is
largest.

## What to unzip into this directory

A ticker-day is a pair of headerless CSVs that must stay together:

```
data/
  AAPL_2012-06-21_34200000_57600000_message_10.csv
  AAPL_2012-06-21_34200000_57600000_orderbook_10.csv
```

Keep LOBSTER's filenames. `lobster.find_pair` reads the level count out of the
`_10` suffix and derives the orderbook path from the message path, so renaming
them breaks the loader. It deliberately refuses to guess when a directory holds
more than one pair — put a second ticker-day in its own subdirectory rather
than alongside the first.

## Why both files

The message file is the input; the orderbook file is LOBSTER's own
reconstruction of the same stream, row-aligned to it. Having both is the whole
reason this project starts here: replaying the messages and diffing against
their book turns "my reconstruction looks right" into a claim that can fail.

A simulator nobody can check is worth nothing.
