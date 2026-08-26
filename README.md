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
- [ ] LOBSTER message and snapshot readers
- [ ] Visible book reconstruction
- [ ] Reconstruction validated against LOBSTER's own snapshots
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

Built against [LOBSTER](https://lobsterdata.com) sample files, which are free
and cover a handful of Nasdaq names for one day each. They are not redistributed
here — download them yourself and drop them in `data/`.

LOBSTER is the right starting point specifically because it ships *both* the
message stream and its own reconstructed book snapshots. That makes the
reconstruction falsifiable: replay the messages, compare against their
snapshots, and any disagreement is a bug in this repo. A simulator nobody can
check is worth nothing.

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
