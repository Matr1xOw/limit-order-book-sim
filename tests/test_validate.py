import unittest

from book import Book
from messages import BUY, SELL, Action, Message, Snapshot
from validate import Divergence, Report, validate


def message(action, side, price, size, *, sequence, last=True, ts=0):
    return Message(
        ts=ts,
        action=action,
        side=side,
        price=price,
        size=size,
        sequence=sequence,
        last_in_event=last,
    )


class Alignment(unittest.TestCase):
    """mbp-10 has far fewer rows than mbo, so alignment is by sequence."""

    def setUp(self):
        self.seed = Snapshot(asks=((101, 50),), bids=((99, 40),), sequence=1)

    def test_compares_only_where_a_snapshot_shares_the_sequence(self):
        messages = [
            message(Action.ADD, BUY, 99, 40, sequence=1),
            message(Action.ADD, BUY, 98, 10, sequence=2),
            message(Action.ADD, BUY, 97, 10, sequence=3),
        ]
        snapshots = [
            self.seed,
            Snapshot(asks=((101, 50),), bids=((99, 40), (97, 10)), sequence=3),
        ]
        report = validate(messages, snapshots, depth=10)
        # Sequence 2 has no published state, so it is not a comparison.
        self.assertEqual(report.compared, 1)

    def test_skips_states_mid_event(self):
        # Three messages at one sequence; only the last leaves a state the
        # exchange ever published.
        messages = [
            message(Action.ADD, BUY, 99, 40, sequence=1),
            message(Action.TRADE, BUY, 98, 5, sequence=2, last=False),
            message(Action.TRADE, SELL, 98, 5, sequence=2, last=False),
            message(Action.CANCEL, BUY, 99, 40, sequence=2, last=True),
        ]
        snapshots = [self.seed, Snapshot(asks=((101, 50),), bids=(), sequence=2)]
        report = validate(messages, snapshots, depth=10)
        self.assertEqual(report.compared, 1)
        self.assertEqual(report.conflicts, 0)

    def test_rejects_exports_from_different_windows(self):
        messages = [message(Action.ADD, BUY, 99, 40, sequence=500)]
        snapshots = [Snapshot(asks=(), bids=(), sequence=1)]
        with self.assertRaises(ValueError) as caught:
            validate(messages, snapshots)
        self.assertIn("different windows", str(caught.exception))


class Seeding(unittest.TestCase):
    def test_does_not_replay_the_event_the_seed_already_includes(self):
        # A snapshot is the state *after* its event. Seeding from sequence 1
        # and then replaying sequence 1 applies that cancel twice, which
        # would take the bid to nothing.
        seed = Snapshot(asks=(), bids=((99, 60),), sequence=1)
        messages = [
            message(Action.CANCEL, BUY, 99, 40, sequence=1),
            message(Action.ADD, BUY, 99, 10, sequence=2),
        ]
        snapshots = [seed, Snapshot(asks=(), bids=((99, 70),), sequence=2)]
        report = validate(messages, snapshots, depth=10)
        self.assertEqual(report.conflicts, 0)
        self.assertEqual(report.agreed, 1)


class TrustAttribution(unittest.TestCase):
    """Separating "the seed could not see that" from "the code is wrong"."""

    def test_a_level_built_within_the_window_is_trusted(self):
        book = Book.seeded(Snapshot(asks=((101, 50),), bids=((99, 40),)))
        book.apply(message(Action.ADD, BUY, 100, 25, sequence=2))
        self.assertTrue(book.trusted(BUY, 100))

    def test_a_seeded_level_is_never_trusted_again(self):
        book = Book.seeded(Snapshot(asks=(), bids=((99, 40),)))
        book.apply(message(Action.CANCEL, BUY, 99, 40, sequence=2))
        book.apply(message(Action.ADD, BUY, 99, 10, sequence=3))
        # Our count reached zero, but the real level may have held size below
        # the seed's horizon all along, so the rebuild inherits the error.
        self.assertFalse(book.trusted(BUY, 99))

    def test_prices_beyond_the_seed_horizon_are_not_trusted(self):
        book = Book.seeded(Snapshot(asks=((101, 50),), bids=((99, 40),)))
        # Deeper than anything the ten-level seed carried, so the seed said
        # nothing about it — even though we never saw it seeded.
        self.assertFalse(book.trusted(BUY, 98))
        self.assertFalse(book.trusted(SELL, 102))
        self.assertTrue(book.trusted(BUY, 100))

    def test_a_book_built_from_empty_has_no_blind_spot(self):
        book = Book()
        book.apply(message(Action.ADD, BUY, 99, 40, sequence=1))
        self.assertTrue(book.trusted(BUY, 99))
        self.assertTrue(book.trusted(BUY, 1))

    def test_an_orphaned_removal_taints_its_level(self):
        book = Book(strict=False)
        book.apply(message(Action.ADD, BUY, 99, 40, sequence=1))
        book.apply(message(Action.CANCEL, BUY, 99, 90, sequence=2))
        self.assertEqual(book.orphans, 1)
        self.assertFalse(book.trusted(BUY, 99))


class CatchesRealFaults(unittest.TestCase):
    """The validator has to be able to fail, or its 100% means nothing."""

    def _messages_and_truth(self):
        messages = [
            message(Action.ADD, BUY, 99, 40, sequence=1),
            message(Action.ADD, BUY, 100, 30, sequence=2),
            message(Action.CANCEL, BUY, 100, 10, sequence=3),
        ]
        snapshots = [
            Snapshot(asks=(), bids=((99, 40),), sequence=1),
            Snapshot(asks=(), bids=((100, 30), (99, 40)), sequence=2),
            Snapshot(asks=(), bids=((100, 20), (99, 40)), sequence=3),
        ]
        return messages, snapshots

    def test_a_correct_replay_reports_no_conflicts(self):
        report = validate(*self._messages_and_truth(), depth=10)
        self.assertEqual(report.conflicts, 0)
        self.assertTrue(report.exact)

    def test_a_wrong_size_is_caught_as_a_conflict(self):
        messages, snapshots = self._messages_and_truth()
        # The exchange says 20 rests at 100; claim 25 instead.
        snapshots[-1] = Snapshot(asks=(), bids=((100, 25), (99, 40)), sequence=3)
        report = validate(messages, snapshots, depth=10)
        self.assertEqual(report.conflicts, 1)
        self.assertLess(report.consistency, 1.0)

    def test_double_counting_a_removal_is_caught(self):
        # The bug this repo actually had: applying both the fill and the
        # cancel of one execution.
        messages, snapshots = self._messages_and_truth()
        messages.append(message(Action.CANCEL, BUY, 100, 10, sequence=4))
        snapshots.append(
            Snapshot(asks=(), bids=((100, 20), (99, 40)), sequence=4)
        )
        report = validate(messages, snapshots, depth=10)
        self.assertEqual(report.conflicts, 1)

    def test_divergences_are_retained_for_reading(self):
        messages, snapshots = self._messages_and_truth()
        snapshots[-1] = Snapshot(asks=(), bids=((100, 25), (99, 40)), sequence=3)
        report = validate(messages, snapshots, depth=10, keep=5)
        self.assertEqual(len(report.divergences), 1)
        text = report.divergences[0].describe(10)
        self.assertIn("ours", text)
        self.assertIn("theirs", text)

    def test_kept_divergences_are_bounded(self):
        # Diverge on a level built inside the window, so the disagreements
        # count as real conflicts rather than being charged to the seed.
        messages = [message(Action.ADD, BUY, 99, 1, sequence=1)]
        snapshots = [Snapshot(asks=(), bids=((99, 1),), sequence=1)]
        for sequence in range(2, 40):
            messages.append(message(Action.ADD, BUY, 100, 1, sequence=sequence))
            # Always wrong, so every comparison diverges.
            snapshots.append(
                Snapshot(asks=(), bids=((100, 9999), (99, 1)), sequence=sequence)
            )
        report = validate(messages, snapshots, depth=10, keep=3)
        self.assertGreater(report.conflicts, 3)
        self.assertEqual(len(report.divergences), 3)


class Coverage(unittest.TestCase):
    def test_reports_how_much_of_the_book_it_speaks_for(self):
        # 100% trusted over 0% of levels would be worthless, so the report
        # has to carry both numbers.
        messages = [
            message(Action.ADD, BUY, 99, 40, sequence=1),
            message(Action.ADD, BUY, 100, 30, sequence=2),
        ]
        snapshots = [
            Snapshot(asks=(), bids=((99, 40),), sequence=1),
            Snapshot(asks=(), bids=((100, 30), (99, 40)), sequence=2),
        ]
        report = validate(messages, snapshots, depth=10)
        self.assertGreater(report.levels_seen, 0)
        self.assertGreater(report.coverage, 0.0)

    def test_empty_report_does_not_divide_by_zero(self):
        report = Report(depth=1)
        self.assertEqual(report.rate, 0.0)
        self.assertEqual(report.consistency, 0.0)
        self.assertEqual(report.coverage, 0.0)
        self.assertFalse(report.exact)


class DivergenceRendering(unittest.TestCase):
    def test_describes_only_the_side_that_differs(self):
        divergence = Divergence(
            index=7,
            sequence=42,
            ours=Snapshot(asks=((101, 5),), bids=((99, 1),)),
            theirs=Snapshot(asks=((101, 5),), bids=((99, 2),)),
        )
        text = divergence.describe(10)
        self.assertIn("bids", text)
        self.assertNotIn("asks", text)


if __name__ == "__main__":
    unittest.main()
