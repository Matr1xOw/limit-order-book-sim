"""A passive order in a real queue.

This is the part the repo exists for. A bar backtest fills a limit order when
price touches it. Here an order joins the back of a queue at its price level
and fills only once everything ahead of it is gone — which for a resting order
at the touch is frequently never, even on a day the market traded through that
price repeatedly.

Three things decide whether a passive order fills, and only the first is
visible in bar data:

  Whether the market traded at the price at all.

  How much size was already resting there when the order arrived. That is the
  queue, and it is why latency matters: every microsecond between deciding and
  arriving is more size joining ahead.

  What happened to the size in front. Trades consume the queue from the front,
  so they always help. Cancellations are the hard case and are modelled
  explicitly below.

What is deliberately not modelled: market impact. A simulated order rests in
the book without anyone else reacting to it, which is a reasonable
approximation for small size in a liquid name and a bad one otherwise. It also
means results are optimistic in a way the queue model is not — nobody steps in
front of an order that no one can see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from book import Book
from fees import NASDAQ, FeeSchedule
from messages import BUY, SELL, Action, Message


class CancelModel(Enum):
    """Where in the queue a cancellation is assumed to have come from.

    The feed says a hundred shares were withdrawn at a price. It does not say
    whether they were in front of our order or behind it, and no amount of
    care with the data will recover that — the information is not there. So
    it is an assumption, and the honest thing is to make it an axis and report
    results across it rather than to pick one and hide it in a comment.
    """

    BEHIND = "behind"
    """
    Assume cancels come from behind us: queue position never improves.

    The pessimistic bound. Wrong in reality — some of those orders were
    certainly ahead — but it cannot flatter a strategy, which makes it the
    right default for a tool whose purpose is to catch flattering results.
    """

    AHEAD = "ahead"
    """
    Assume cancels come from in front: every withdrawal advances us.

    The optimistic bound. Reality sits between this and BEHIND, so a strategy
    that loses money here loses money for certain.
    """

    PROPORTIONAL = "proportional"
    """
    Assume cancels are spread uniformly through the queue.

    A cancellation of `n` shares at a level holding `total` advances us by
    `n * ahead / total`. The most defensible single choice, and the one to
    quote if only one number can be quoted — but it is still an assumption,
    and the gap between the three bounds is a real measure of how much any
    conclusion depends on it.
    """


class Status(Enum):
    PENDING = "pending"
    """Submitted, still in flight. Not yet in anyone's book."""

    RESTING = "resting"
    """Arrived and queued."""

    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    """Withdrawn by its own lifetime without filling completely."""


@dataclass
class Order:
    """One simulated passive order."""

    side: int
    price: int
    size: int
    submitted: int
    """Decision time, in nanoseconds."""

    arrives: int
    """When it reaches the exchange. `submitted` plus latency."""

    expires: int | None = None
    """
    When to give up, or None to rest forever.

    Resting forever is what makes a naive fill-rate measurement useless: over
    a long enough window the market wanders through almost any price, so
    nearly everything eventually fills and queue position stops mattering. A
    lifetime is what turns "did this ever fill" into "did this fill while the
    quote was still worth having", which is the question a strategy asks.
    """

    status: Status = Status.PENDING
    ahead: int = 0
    """Shares queued in front. The number that decides everything."""

    filled: int = 0
    fill_cost: int = 0
    """Fees paid, in nanodollars. Negative is a rebate collected."""

    first_fill: int | None = None
    """Timestamp of the first partial fill, or None."""

    initial_ahead: int | None = None
    """Queue length on arrival, kept for reporting."""

    @property
    def remaining(self) -> int:
        return self.size - self.filled

    @property
    def wait(self) -> int | None:
        """Nanoseconds from arrival to first fill, or None if never filled."""
        return None if self.first_fill is None else self.first_fill - self.arrives


@dataclass
class Simulation:
    """Aggregate outcome of a run."""

    orders: list[Order] = field(default_factory=list)
    messages: int = 0

    @property
    def resting(self) -> list[Order]:
        """Orders that reached the exchange, whatever became of them after."""
        return [o for o in self.orders if o.status is not Status.PENDING]

    @property
    def expired(self) -> list[Order]:
        return [o for o in self.orders if o.status is Status.EXPIRED]

    @property
    def filled(self) -> list[Order]:
        return [o for o in self.orders if o.filled > 0]

    @property
    def fill_rate(self) -> float:
        """Share of arrived orders that got any fill at all."""
        arrived = self.resting
        return len(self.filled) / len(arrived) if arrived else 0.0

    @property
    def completion(self) -> float:
        """Share of submitted volume that actually traded."""
        wanted = sum(o.size for o in self.resting)
        return sum(o.filled for o in self.resting) / wanted if wanted else 0.0

    @property
    def fees(self) -> int:
        """Total fees across all fills. Negative is a net rebate."""
        return sum(o.fill_cost for o in self.orders)

    @property
    def median_wait(self) -> int | None:
        """Median nanoseconds from arrival to first fill, among fills.

        The median rather than the mean: these distributions have long right
        tails, and one order that sat for a minute would drag an average into
        describing something typical never does.
        """
        waits = sorted(o.wait for o in self.filled if o.wait is not None)
        if not waits:
            return None
        middle = len(waits) // 2
        if len(waits) % 2:
            return waits[middle]
        return (waits[middle - 1] + waits[middle]) // 2


class FillSimulator:
    """Replays a message stream with simulated passive orders resting in it."""

    def __init__(
        self,
        *,
        book: Book | None = None,
        latency: int = 0,
        cancels: CancelModel = CancelModel.BEHIND,
        schedule: FeeSchedule = NASDAQ,
    ):
        """
        `latency` is nanoseconds between submitting and arriving. It is the
        single most informative knob here: queue position is decided at
        arrival, so latency does not merely delay a fill, it changes which
        fills are reachable at all.
        """
        self.book = book if book is not None else Book(strict=False)
        self.latency = latency
        self.cancels = cancels
        self.schedule = schedule
        self.simulation = Simulation()
        self._pending: list[Order] = []
        self._resting: list[Order] = []
        # Order ids seen in a fill print, so the removal that follows can be
        # told apart from an ordinary cancellation. The feed emits both as a
        # withdrawal of size, but they mean opposite things for a queue: a
        # trade consumed the front, a cancel might have come from anywhere.
        self._executing: set[int] = set()

    def submit(
        self,
        ts: int,
        side: int,
        price: int,
        size: int,
        lifetime: int | None = None,
    ) -> Order:
        """Place a passive order, decided at `ts`, arriving `latency` later.

        `lifetime` is nanoseconds to rest *after arriving* before giving up.
        Measured from arrival rather than from submission so that latency
        does not quietly shorten the order's time in the queue as well as
        worsening its position — those are two different penalties, and
        conflating them would overstate the cost of being slow.
        """
        if size <= 0:
            raise ValueError(f"size must be positive, got {size}")
        if side not in (BUY, SELL):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        if lifetime is not None and lifetime < 0:
            raise ValueError(f"lifetime must not be negative, got {lifetime}")
        arrives = ts + self.latency
        order = Order(
            side=side,
            price=price,
            size=size,
            submitted=ts,
            arrives=arrives,
            expires=None if lifetime is None else arrives + lifetime,
        )
        self._pending.append(order)
        self.simulation.orders.append(order)
        return order

    def cancel(self, order: Order) -> None:
        """Withdraw an order that has not fully filled."""
        if order.status in (Status.FILLED, Status.CANCELLED):
            return
        order.status = Status.CANCELLED
        if order in self._pending:
            self._pending.remove(order)
        if order in self._resting:
            self._resting.remove(order)

    def process(self, message: Message) -> None:
        """Advance one message: admit arrivals, work the queue, apply to book."""
        self.simulation.messages += 1
        self._admit(message.ts)
        self._expire(message.ts)

        # Queue effects are computed against the book as it stands *before*
        # this message applies. An execution consumes size that was resting a
        # moment ago, and asking the book afterwards would ask about a level
        # that has already shrunk.
        if message.action is Action.TRADE and message.order_id:
            self._executing.add(message.order_id)
        elif message.action is Action.CANCEL:
            execution = message.order_id in self._executing
            self._executing.discard(message.order_id)
            self._consume(message, execution=execution)

        self.book.apply(message)

    def _admit(self, now: int) -> None:
        """Move orders that have arrived into the queue."""
        still_pending = []
        for order in self._pending:
            if order.arrives > now:
                still_pending.append(order)
                continue
            # Everything visible at this price is in front of us. Joining a
            # level means joining the back of it.
            order.ahead = self.book.size_at(order.side, order.price)
            order.initial_ahead = order.ahead
            order.status = Status.RESTING
            self._resting.append(order)
        self._pending = still_pending

    def _expire(self, now: int) -> None:
        """Retire orders that have outlived their usefulness.

        A partially filled order still expires. The shares that traded stay
        traded — this is a cancellation of the remainder, not an unwind.
        """
        for order in list(self._resting):
            if order.expires is not None and order.expires <= now:
                order.status = Status.EXPIRED
                self._resting.remove(order)

    def _consume(self, message: Message, *, execution: bool) -> None:
        """Apply a removal at some price to any of our orders resting there."""
        for order in list(self._resting):
            if order.side != message.side or order.price != message.price:
                continue
            if execution:
                self._trade_through(order, message)
            else:
                self._cancel_ahead(order, message)

    def _trade_through(self, order: Order, message: Message) -> None:
        """A trade consumed size at our level, always from the front."""
        shares = message.size
        eaten = min(order.ahead, shares)
        order.ahead -= eaten
        shares -= eaten
        if shares <= 0:
            return
        # The queue in front is gone; what is left of this trade hits us.
        fill = min(shares, order.remaining)
        if fill <= 0:
            return
        order.filled += fill
        order.fill_cost += self.schedule.cost(fill, maker=True)
        if order.first_fill is None:
            order.first_fill = message.ts
        if order.remaining == 0:
            order.status = Status.FILLED
            self._resting.remove(order)

    def _cancel_ahead(self, order: Order, message: Message) -> None:
        """Someone withdrew size at our level. Whose, we cannot know."""
        if self.cancels is CancelModel.BEHIND:
            return
        if self.cancels is CancelModel.AHEAD:
            order.ahead = max(0, order.ahead - message.size)
            return
        total = self.book.size_at(order.side, order.price)
        if total <= 0:
            return
        # Uniformly spread: our share of the withdrawal is our share of the
        # queue. Integer arithmetic throughout, rounding down, so the
        # assumption cannot quietly manufacture progress.
        order.ahead = max(0, order.ahead - message.size * order.ahead // total)


def run(
    messages,
    orders,
    *,
    book: Book | None = None,
    latency: int = 0,
    cancels: CancelModel = CancelModel.BEHIND,
    schedule: FeeSchedule = NASDAQ,
    lifetime: int | None = None,
) -> Simulation:
    """Replay `messages`, submitting `orders` as their decision times pass.

    `orders` is a sequence of `(ts, side, price, size)`, in time order. They
    are submitted when the stream reaches their decision time, so a strategy's
    own latency is applied to it rather than assumed away.
    """
    simulator = FillSimulator(
        book=book, latency=latency, cancels=cancels, schedule=schedule
    )
    queue = list(orders)
    index = 0
    for message in messages:
        while index < len(queue) and queue[index][0] <= message.ts:
            simulator.submit(*queue[index], lifetime=lifetime)
            index += 1
        simulator.process(message)
    return simulator.simulation
