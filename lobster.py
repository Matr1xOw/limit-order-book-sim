"""Readers for LOBSTER's two CSV exports.

Kept working, but no longer the project's data source: the free samples now
require proof of purchase of a particular book and an institutional academic
e-mail address. See `data/README.md`. `databento.py` is what you want.


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
from enum import IntEnum
from pathlib import Path

from messages import BUY, SELL, Action, Message, Snapshot

#: LOBSTER quotes prices in ten-thousandths of a dollar; the shared vocabulary
#: uses nanodollars. 1234500 (i.e. $123.45) becomes 123_450_000_000.
_LOBSTER_TO_NANO = 100_000

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


#: LOBSTER's event codes in the shared vocabulary. The two cancel codes differ
#: only in whether the order had size left over, which the book does not care
#: about — both carry the shares removed.
_ACTIONS = {
    EventType.SUBMIT: Action.ADD,
    EventType.CANCEL_PARTIAL: Action.CANCEL,
    EventType.CANCEL_TOTAL: Action.CANCEL,
    EventType.EXECUTE_VISIBLE: Action.EXECUTE,
    EventType.EXECUTE_HIDDEN: Action.TRADE,
    EventType.CROSS: Action.STATUS,
    EventType.HALT: Action.STATUS,
}


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
            event = EventType(int(row[1]))
            messages.append(
                Message(
                    # Seconds after midnight, to nanoseconds. Not epoch-based —
                    # LOBSTER files carry no date, it lives in the filename.
                    ts=int(round(float(row[0]) * 1_000_000_000)),
                    action=_ACTIONS[event],
                    side=int(row[5]),
                    price=int(row[4]) * _LOBSTER_TO_NANO,
                    size=int(row[3]),
                    order_id=int(row[2]),
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
                    asks.append((ask_price * _LOBSTER_TO_NANO, ask_size))
                if bid_price != _BID_PADDING and bid_size > 0:
                    bids.append((bid_price * _LOBSTER_TO_NANO, bid_size))
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
