"""Check a reconstruction against the exchange's own published book.

The claim this repo rests on is that its book is *correct*. That is not
something to assert in a README. Databento serves the same symbol-window as
both a message stream and a top-of-book product, so the messages can be
replayed and the result diffed against what the exchange itself published.

Three things make the comparison less obvious than it sounds.

  Alignment is by sequence, not by row. `mbp-10` publishes only when the top
  ten changes, so it has far fewer rows than `mbo` — 176k against 455k in the
  reference window. Row *i* of one is unrelated to row *i* of the other.

  Only end-of-event states are comparable. An execution spans three messages
  at one sequence; between the first and the last the book is halfway through
  changing and will not match anything. Comparisons happen on `last_in_event`.

  Depth is bounded by the seed. A window that opens mid-session starts from a
  ten-level snapshot, so levels below that are unknown until they trade or are
  cancelled into view. Comparing at depth 10 therefore measures reconstruction
  *and* seeding; comparing at depth 1 measures mostly reconstruction. Running
  both is how you tell those apart, which is what `--depth` is for.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from book import Book, replay
from databento import find_pair, load_messages, load_snapshots
from messages import BUY, SELL, Message, Snapshot, format_price


@dataclass
class Divergence:
    """One point where the reconstruction and the feed disagreed."""

    index: int
    """Position in the message stream."""

    sequence: int
    ours: Snapshot
    theirs: Snapshot
    trusted: bool = True
    """False when the disagreement involves a level the seed could not see."""

    def describe(self, depth: int) -> str:
        lines = [f"message {self.index}, sequence {self.sequence}:"]
        for label, side in (("bids", "bids"), ("asks", "asks")):
            ours = getattr(self.ours, side)[:depth]
            theirs = getattr(self.theirs, side)[:depth]
            if ours == theirs:
                continue
            lines.append(f"  {label}")
            lines.append(f"    ours   {_render(ours)}")
            lines.append(f"    theirs {_render(theirs)}")
        return "\n".join(lines)


def _trusted_disagreement(
    book: Book, ours: Snapshot, theirs: Snapshot, depth: int
) -> bool:
    """Whether a disagreement touches a level we claim to fully account for.

    A single untrusted level explains the whole comparison, so this is only
    a real fault when *every* differing price is one we fully account for.
    Charging it per-price instead would blame the wrong level: when a
    seed-blind best bid is missing, the next level down surfaces in its
    place and gets recorded as a conflict despite being perfectly correct.
    """
    saw_difference = False
    for side, ours_levels, theirs_levels in (
        (BUY, ours.bids[:depth], theirs.bids[:depth]),
        (SELL, ours.asks[:depth], theirs.asks[:depth]),
    ):
        mine = dict(ours_levels)
        yours = dict(theirs_levels)
        for price in set(mine) | set(yours):
            if mine.get(price) == yours.get(price):
                continue
            if not book.trusted(side, price):
                return False
            saw_difference = True
    return saw_difference


def _render(levels: tuple[tuple[int, int], ...]) -> str:
    return " ".join(f"{format_price(p)}x{s}" for p, s in levels) or "(empty)"


@dataclass
class Report:
    """What a validation run found."""

    depth: int
    compared: int = 0
    agreed: int = 0
    messages: int = 0
    orphans: int = 0
    untrusted: int = 0
    """Disagreements confined to levels inherited from the seed."""
    conflicts: int = 0
    """Disagreements on levels whose whole history we watched. Should be 0."""
    levels_seen: int = 0
    levels_trusted: int = 0
    """
    How much of the book the trusted figure actually speaks for.

    Without this, `trusted-levels 100%` is unreadable: a `trusted` predicate
    strict enough to exclude everything would report the same thing. Coverage
    is what makes the claim falsifiable.
    """
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def rate(self) -> float:
        """Share of compared states that matched exactly, 0.0 to 1.0."""
        return self.agreed / self.compared if self.compared else 0.0

    @property
    def exact(self) -> bool:
        return self.compared > 0 and self.agreed == self.compared

    @property
    def consistency(self) -> float:
        """Share of comparisons with no disagreement on a *trusted* level.

        Exact agreement counts the seed's blind spot as a failure, which is
        honest but conflates a known limit of the input with a bug in the
        code. This counts only levels whose entire history the replay
        watched, and is the number that should be 1.0 if the reconstruction
        is right. Anything less is a real defect.
        """
        if not self.compared:
            return 0.0
        return (self.compared - self.conflicts) / self.compared

    @property
    def coverage(self) -> float:
        """Share of level-observations the trusted figure speaks for."""
        return self.levels_trusted / self.levels_seen if self.levels_seen else 0.0

    def summary(self) -> str:
        return (
            f"depth {self.depth:<2} "
            f"exact {100 * self.rate:6.2f}%  "
            f"trusted {100 * self.consistency:7.3f}% "
            f"over {100 * self.coverage:.1f}% of levels  "
            f"(seed-blind {self.untrusted}, conflicts {self.conflicts})  "
            f"orphans {self.orphans}"
        )


def validate(
    messages: list[Message],
    snapshots: list[Snapshot],
    *,
    depth: int = 10,
    keep: int = 5,
) -> Report:
    """Replay `messages` and compare against `snapshots` at each event end.

    `keep` bounds how many divergences are retained; a broken reconstruction
    diverges hundreds of thousands of times and holding all of them is how a
    validator runs the machine out of memory instead of reporting a fault.
    """
    by_sequence = {snapshot.sequence: snapshot for snapshot in snapshots}
    report = Report(depth=depth, messages=len(messages))

    # A published snapshot is the state *after* its event, so seeding from the
    # one at sequence S means the messages at S have already been applied.
    # Replaying them again double-counts the first event — which shows up as a
    # whole price level missing from the top of the book, every level below it
    # apparently shifted by one, and nothing else wrong.
    seed_index = next(
        (i for i, m in enumerate(messages) if m.sequence in by_sequence), None
    )
    if seed_index is None:
        raise ValueError(
            "no snapshot shares a sequence with any message — the two exports "
            "almost certainly cover different windows"
        )
    seed_sequence = messages[seed_index].sequence
    seed = by_sequence[seed_sequence]
    while (
        seed_index < len(messages) and messages[seed_index].sequence == seed_sequence
    ):
        seed_index += 1

    book = Book.seeded(seed)
    for index, (message, book) in enumerate(
        replay(messages[seed_index:], book=book), start=seed_index
    ):
        # Mid-event the book is halfway through changing; only the last
        # message of an event leaves it in a state anyone published.
        if not message.last_in_event:
            continue
        theirs = by_sequence.get(message.sequence)
        if theirs is None:
            continue
        ours = book.snapshot(depth)
        report.compared += 1
        for side, levels in ((BUY, ours.bids), (SELL, ours.asks)):
            for price, _ in levels:
                report.levels_seen += 1
                report.levels_trusted += book.trusted(side, price)
        if (ours.bids[:depth], ours.asks[:depth]) == (
            theirs.bids[:depth],
            theirs.asks[:depth],
        ):
            report.agreed += 1
        else:
            trusted = _trusted_disagreement(book, ours, theirs, depth)
            divergence = Divergence(
                index=index,
                sequence=message.sequence,
                ours=ours,
                theirs=theirs,
                trusted=trusted,
            )
            if trusted:
                report.conflicts += 1
                # Keep these by preference: a seed-blind difference is
                # expected and a trusted one is the thing worth reading.
                if len(report.divergences) < keep:
                    report.divergences.append(divergence)
            else:
                report.untrusted += 1

    report.orphans = book.orphans
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "directory",
        nargs="?",
        default="data",
        type=Path,
        help="directory holding one mbo and one mbp-10 export (default: data)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        action="append",
        help="book depth to compare at; repeatable (default: 1, 5 and 10)",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=3,
        help="how many divergences to print per depth (default: 3)",
    )
    args = parser.parse_args(argv)

    mbo_path, mbp10_path = find_pair(args.directory)
    print(f"messages  {mbo_path}")
    print(f"answer key {mbp10_path}\n")

    messages = load_messages(mbo_path)
    snapshots = load_snapshots(mbp10_path)
    print(f"{len(messages)} messages, {len(snapshots)} published book states\n")

    worst: Report | None = None
    for depth in args.depth or (1, 5, 10):
        report = validate(messages, snapshots, depth=depth, keep=args.show)
        print(report.summary())
        for divergence in report.divergences:
            print(divergence.describe(depth))
        if worst is None or report.consistency < worst.consistency:
            worst = report

    # Exit non-zero only on a reconstruction that is obviously broken, not on
    # the known depth shortfall of a mid-session seed. The number is the
    # output; the status code is for CI.
    # The exit status tracks the number that is supposed to be perfect —
    # agreement on levels we fully observed. Exact agreement is reported but
    # not gated on, because the seed's blind spot is a property of the data.
    return 0 if worst is not None and worst.consistency == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
