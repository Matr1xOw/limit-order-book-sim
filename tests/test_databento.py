import csv
import unittest

from book import Book
from databento import _timestamp, find_pair, load_messages, load_snapshots
from messages import BUY, NO_SIDE, SELL, Action

from .fixtures import DATABENTO_MBO, DATABENTO_MBP10, FIXTURE_DIR


class LoadMessages(unittest.TestCase):
    def setUp(self):
        self.messages = load_messages(DATABENTO_MBO)

    def test_reads_every_row(self):
        self.assertEqual(len(self.messages), 21)

    def test_prices_parse_to_exact_nanodollars(self):
        # 324.58 through a float is 324579999999.99994. Level identity is
        # dictionary lookup, so the last digit is not cosmetic.
        prices = {m.price for m in self.messages}
        self.assertIn(324_580_000_000, prices)
        for message in self.messages:
            self.assertIsInstance(message.price, int)

    def test_timestamps_keep_nanosecond_resolution(self):
        # datetime truncates at microseconds, which would silently reorder
        # messages. The last three digits have to survive.
        apart = _timestamp("2026-09-02T14:30:00.123456789Z") - _timestamp(
            "2026-09-02T14:30:00.123456788Z"
        )
        self.assertEqual(apart, 1)

    def test_parsing_loses_no_distinct_instants(self):
        # Messages within one event share a timestamp — the fixture's six
        # execution rows all print at once — so the parsed set is smaller than
        # the message count. What matters is that it is no smaller than the
        # set of distinct strings in the file.
        with open(DATABENTO_MBO, newline="") as handle:
            raw = {row["ts_event"] for row in csv.DictReader(handle)}
        self.assertEqual(len({m.ts for m in self.messages}), len(raw))

    def test_timestamps_are_epoch_based(self):
        for message in self.messages:
            self.assertGreater(message.ts, 1_700_000_000_000_000_000)

    def test_timestamps_are_non_decreasing(self):
        stamps = [m.ts for m in self.messages]
        self.assertEqual(stamps, sorted(stamps))

    def test_side_codes_map_to_the_resting_side(self):
        sides = {m.side for m in self.messages}
        self.assertLessEqual(sides, {BUY, SELL, NO_SIDE})

    def test_action_codes_normalise(self):
        actions = {m.action for m in self.messages}
        self.assertLessEqual(
            actions, {Action.ADD, Action.CANCEL, Action.EXECUTE, Action.TRADE}
        )


class ExecutionTriplets(unittest.TestCase):
    """The fixture spans two executions, each emitted as a T, an F and a C.

    Only the C removes size. Measured against the exchange's own book over the
    reference window, applying F as well drops top-of-book agreement from
    95.4% to 94.6% and nearly quadruples the unaccountable removals.
    """

    def setUp(self):
        self.messages = load_messages(DATABENTO_MBO)
        self.triplets = {}
        for message in self.messages:
            self.triplets.setdefault(message.sequence, []).append(message)

    def _executions(self):
        return [
            group
            for group in self.triplets.values()
            if any(m.action is Action.CANCEL for m in group)
            and any(m.action is Action.TRADE for m in group)
        ]

    def test_an_execution_arrives_as_three_rows_at_one_sequence(self):
        executions = self._executions()
        self.assertEqual(len(executions), 2)
        for group in executions:
            self.assertEqual(len(group), 3)
            # One instant, too: the three rows share a timestamp.
            self.assertEqual(len({m.ts for m in group}), 1)

    def test_only_the_cancel_moves_the_book(self):
        for group in self._executions():
            movers = [m for m in group if m.moves_book]
            self.assertEqual(len(movers), 1)
            self.assertIs(movers[0].action, Action.CANCEL)
            # It rests on the ask; the aggressor was buying.
            self.assertEqual(movers[0].side, SELL)

    def test_the_prints_carry_aggressor_side_and_are_inert(self):
        for group in self._executions():
            prints = [m for m in group if m.action is Action.TRADE]
            self.assertEqual(len(prints), 2)
            for tape in prints:
                self.assertFalse(tape.moves_book)
            self.assertEqual({t.side for t in prints}, {BUY, SELL})

    def test_the_fill_and_its_cancel_name_the_same_order(self):
        for group in self._executions():
            owned = [m for m in group if m.order_id]
            self.assertEqual(len({m.order_id for m in owned}), 1)

    def test_applying_the_cancel_removes_the_traded_size_once(self):
        # Guards the decision rather than the code. If F became book-moving
        # again, this level would lose its size twice over.
        group = self._executions()[0]
        cancel = next(m for m in group if m.action is Action.CANCEL)
        book = Book(strict=False)
        book.asks[cancel.price] = 1000
        for message in group:
            book.apply(message)
        self.assertEqual(book.size_at(SELL, cancel.price), 1000 - cancel.size)
        self.assertEqual(book.orphans, 0)

    def test_hidden_trade_has_no_side(self):
        hidden = [m for m in self.messages if m.side == NO_SIDE]
        self.assertEqual(len(hidden), 1)
        self.assertIs(hidden[0].action, Action.TRADE)


class Flags(unittest.TestCase):
    def test_last_in_event_is_read_from_the_flag_bit(self):
        messages = load_messages(DATABENTO_MBO)
        self.assertTrue(any(m.last_in_event for m in messages))
        # The trade and fill prints sit mid-event, so the book is not
        # comparable against a published snapshot at those points.
        prints = [m for m in messages if m.action is Action.TRADE and m.side]
        self.assertFalse(any(p.last_in_event for p in prints))


class LoadSnapshots(unittest.TestCase):
    def setUp(self):
        self.snapshots = load_snapshots(DATABENTO_MBP10)

    def test_reads_every_row(self):
        self.assertEqual(len(self.snapshots), 20)

    def test_levels_are_ordered_and_uncrossed(self):
        for snapshot in self.snapshots:
            bids = [price for price, _ in snapshot.bids]
            asks = [price for price, _ in snapshot.asks]
            self.assertEqual(bids, sorted(bids, reverse=True))
            self.assertEqual(asks, sorted(asks))
            if bids and asks:
                self.assertLess(bids[0], asks[0])

    def test_sizes_are_positive(self):
        for snapshot in self.snapshots:
            for _, size in snapshot.bids + snapshot.asks:
                self.assertGreater(size, 0)

    def test_carries_the_sequence_for_alignment(self):
        # mbp-10 is not row-aligned to mbo, so the sequence is the only way
        # to say which message a snapshot follows.
        for snapshot in self.snapshots:
            self.assertGreater(snapshot.sequence, 0)

    def test_seeds_a_book(self):
        book = Book.seeded(self.snapshots[0])
        self.assertEqual(book.best_bid, self.snapshots[0].best_bid)
        self.assertEqual(book.best_ask, self.snapshots[0].best_ask)
        self.assertFalse(book.crossed)


class FindPair(unittest.TestCase):
    def test_locates_both_exports(self):
        mbo, mbp10 = find_pair(FIXTURE_DIR)
        self.assertEqual(mbo, DATABENTO_MBO)
        self.assertEqual(mbp10, DATABENTO_MBP10)

    def test_rejects_a_directory_with_no_exports(self):
        with self.assertRaises(ValueError):
            find_pair(FIXTURE_DIR.parent)

    def test_refuses_a_still_compressed_file(self):
        packed = FIXTURE_DIR / "packed.mbo.csv.zst"
        packed.write_bytes(b"")
        try:
            with self.assertRaises(ValueError) as caught:
                load_messages(packed)
            self.assertIn("zstd", str(caught.exception))
        finally:
            packed.unlink()


if __name__ == "__main__":
    unittest.main()
