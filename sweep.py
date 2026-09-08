"""What latency and queue position are actually worth.

The question the repo exists to ask, run as an experiment. A trivial passive
strategy — quote both sides at the touch, give up after a fixed lifetime — is
swept across latencies and cancellation assumptions, and its fills are marked
out against the mid a second later.

Markout, not spread capture, because the whole hazard of resting passively is
adverse selection: you get filled precisely when someone better informed wants
the other side, and the position is already underwater at the moment it opens.
Counting half the spread as profit assumes the fill was free information. It
is not, and the difference between those two accountings is most of the gap
between a strategy that backtests well and one that makes money.

Two accounting notes, both deliberate:

  Fees are applied afterwards rather than simulated per venue. A fill is a
  fill regardless of the schedule, so the same set is priced under each. This
  is not a shortcut — it is what makes the comparison exact, since any
  difference between the columns is the fee structure and nothing else.

  The mid timeline is built once and shared. Simulated orders never enter the
  real book, so the market is identical in every run; recomputing it per
  configuration would only invite the two to drift apart.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from book import Book, replay
from databento import find_pair, load_messages, load_snapshots
from fees import FREE, NASDAQ, FeeSchedule
from messages import BUY, PRICE_SCALE, SELL, Message, Snapshot
from simulator import CancelModel, FillSimulator

NS_PER_MS = 1_000_000
NS_PER_SECOND = 1_000_000_000


@dataclass
class Timeline:
    """Mid price over time, for marking fills out against."""

    times: list[int]
    mids: list[int]

    def mid_at(self, ts: int) -> int | None:
        """The most recent mid at or before `ts`, or None if too early.

        Deliberately not interpolated. A mid between two book states is a
        price that never existed, and inventing one would smooth exactly the
        jumps that adverse selection consists of.
        """
        index = bisect_right(self.times, ts)
        return self.mids[index - 1] if index else None


def build_timeline(messages: list[Message], seed: Snapshot | None) -> Timeline:
    """Record the mid after every message that moves the top of book."""
    book = Book.seeded(seed) if seed is not None else Book(strict=False)
    times: list[int] = []
    mids: list[int] = []
    last: int | None = None
    for message, book in replay(messages, book=book):
        bid, ask = book.best_bid, book.best_ask
        if bid is None or ask is None:
            continue
        mid = (bid + ask) // 2
        if mid != last:
            times.append(message.ts)
            mids.append(mid)
            last = mid
    return Timeline(times=times, mids=mids)


@dataclass
class Outcome:
    """One configuration's result."""

    latency: int
    cancels: CancelModel
    submitted: int = 0
    filled: int = 0
    shares: int = 0
    gross: int = 0
    """Markout P&L before fees, in nanodollars."""

    queue_on_arrival: list[int] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.queue_on_arrival is None:
            self.queue_on_arrival = []

    @property
    def fill_rate(self) -> float:
        return self.filled / self.submitted if self.submitted else 0.0

    @property
    def median_queue(self) -> int:
        if not self.queue_on_arrival:
            return 0
        ordered = sorted(self.queue_on_arrival)
        return ordered[len(ordered) // 2]

    def net(self, schedule: FeeSchedule) -> int:
        """Markout P&L after paying `schedule`'s maker rate on every fill."""
        return self.gross - schedule.cost(self.shares, maker=True)

    def per_share(self, schedule: FeeSchedule) -> float:
        """Net P&L per filled share, in cents. The number worth comparing."""
        if not self.shares:
            return 0.0
        return 100 * self.net(schedule) / self.shares / PRICE_SCALE


def sweep_one(
    messages: list[Message],
    seed: Snapshot | None,
    timeline: Timeline,
    *,
    latency: int,
    cancels: CancelModel,
    interval: int,
    lifetime: int,
    horizon: int,
    size: int,
) -> Outcome:
    """Quote both sides at the touch every `interval`, and mark the fills out."""
    outcome = Outcome(latency=latency, cancels=cancels)
    simulator = FillSimulator(
        book=Book.seeded(seed) if seed is not None else Book(strict=False),
        latency=latency,
        cancels=cancels,
        # Priced afterwards, so the simulation itself stays fee-neutral.
        schedule=FREE,
    )

    next_decision = messages[0].ts
    live: list = []
    for message in messages:
        if message.ts >= next_decision:
            bid, ask = simulator.book.best_bid, simulator.book.best_ask
            # A window can open before both sides exist, and the very first
            # message always arrives at a book we have not applied anything
            # to yet. Quoting nothing there is fine; consuming the decision
            # slot is not, because the strategy would then sit out a whole
            # interval for no reason and the fill rate would understate.
            if bid is not None or ask is not None:
                for side, price in ((BUY, bid), (SELL, ask)):
                    if price is None:
                        continue
                    live.append(
                        simulator.submit(
                            message.ts, side, price, size, lifetime=lifetime
                        )
                    )
                    outcome.submitted += 1
                next_decision = message.ts + interval
        simulator.process(message)

    for order in live:
        if order.initial_ahead is not None:
            outcome.queue_on_arrival.append(order.initial_ahead)
        if not order.filled:
            continue
        after = timeline.mid_at(order.first_fill + horizon)
        if after is None:
            continue
        outcome.filled += 1
        outcome.shares += order.filled
        # Bought below the later mid, or sold above it, is a gain.
        edge = after - order.price if order.side == BUY else order.price - after
        outcome.gross += edge * order.filled
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", nargs="?", default="data", type=Path)
    parser.add_argument(
        "--latency-ms",
        type=float,
        nargs="+",
        default=[0, 0.1, 1, 10, 100],
        help="latencies to sweep, in milliseconds",
    )
    parser.add_argument("--interval-ms", type=float, default=1000)
    parser.add_argument("--lifetime-ms", type=float, default=1000)
    parser.add_argument("--horizon-ms", type=float, default=1000)
    parser.add_argument("--size", type=int, default=100)
    args = parser.parse_args(argv)

    mbo_path, mbp10_path = find_pair(args.directory)
    messages = load_messages(mbo_path)
    snapshots = load_snapshots(mbp10_path)
    by_sequence = {s.sequence: s for s in snapshots}

    start = next(
        (i for i, m in enumerate(messages) if m.sequence in by_sequence), None
    )
    seed = None
    if start is not None:
        sequence = messages[start].sequence
        seed = by_sequence[sequence]
        while start < len(messages) and messages[start].sequence == sequence:
            start += 1
        messages = messages[start:]

    timeline = build_timeline(messages, seed)
    span = (messages[-1].ts - messages[0].ts) / NS_PER_SECOND
    print(
        f"{len(messages)} messages over {span:.0f}s, "
        f"{len(timeline.times)} distinct mids\n"
    )
    print(
        f"quoting {args.size} shares both sides every {args.interval_ms:g}ms, "
        f"cancelling after {args.lifetime_ms:g}ms, "
        f"marked out {args.horizon_ms:g}ms later\n"
    )

    header = (
        f"{'latency':>9} {'cancels':>13} {'queue':>7} {'fills':>7} "
        f"{'fill rate':>10} {'free':>10} {'nasdaq':>10}"
    )
    print(header)
    print("-" * len(header))

    for cancels in (CancelModel.BEHIND, CancelModel.PROPORTIONAL):
        for latency_ms in args.latency_ms:
            outcome = sweep_one(
                messages,
                seed,
                timeline,
                latency=int(latency_ms * NS_PER_MS),
                cancels=cancels,
                interval=int(args.interval_ms * NS_PER_MS),
                lifetime=int(args.lifetime_ms * NS_PER_MS),
                horizon=int(args.horizon_ms * NS_PER_MS),
                size=args.size,
            )
            print(
                f"{latency_ms:>7g}ms {cancels.value:>13} "
                f"{outcome.median_queue:>7} {outcome.filled:>7} "
                f"{100 * outcome.fill_rate:>9.1f}% "
                f"{outcome.per_share(FREE):>9.3f}c "
                f"{outcome.per_share(NASDAQ):>9.3f}c"
            )
        print()

    print("Per-share numbers are cents of markout P&L per filled share.")
    print("'free' charges nothing; 'nasdaq' adds the 20 mil maker rebate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
