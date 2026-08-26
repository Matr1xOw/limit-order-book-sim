import unittest

from lobster import (
    BUY,
    SELL,
    EventType,
    find_pair,
    load_messages,
    load_snapshots,
)

from .fixtures import FIXTURE_DIR, LEVELS, MESSAGE_FILE, ORDERBOOK_FILE


class LoadMessages(unittest.TestCase):
    def setUp(self):
        self.messages = load_messages(MESSAGE_FILE)

    def test_reads_every_row(self):
        self.assertEqual(len(self.messages), 5)

    def test_parses_fields_in_lobster_column_order(self):
        first = self.messages[0]
        self.assertAlmostEqual(first.time, 34200.0)
        self.assertEqual(first.type, EventType.SUBMIT)
        self.assertEqual(first.order_id, 1)
        self.assertEqual(first.size, 100)
        self.assertEqual(first.price, 1000000)
        self.assertEqual(first.direction, SELL)

    def test_prices_stay_integers(self):
        # Not 100.0 dollars. Queue lookup is by exact price equality, so the
        # moment a price becomes a float the whole model is unsound.
        for message in self.messages:
            self.assertIsInstance(message.price, int)

    def test_direction_names_the_resting_side_on_an_execution(self):
        execution = self.messages[3]
        self.assertEqual(execution.type, EventType.EXECUTE_VISIBLE)
        # A resting *sell* was executed, so the aggressor was a buyer.
        self.assertEqual(execution.direction, SELL)

    def test_classifies_event_families(self):
        self.assertTrue(self.messages[3].is_execution)
        self.assertFalse(self.messages[3].is_cancel)
        self.assertTrue(self.messages[4].is_cancel)
        self.assertFalse(self.messages[4].is_execution)
        self.assertFalse(self.messages[0].is_execution)

    def test_rejects_short_rows(self):
        broken = FIXTURE_DIR / "broken_message_2.csv"
        broken.write_text("34200.0,1,1,100\n")
        try:
            with self.assertRaises(ValueError):
                load_messages(broken)
        finally:
            broken.unlink()


class LoadSnapshots(unittest.TestCase):
    def setUp(self):
        self.snapshots = load_snapshots(ORDERBOOK_FILE, LEVELS)

    def test_one_snapshot_per_message(self):
        self.assertEqual(len(self.snapshots), 5)

    def test_strips_padding_levels(self):
        # After the first message the book holds a single ask and nothing
        # else; LOBSTER pads the other three slots with sentinels.
        first = self.snapshots[0]
        self.assertEqual(first.asks, ((1000000, 100),))
        self.assertEqual(first.bids, ())

    def test_orders_levels_best_first(self):
        third = self.snapshots[2]
        self.assertEqual(third.asks, ((1000000, 100), (1000100, 50)))
        self.assertEqual(third.best_ask, 1000000)
        self.assertEqual(third.best_bid, 999000)

    def test_best_prices_are_none_on_an_empty_side(self):
        self.assertIsNone(self.snapshots[0].best_bid)

    def test_rejects_wrong_level_count(self):
        with self.assertRaises(ValueError):
            load_snapshots(ORDERBOOK_FILE, levels=10)


class FindPair(unittest.TestCase):
    def test_locates_both_files_and_the_level_count(self):
        message_path, orderbook_path, levels = find_pair(FIXTURE_DIR)
        self.assertEqual(message_path, MESSAGE_FILE)
        self.assertEqual(orderbook_path, ORDERBOOK_FILE)
        self.assertEqual(levels, 2)

    def test_rejects_a_directory_with_no_pair(self):
        with self.assertRaises(ValueError):
            find_pair(FIXTURE_DIR.parent)


if __name__ == "__main__":
    unittest.main()
