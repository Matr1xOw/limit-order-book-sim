"""Reconstruction of the visible limit order book from a message stream.

The book is a pair of price-to-size maps. That is all it needs to be for
queue accounting: an order's position depends on how much size sits at its
own price level, and nothing about the rest of the book changes that.

What is deliberately *not* modelled:

  Hidden liquidity. `EXECUTE_HIDDEN` prints on the tape but was never in the
  book, so it cannot have been ahead of you in a visible queue. It is skipped.
  The consequence is that real fills arrive slightly sooner than simulated
  ones on venues with meaningful hidden flow — an error in the pessimistic
  direction, which is the only kind worth having.

  Individual orders. LOBSTER gives order IDs, but a queue is FIFO by size and
  tracking identities would buy nothing except the ability to get them wrong.
"""

from __future__ import annotations

from lobster import BUY, SELL, EventType, Message, Snapshot


class BookError(Exception):
    """The message stream and the book disagree about what is resting."""


class Book:
    """The visible book, advanced one message at a time."""

    __slots__ = ("bids", "asks", "_strict")

    def __init__(self, *, strict: bool = True):
        """
        `strict` decides what happens when a message removes more size than
        the book holds at that price. That is impossible in a stream replayed
        from the open, so by default it raises rather than papering over a
        desync that would silently corrupt every fill downstream.

        Pass `strict=False` for files that begin mid-session, where the book
        legitimately starts populated and the opening messages refer to
        orders placed before the first row.
        """
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self._strict = strict

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

    def levels(self, direction: int, depth: int | None = None) -> tuple[tuple[int, int], ...]:
        """Occupied levels on one side, best first."""
        book_side = self.side(direction)
        prices = sorted(book_side, reverse=(direction == BUY))
        if depth is not None:
            prices = prices[:depth]
        return tuple((price, book_side[price]) for price in prices)

    def snapshot(self, levels: int) -> Snapshot:
        """The book in the shape `lobster.load_snapshots` returns.

        Lets reconstruction be compared against LOBSTER's own answer key by
        plain equality, which is what `validate.py` does.
        """
        return Snapshot(
            asks=self.levels(SELL, levels),
            bids=self.levels(BUY, levels),
        )

    def apply(self, message: Message) -> None:
        """Advance the book by one message."""
        if message.type == EventType.SUBMIT:
            self._add(message.direction, message.price, message.size)
        elif message.type in (
            EventType.CANCEL_PARTIAL,
            EventType.CANCEL_TOTAL,
            EventType.EXECUTE_VISIBLE,
        ):
            # All three remove resting size, and `direction` names the side
            # the resting order was on. An execution against a bid is a sell
            # aggressing; the size leaves the bid side either way.
            self._remove(message.direction, message.price, message.size)
        elif message.type in (
            EventType.EXECUTE_HIDDEN,
            EventType.CROSS,
            EventType.HALT,
        ):
            # Tape events. Nothing visible was resting, so nothing leaves.
            pass
        else:
            raise BookError(f"unhandled message type {message.type!r}")

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
            book_side.pop(price, None)
            return
        if size == resting:
            del book_side[price]
        else:
            book_side[price] = resting - size


def replay(messages: list[Message], *, strict: bool = True):
    """Yield `(message, book)` after each message is applied.

    The same `Book` instance is yielded every time — it is mutable state being
    advanced, not a sequence of independent values. Callers that need to keep
    a state must copy it via `snapshot`.
    """
    book = Book(strict=strict)
    for message in messages:
        book.apply(message)
        yield message, book
