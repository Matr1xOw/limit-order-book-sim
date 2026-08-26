"""Readers for LOBSTER's two CSV exports.

LOBSTER ships each ticker-day as a pair of headerless CSV files:

  TICKER_DATE_START_END_message_N.csv    one row per exchange event
  TICKER_DATE_START_END_orderbook_N.csv  the book state *after* that event

Row i of the second file is the state produced by row i of the first, which is
what makes the pair useful: it is a message stream and an independent answer
key for replaying it. `validate.py` leans on that.

Prices are integers throughout — LOBSTER quotes them in ten-thousandths of a
dollar, and they stay that way. Floating point has no business anywhere near a
price level that orders are queued at by exact equality.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

#: Side of a limit order. LOBSTER's own encoding, kept rather than renamed.
BUY = 1
SELL = -1

#: LOBSTER pads unoccupied book levels with these sentinels rather than
#: shortening the row, so a 10-level file always has 40 columns.
_ASK_PADDING = 9999999999
_BID_PADDING = -9999999999


class EventType(IntEnum):
    """LOBSTER message type codes."""

    SUBMIT = 1
    """New limit order added to the book."""

    CANCEL_PARTIAL = 2
    """Part of a resting order withdrawn. `size` is the amount removed."""

    CANCEL_TOTAL = 3
    """A resting order withdrawn entirely. `size` is the amount removed."""

    EXECUTE_VISIBLE = 4
    """A visible resting order traded against. Removes size from the book."""

    EXECUTE_HIDDEN = 5
    """A hidden order traded. Prints on the tape, never was in the book."""

    CROSS = 6
    """Auction cross. Does not move the continuous book."""

    HALT = 7
    """Trading halt or resumption. A marker, not a book event."""


@dataclass(frozen=True, slots=True)
class Message:
    """One exchange event."""

    time: float
    """Seconds after midnight, to nanosecond resolution."""

    type: EventType

    order_id: int
    """Exchange order reference. Zero for events with no single owner."""

    size: int
    """Shares. For cancels, the shares removed rather than the order's total."""

    price: int
    """Ten-thousandths of a dollar. $12.34 is 123400."""

    direction: int
    """
    `BUY` or `SELL`, naming the side of the *resting* limit order.

    This is the field people get backwards. On an `EXECUTE_VISIBLE`,
    `direction == BUY` means a resting bid was hit, which means the aggressor
    was a seller. The message describes the order that was sitting there, not
    the one that arrived.
    """

    @property
    def is_execution(self) -> bool:
        return self.type in (EventType.EXECUTE_VISIBLE, EventType.EXECUTE_HIDDEN)

    @property
    def is_cancel(self) -> bool:
        return self.type in (EventType.CANCEL_PARTIAL, EventType.CANCEL_TOTAL)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """LOBSTER's own view of the book after one message.

    Levels are ordered best-first and padding is stripped, so a snapshot from
    a 10-level file may hold fewer than 10 entries per side if the book was
    thin. Absence means "LOBSTER saw nothing there", not "there was nothing" —
    a 10-level file simply cannot speak to level 11.
    """

    asks: tuple[tuple[int, int], ...]
    """(price, size), ascending."""

    bids: tuple[tuple[int, int], ...]
    """(price, size), descending."""

    @property
    def best_ask(self) -> int | None:
        return self.asks[0][0] if self.asks else None

    @property
    def best_bid(self) -> int | None:
        return self.bids[0][0] if self.bids else None


def load_messages(path: str | Path) -> list[Message]:
    """Read a LOBSTER message file in full.

    Returned as a list rather than a generator: every caller here replays the
    stream more than once (once to validate, once per latency setting), and a
    day of one symbol is tens of megabytes, not gigabytes.
    """
    messages: list[Message] = []
    with open(path, newline="") as handle:
        for lineno, row in enumerate(csv.reader(handle), start=1):
            if not row or not row[0].strip():
                continue
            if len(row) < 6:
                raise ValueError(
                    f"{path}:{lineno}: expected 6 columns, found {len(row)}"
                )
            messages.append(
                Message(
                    time=float(row[0]),
                    type=EventType(int(row[1])),
                    order_id=int(row[2]),
                    size=int(row[3]),
                    price=int(row[4]),
                    direction=int(row[5]),
                )
            )
    return messages


def load_snapshots(path: str | Path, levels: int) -> list[Snapshot]:
    """Read a LOBSTER order book file.

    `levels` is the level count the file was exported at — the `_N` in its
    name. It is a required argument rather than something inferred from the
    column count because getting it wrong silently misaligns every level, and
    a caller who cannot name it is not ready to trust the output.
    """
    expected = levels * 4
    snapshots: list[Snapshot] = []
    with open(path, newline="") as handle:
        for lineno, row in enumerate(csv.reader(handle), start=1):
            if not row or not row[0].strip():
                continue
            if len(row) < expected:
                raise ValueError(
                    f"{path}:{lineno}: {levels}-level file needs {expected} "
                    f"columns, found {len(row)}"
                )
            asks: list[tuple[int, int]] = []
            bids: list[tuple[int, int]] = []
            for level in range(levels):
                base = level * 4
                ask_price, ask_size = int(row[base]), int(row[base + 1])
                bid_price, bid_size = int(row[base + 2]), int(row[base + 3])
                if ask_price != _ASK_PADDING and ask_size > 0:
                    asks.append((ask_price, ask_size))
                if bid_price != _BID_PADDING and bid_size > 0:
                    bids.append((bid_price, bid_size))
            snapshots.append(Snapshot(asks=tuple(asks), bids=tuple(bids)))
    return snapshots


def find_pair(directory: str | Path) -> tuple[Path, Path, int]:
    """Locate the message/orderbook pair in a directory of LOBSTER files.

    Returns `(message_path, orderbook_path, levels)`. Raises if there is not
    exactly one pair, because guessing which ticker-day the caller meant is
    how you end up validating one symbol's messages against another's book.
    """
    directory = Path(directory)
    message_files = sorted(directory.glob("*_message_*.csv"))
    if len(message_files) != 1:
        raise ValueError(
            f"{directory}: need exactly one *_message_*.csv, "
            f"found {len(message_files)}"
        )
    message_path = message_files[0]

    stem = message_path.name
    levels_text = stem.rsplit("_", 1)[1].removesuffix(".csv")
    try:
        levels = int(levels_text)
    except ValueError as error:
        raise ValueError(
            f"{message_path}: cannot read level count from filename"
        ) from error

    orderbook_path = message_path.with_name(stem.replace("_message_", "_orderbook_"))
    if not orderbook_path.exists():
        raise ValueError(f"{orderbook_path}: missing, expected alongside messages")

    return message_path, orderbook_path, levels
