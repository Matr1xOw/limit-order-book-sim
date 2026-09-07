# limit-order-book-sim

A limit order book reconstruction and fill simulator, built to answer one
question: **how much of a backtest's profit is an artifact of pretending you
always get filled?**

Bar-level backtests ask "did price touch my limit?" and book a fill if it did.
Real passive orders join the back of a queue at their price level and fill only
once everything ahead of them is consumed. Between those two models sits most
of the edge in a lot of published strategies.

This repo replays real message-by-message exchange data, reconstructs the
visible book exactly, and fills simulated orders according to their actual
queue position — including the part where your order arrives late.

## Status

Early. See the roadmap below for what exists and what does not.

- [x] Repo scaffold
- [x] LOBSTER message and snapshot readers
- [x] Databento MBO and MBP-10 readers
- [x] Visible book reconstruction
- [x] Reconstruction validated against the exchange's own snapshots
- [x] Maker/taker fee schedules
- [x] Queue-position fill simulator
- [ ] Latency curve runner

## Is the reconstruction right?

Yes, on every level it can account for. `python3 validate.py` replays the
message stream and diffs it against the exchange's own published book at each
event boundary — 170,304 comparisons over the reference window:

    depth 1   exact  98.38%   trusted 100.000% over 49.9% of levels
    depth 5   exact  93.07%   trusted 100.000% over 45.8% of levels
    depth 10  exact  82.11%   trusted 100.000% over 40.5% of levels

Two numbers, because one of them is not the code's fault. A window that opens
mid-session inherits a book it never saw built, and a ten-level seed inherits
only ten levels of it. Sizes at every other price are unknown — not wrong,
unknown — and they stay unknown, because a level whose count we watch reach
zero may still hold shares we never knew were there.

**Exact** counts those as failures, which is why it falls away with depth: the
deeper you look, the more of the seed's blind spot you are looking at.

**Trusted** counts only levels whose entire history the replay watched, and it
is 100.000% with zero conflicts. That is the number that says the code is
right, and it is reported beside its coverage because a `trusted` predicate
strict enough to exclude everything would also read 100%.

Getting there cost two bugs, both found by the validator rather than by
reading. The reconstruction double-counted every execution — Databento emits
three rows per trade and only one of them removes size. And the validator
itself seeded from a snapshot and then replayed the event that snapshot
already included, applying it twice.

## The assumption the data cannot settle

The feed says a hundred shares were withdrawn at a price. It does not say
whether they were in front of your order or behind it, and that information is
not recoverable — it was never published. Since queue position is the whole
question, the simulator makes this an axis rather than a buried constant:

    BEHIND        cancels never help you. The pessimistic bound, and the
                  default, because it cannot flatter a strategy.
    AHEAD         every cancel advances you. The optimistic bound.
    PROPORTIONAL  cancels are spread uniformly through the queue.

Reality lies between the first two. A conclusion that survives all three is
robust to the assumption; one that only holds under AHEAD is an artifact of it.

## The three things bar backtests get wrong

**Queue position.** You are behind everyone already resting at your price when
you arrive. A limit order at the touch may never fill even though the market
traded there all day, because the queue ahead of you never fully cleared.

**Latency.** A signal computed at time *t* does not reach the exchange until
*t + δ*. Every microsecond of δ is more size joining the queue in front of you.
Results here are reported as a curve over δ rather than a single number,
because "profitable at 1ms, dead at 50ms" is the finding, not a footnote.

**Fees.** Maker rebates and taker fees are per-share and they are not small
relative to the edge being claimed. A strategy can be a rebate-capture scheme
wearing a signal's clothes, and you cannot tell without modelling them.

## Data

[Databento](https://databento.com) Nasdaq TotalView-ITCH, requested twice over
one symbol-window: `mbo` for the order messages, `mbp-10` for the top ten
levels. See [`data/README.md`](data/README.md) for the exact request.

Asking for both is the point. The message stream is the input and the book is
an independent answer key, so the reconstruction is falsifiable: replay the
messages, compare against the exchange's own book, and any disagreement is a
bug in this repo. A simulator nobody can check is worth nothing.

This was originally built against [LOBSTER](https://lobsterdata.com), whose
free samples are now gated behind proof of purchase of a particular book and
an institutional academic e-mail. `lobster.py` still reads that format. Two
readers behind one message vocabulary is now a deliberate property rather than
an accident — market data access moves, and when it does the cost should be a
reader rather than a rewrite.

## Prior art in the author's own work

This grew out of [lp-stock-signals](https://github.com/Matr1xOw/lp-stock-signals),
a chart-pattern signal desk whose backtester is honest about lookahead and
intrabar ambiguity but still assumes touch-equals-fill. That assumption is the
thing being replaced here.

## Running

```sh
python3 -m unittest discover -v
```

No dependencies.
