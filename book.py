"""Reconstruction of the visible limit order book from a message stream.

The book is a pair of price-to-size maps. That is all it needs to be for queue
accounting: an order's position depends on how much size sits at its own price
level, and nothing about the rest of the book changes that.

What is deliberately *not* modelled:

  Hidden liquidity. A trade that consumed no visible size was never in anyone's
  queue, so it cannot have been ahead of you. The consequence is that real
  fills arrive slightly sooner than simulated ones on venues with meaningful
  hidden flow — an error in the pessimistic direction, which is the only kind
  worth having.

  Individual orders. Feeds give order IDs, but a queue is FIFO by size and
  tracking identities would buy nothing except the ability to get them wrong.
  `Action.MODIFY` is the one event this costs us, and it is rejected rather
  than guessed at.
"""

from __future__ import annotations

from messages import BUY, SELL, Action, Message, Snapshot


class BookError(Exception):
    """The message stream and the book disagree about what is resting."""


class Book:
    """The visible book, advanced one message at a time."""

    __slots__ = ("bids", "asks", "_strict", "orphans")

    def __init__(self, *, strict: bool = True):
        """
        `strict` decides what happens when a message removes more size than
        the book holds at that price. Replayed from a genuine feed start that
        is impossible, so it raises by default rather than papering over a
        desync that would corrupt every fill downstream.

        Pass `strict=False` for a window that opens mid-session, where the
        book was already full and early cancels refer to orders placed before
        the first row. Those removals are counted in `orphans` rather than
        silently ignored — an orphan rate that climbs above a fraction of a
        percent means the seed was wrong, not that the session was busy.
        """
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self._strict = strict
        self.orphans = 0

    @classmethod
    def seeded(cls, snapshot: Snapshot, *, strict: bool = False) -> "Book":
        """A book pre-loaded with a published snapshot.

        The only honest way to start mid-session. Note what it does not fix:
        a ten-level snapshot seeds ten levels, so anything deeper stays unknown
        until it trades or is cancelled into view.
        """
        book = cls(strict=strict)
        for price, size in snapshot.bids:
            book.bids[price] = size
        for price, size in snapshot.asks:
            book.asks[price] = size
        return book

    def side(self, direction: int) -> dict[int, int]:
        """The price-to-size map for `BUY` or `SELL`."""
        if direction == BUY:
            return self.bids
        if direction == SELL:
            return self.asks
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    def size_at(self, direction: int, price: int) -> int:
        """Visible size resting at one price level. Zero if the level is empty."""
        return self.side(direction).get(price, 0)

    @property
    def best_bid(self) -> int | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> int | None:
        return min(self.asks) if self.asks else None

    @property
    def crossed(self) -> bool:
        """True if the best bid is at or above the best ask.

        Never legitimate in a continuous book. A useful invariant to assert
        against when no independent snapshot is available to diff.
        """
        bid, ask = self.best_bid, self.best_ask
        return bid is not None and ask is not None and bid >= ask

    def levels(
        self, direction: int, depth: int | None = None
    ) -> tuple[tuple[int, int], ...]:
        """Occupied levels on one side, best first."""
        book_side = self.side(direction)
        prices = sorted(book_side, reverse=(direction == BUY))
        if depth is not None:
            prices = prices[:depth]
        return tuple((price, book_side[price]) for price in prices)

    def snapshot(self, levels: int) -> Snapshot:
        """The book in the shape a reader's snapshots come in.

        Lets a reconstruction be compared against the feed's own answer key by
        plain equality, which is what `validate.py` does.
        """
        return Snapshot(asks=self.levels(SELL, levels), bids=self.levels(BUY, levels))

    def apply(self, message: Message) -> None:
        """Advance the book by one message."""
        action = message.action
        if action is Action.ADD:
            self._add(message.side, message.price, message.size)
        elif action in (Action.CANCEL, Action.EXECUTE):
            # Both remove resting size, and `side` names the side the resting
            # order was on. An execution against a bid is a sell aggressing;
            # the size leaves the bid either way.
            self._remove(message.side, message.price, message.size)
        elif action is Action.CLEAR:
            self.bids.clear()
            self.asks.clear()
        elif action in (Action.TRADE, Action.STATUS):
            # Tape only. Nothing visible was resting, so nothing leaves.
            pass
        elif action is Action.MODIFY:
            raise BookError(
                "MODIFY needs the order's previous price and size, which an "
                "aggregate book does not keep — add order tracking to support it"
            )
        else:
            raise BookError(f"unhandled action {action!r}")

    def _add(self, direction: int, price: int, size: int) -> None:
        if size <= 0:
            return
        book_side = self.side(direction)
        book_side[price] = book_side.get(price, 0) + size

    def _remove(self, direction: int, price: int, size: int) -> None:
        if size <= 0:
            return
        book_side = self.side(direction)
        resting = book_side.get(price, 0)
        if size > resting:
            if self._strict:
                name = "bid" if direction == BUY else "ask"
                raise BookError(
                    f"removing {size} from {name} {price} which holds {resting}"
                )
            # The order was resting before the window opened. Take what is
            # there and record that we could not account for the rest.
            self.orphans += 1
            book_side.pop(price, None)
            return
        if size == resting:
            del book_side[price]
        else:
            book_side[price] = resting - size


def replay(messages, *, strict: bool = True, book: Book | None = None):
    """Yield `(message, book)` after each message is applied.

    The same `Book` instance is yielded every time — it is mutable state being
    advanced, not a sequence of independent values. Callers that need to keep
    a state must copy it via `snapshot`.

    Pass `book` to continue from a seeded state; otherwise replay starts empty.
    """
    if book is None:
        book = Book(strict=strict)
    for message in messages:
        book.apply(message)
        yield message, book
