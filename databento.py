"""Reader for Databento CSV exports of Nasdaq TotalView-ITCH.

Two schemas over one symbol-window, which is the whole reason this source
replaces LOBSTER:

  `mbo`     one row per order message — the input
  `mbp-10`  the top ten levels after an event — an independent answer key

Written against exports made with `pretty_px` and `pretty_ts` on, so prices
arrive as decimal strings and timestamps as RFC 3339. Both are converted to
integers here and stay that way.

Three things about this feed that a reader has to get right, all of them
learned from the data rather than from the documentation:

  An execution emits *three* rows, and only one of them moves the book. A `T`
  on the aggressor's side, an `F` on the resting order, and a `C` for that
  same order id — all at one instant and one sequence number. The `C` is the
  removal; `T` and `F` are prints. Every one of the 5680 fills in the
  reference window is followed by its `C`, and applying `F` as well removes
  the size twice. Measured against the exchange's own book over 23150 events,
  double-counting agrees 94.6% of the time against 95.4% for `C` alone, and
  nearly quadruples the unaccountable removals. A `T` with side `N` is a
  trade that consumed no visible size at all — hidden liquidity.

  `sequence` is not unique. Those three rows share one, so a sequence number
  identifies an event rather than a message. Anything aligning two schemas has
  to key on it together with the last-in-event flag.

  `mbp-10` is not row-aligned to `mbo`. It publishes only when the top ten
  changes, so the example window holds 455k messages against 176k book
  updates. LOBSTER's one-row-per-message pairing does not carry over.
"""

from __future__ import annotations

import csv
import lzma
from datetime import datetime
from pathlib import Path

from messages import Action, Message, Snapshot, parse_price

BUY = 1
SELL = -1
NO_SIDE = 0

#: Databento's side codes. `N` appears on trades that touched no visible side.
_SIDES = {"B": BUY, "A": SELL, "N": NO_SIDE}

#: Databento's action codes, normalised.
_ACTIONS = {
    "A": Action.ADD,
    "C": Action.CANCEL,
    "M": Action.MODIFY,
    "R": Action.CLEAR,
    # Neither T nor F removes anything. See the module docstring: the paired
    # C is what takes the size out, and applying F as well double-counts it.
    "F": Action.TRADE,
    "T": Action.TRADE,
}

#: Bit 7 of `flags`, set on the last message of an event. The book is only
#: guaranteed to match a published snapshot at these points.
_F_LAST = 1 << 7


def _open(path: str | Path):
    """Open a Databento CSV, decompressing if it is still packed.

    `.zst` is not handled — the stdlib has no zstd — so decompress those with
    the `zstd` tool first. `.xz` works because `lzma` ships with Python.
    """
    path = Path(path)
    if path.suffix == ".xz":
        return lzma.open(path, "rt", newline="")
    if path.suffix == ".zst":
        raise ValueError(
            f"{path}: zstd is not in the standard library — run "
            f"`zstd -d {path}` and pass the .csv"
        )
    return open(path, newline="")


def _timestamp(text: str) -> int:
    """RFC 3339 with nanoseconds to integer nanoseconds since the epoch.

    `datetime` truncates at microseconds, so the last three digits are parsed
    off the string by hand rather than lost. At these timescales a nanosecond
    is not a rounding detail — it is the ordering of two messages.
    """
    text = text.strip().replace("Z", "+00:00")
    head, _, rest = text.partition(".")
    fraction, sign, offset = rest.partition("+")
    if not sign:
        fraction, sign, offset = rest.partition("-")
    fraction = (fraction + "0" * 9)[:9]
    moment = datetime.fromisoformat(head + (sign + offset if sign else ""))
    return int(moment.timestamp()) * 1_000_000_000 + int(fraction)


def load_messages(path: str | Path) -> list[Message]:
    """Read an `mbo` export in full.

    Every row is returned, including the `T` prints that do not move the book.
    Dropping them here would be convenient and wrong: they are the only record
    of aggressor side and of hidden liquidity, and a strategy may well want
    both. `book.py` is what decides they change nothing.
    """
    messages: list[Message] = []
    with _open(path) as handle:
        for lineno, row in enumerate(csv.DictReader(handle), start=2):
            action = row["action"]
            if action not in _ACTIONS:
                raise ValueError(f"{path}:{lineno}: unknown action {action!r}")
            side = _SIDES.get(row["side"])
            if side is None:
                raise ValueError(f"{path}:{lineno}: unknown side {row['side']!r}")
            flags = int(row["flags"] or 0)
            messages.append(
                Message(
                    ts=_timestamp(row["ts_event"]),
                    action=_ACTIONS[action],
                    side=side,
                    price=parse_price(row["price"]),
                    size=int(row["size"]),
                    order_id=int(row["order_id"] or 0),
                    sequence=int(row["sequence"] or 0),
                    last_in_event=bool(flags & _F_LAST),
                )
            )
    return messages


def load_snapshots(path: str | Path, levels: int = 10) -> list[Snapshot]:
    """Read an `mbp-10` export.

    Empty levels carry a price of 0 and a size of 0 rather than a sentinel,
    and are stripped, so a thin book yields a short snapshot.
    """
    snapshots: list[Snapshot] = []
    with _open(path) as handle:
        for row in csv.DictReader(handle):
            bids: list[tuple[int, int]] = []
            asks: list[tuple[int, int]] = []
            for level in range(levels):
                bid_size = int(row.get(f"bid_sz_{level:02d}") or 0)
                ask_size = int(row.get(f"ask_sz_{level:02d}") or 0)
                if bid_size > 0:
                    price = parse_price(row[f"bid_px_{level:02d}"])
                    if price > 0:
                        bids.append((price, bid_size))
                if ask_size > 0:
                    price = parse_price(row[f"ask_px_{level:02d}"])
                    if price > 0:
                        asks.append((price, ask_size))
            snapshots.append(
                Snapshot(
                    asks=tuple(asks),
                    bids=tuple(bids),
                    sequence=int(row["sequence"] or 0),
                )
            )
    return snapshots


def find_pair(directory: str | Path) -> tuple[Path, Path]:
    """Locate the `mbo` and `mbp-10` exports in a directory.

    Returns `(mbo_path, mbp10_path)`. Raises unless there is exactly one of
    each, because guessing which window the caller meant is how you end up
    validating one slice of the day against another.
    """
    directory = Path(directory)

    def one(pattern: str, label: str) -> Path:
        found = sorted(
            path
            for path in directory.glob(pattern)
            if path.suffix in (".csv", ".xz")
        )
        if len(found) != 1:
            raise ValueError(
                f"{directory}: need exactly one {label} export, found {len(found)}"
            )
        return found[0]

    return one("*.mbo.csv*", "mbo"), one("*.mbp-10.csv*", "mbp-10")
