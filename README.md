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
- [ ] Reconstruction validated against the exchange's own snapshots
      (top of book agrees 99.35% on the reference window; the residual is
      chunk 4's job)
- [ ] Maker/taker fee schedules
- [ ] Queue-position fill simulator
- [ ] Latency curve runner

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
