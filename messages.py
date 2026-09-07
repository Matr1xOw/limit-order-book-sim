"""The normalised message vocabulary every reader emits.

Feed formats disagree about almost everything — LOBSTER numbers its event
types, Databento letters them; one signs the side, the other letters that too;
prices are scaled differently. None of that is interesting to a book, which
only ever wants to know *what happened to how much size at which price*.

So readers translate into the types here and `book.py` speaks only this. When
the next data source appears — and it will, LOBSTER already went away once —
the cost is a reader rather than a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Side of the *resting* order. Ints rather than an enum because they index
#: naturally and compare cheaply in the replay loop.
BUY = 1
SELL = -1
NO_SIDE = 0


class Action(Enum):
    """What a message does to the book."""

    ADD = "add"
    """New resting order. Adds `size` at `price`."""

    CANCEL = "cancel"
    """Resting size withdrawn. `size` is the amount removed, not the order's total."""

    EXECUTE = "execute"
    """Visible resting order traded against. Removes `size` at `price`."""

    TRADE = "trade"
    """
    A trade print that does not itself move the book.

    Two quite different things land here, and it is worth knowing which:

      Hidden executions, where no visible size was ever queued.

      Prints that accompany a removal the feed reports separately. Databento
      emits three rows per execution — a `T` on the aggressor's side, an `F`
      on the resting order, and a `C` that actually takes the size out. Only
      the `C` may be applied; treating `F` as a removal double-counts it,
      which measurably degrades agreement with the exchange's own book.

    Either way nothing is removed here. The events are kept rather than
    dropped at the reader, because they are real tape carrying aggressor side
    and hidden volume, and a strategy may want both.
    """

    MODIFY = "modify"
    """
    Resting order repriced or resized.

    Carried for completeness. Applying one needs the order's previous state,
    which an aggregate price→size book does not keep, so `Book` rejects it
    rather than guessing. No feed read so far emits these for the windows
    used here; if one does, that is the point to add order tracking.
    """

    CLEAR = "clear"
    """Wipe the book. Feed restarts and session boundaries."""

    STATUS = "status"
    """Halts, auctions, and other markers that leave the book alone."""


@dataclass(frozen=True, slots=True)
class Message:
    """One normalised book event."""

    ts: int
    """Event time in nanoseconds since the Unix epoch."""

    action: Action

    side: int
    """`BUY`, `SELL`, or `NO_SIDE`, naming the side of the *resting* order."""

    price: int
    """
    Price in nanodollars — $324.55 is 324_550_000_000.

    Integer, always. Orders queue at a price level found by exact equality,
    and a float price would make level identity depend on rounding.
    """

    size: int
    """Shares this message adds or removes. Not the resting order's total."""

    order_id: int = 0
    """Feed order reference, or 0 where the feed gives none."""

    sequence: int = 0
    """
    Feed sequence number. Not unique — a trade and the fill it caused share
    one — so it identifies an *event*, not a message.
    """

    last_in_event: bool = True
    """
    True on the final message of a multi-message event.

    The book is only guaranteed consistent with a published snapshot at these
    points, which is what makes validation possible; comparing mid-event is
    comparing against a book that is halfway through changing.
    """

    @property
    def moves_book(self) -> bool:
        """Whether applying this message can change visible resting size."""
        return self.action in (
            Action.ADD,
            Action.CANCEL,
            Action.EXECUTE,
            Action.MODIFY,
            Action.CLEAR,
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A book state published by the feed, for checking a reconstruction against.

    Levels are best-first with empty ones stripped, so a snapshot may hold
    fewer than its nominal depth. Absence means "the feed reported nothing
    there", never "there was nothing" — a ten-level feed cannot speak to
    level eleven.
    """

    asks: tuple[tuple[int, int], ...]
    """(price, size), ascending."""

    bids: tuple[tuple[int, int], ...]
    """(price, size), descending."""

    sequence: int = 0

    @property
    def best_ask(self) -> int | None:
        return self.asks[0][0] if self.asks else None

    @property
    def best_bid(self) -> int | None:
        return self.bids[0][0] if self.bids else None


#: Nanodollars per dollar. Prices are stored scaled by this.
PRICE_SCALE = 1_000_000_000


def parse_price(text: str) -> int:
    """Convert a decimal price string to nanodollars without touching a float.

    `float("324.55") * 1e9` is 324549999999.99994. Splitting on the point and
    padding the fraction keeps it exact, which matters because these values
    are dictionary keys.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty price")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    whole, _, fraction = text.partition(".")
    fraction = (fraction + "0" * 9)[:9]
    value = int(whole or "0") * PRICE_SCALE + int(fraction or "0")
    return -value if negative else value


def format_price(price: int) -> str:
    """Render nanodollars as dollars, for error messages and reports."""
    sign = "-" if price < 0 else ""
    price = abs(price)
    return f"{sign}{price // PRICE_SCALE}.{price % PRICE_SCALE:09d}".rstrip("0").rstrip(".")
