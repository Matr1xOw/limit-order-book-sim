import unittest

from book import Book, BookError, replay
from lobster import load_messages
from messages import BUY, SELL, Action, Message, Snapshot

from .fixtures import MESSAGE_FILE


def message(action, side, price, size, *, ts=0, order_id=1):
    return Message(
        ts=ts, action=action, side=side, price=price, size=size, order_id=order_id
    )


SUBMIT = Action.ADD
EXECUTE = Action.EXECUTE
HIDDEN = Action.TRADE
CANCEL = Action.CANCEL
PARTIAL = Action.CANCEL


class Submissions(unittest.TestCase):
    def test_a_submission_creates_a_level(self):
        book = Book()
        book.apply(message(SUBMIT, SELL, 1000000, 100))
        self.assertEqual(book.size_at(SELL, 1000000), 100)
        self.assertEqual(book.best_ask, 1000000)

    def test_submissions_at_one_price_accumulate(self):
        book = Book()
        book.apply(message(SUBMIT, BUY, 999000, 100))
        book.apply(message(SUBMIT, BUY, 999000, 250, order_id=2))
        self.assertEqual(book.size_at(BUY, 999000), 350)

    def test_sides_are_independent(self):
        book = Book()
        book.apply(message(SUBMIT, BUY, 999000, 100))
        book.apply(message(SUBMIT, SELL, 999000, 100))
        self.assertEqual(book.size_at(BUY, 999000), 100)
        self.assertEqual(book.size_at(SELL, 999000), 100)


class Removals(unittest.TestCase):
    def setUp(self):
        self.book = Book()
        self.book.apply(message(SUBMIT, SELL, 1000000, 100))

    def test_execution_removes_resting_size(self):
        self.book.apply(message(EXECUTE, SELL, 1000000, 40))
        self.assertEqual(self.book.size_at(SELL, 1000000), 60)

    def test_partial_cancel_removes_the_stated_amount(self):
        self.book.apply(message(PARTIAL, SELL, 1000000, 30))
        self.assertEqual(self.book.size_at(SELL, 1000000), 70)

    def test_emptying_a_level_deletes_it(self):
        self.book.apply(message(CANCEL, SELL, 1000000, 100))
        self.assertEqual(self.book.size_at(SELL, 1000000), 0)
        self.assertIsNone(self.book.best_ask)
        self.assertNotIn(1000000, self.book.asks)

    def test_execution_direction_names_the_resting_side(self):
        # A resting bid executed means a seller aggressed. The size must come
        # off the bid, not the ask. Getting this backwards is the classic
        # LOBSTER parsing bug.
        book = Book()
        book.apply(message(SUBMIT, BUY, 999000, 500))
        book.apply(message(SUBMIT, SELL, 1000000, 500))
        book.apply(message(EXECUTE, BUY, 999000, 200))
        self.assertEqual(book.size_at(BUY, 999000), 300)
        self.assertEqual(book.size_at(SELL, 1000000), 500)


class TapeEvents(unittest.TestCase):
    def test_hidden_execution_leaves_the_book_alone(self):
        book = Book()
        book.apply(message(SUBMIT, SELL, 1000000, 100))
        book.apply(message(HIDDEN, SELL, 1000000, 50))
        # The hidden order was never visible, so it was never in anyone's
        # queue and its removal takes nothing out of the book.
        self.assertEqual(book.size_at(SELL, 1000000), 100)

    def test_status_leaves_the_book_alone(self):
        book = Book()
        book.apply(message(SUBMIT, SELL, 1000000, 100))
        book.apply(message(Action.STATUS, SELL, -1, 0))
        self.assertEqual(book.size_at(SELL, 1000000), 100)


class Strictness(unittest.TestCase):
    def test_strict_book_raises_on_oversized_removal(self):
        book = Book()
        book.apply(message(SUBMIT, SELL, 1000000, 100))
        with self.assertRaises(BookError):
            book.apply(message(EXECUTE, SELL, 1000000, 150))

    def test_strict_book_raises_on_removal_from_an_empty_level(self):
        with self.assertRaises(BookError):
            Book().apply(message(CANCEL, BUY, 999000, 10))

    def test_lenient_book_clears_the_level_instead(self):
        book = Book(strict=False)
        book.apply(message(SUBMIT, SELL, 1000000, 100))
        book.apply(message(EXECUTE, SELL, 1000000, 150))
        self.assertEqual(book.size_at(SELL, 1000000), 0)


class Levels(unittest.TestCase):
    def setUp(self):
        self.book = Book()
        for price, size in ((1000000, 100), (1000100, 50), (1000200, 25)):
            self.book.apply(message(SUBMIT, SELL, price, size))
        for price, size in ((999000, 200), (998900, 300)):
            self.book.apply(message(SUBMIT, BUY, price, size))

    def test_asks_ascend_and_bids_descend(self):
        self.assertEqual(
            self.book.levels(SELL), ((1000000, 100), (1000100, 50), (1000200, 25))
        )
        self.assertEqual(self.book.levels(BUY), ((999000, 200), (998900, 300)))

    def test_depth_truncates_to_the_best_levels(self):
        self.assertEqual(self.book.levels(SELL, 2), ((1000000, 100), (1000100, 50)))

    def test_snapshot_reports_only_the_requested_depth(self):
        snapshot = self.book.snapshot(levels=2)
        self.assertEqual(len(snapshot.asks), 2)
        self.assertEqual(snapshot.best_bid, 999000)


class Replay(unittest.TestCase):
    def test_replays_the_fixture_to_its_final_state(self):
        messages = load_messages(MESSAGE_FILE)
        book = None
        for _, book in replay(messages):
            pass
        # Last message cancels the remaining 60 at $100.00, promoting $100.01.
        self.assertEqual(book.best_ask, 100_010_000_000)
        self.assertEqual(book.size_at(SELL, 100_010_000_000), 50)
        self.assertEqual(book.best_bid, 99_900_000_000)

    def test_yields_once_per_message(self):
        messages = load_messages(MESSAGE_FILE)
        self.assertEqual(sum(1 for _ in replay(messages)), len(messages))



class Seeding(unittest.TestCase):
    def test_seeded_book_starts_from_a_published_snapshot(self):
        snapshot = Snapshot(
            asks=((1000100, 50), (1000200, 25)), bids=((999000, 200),)
        )
        book = Book.seeded(snapshot)
        self.assertEqual(book.best_ask, 1000100)
        self.assertEqual(book.size_at(BUY, 999000), 200)

    def test_seeded_book_is_lenient_by_default(self):
        # A mid-session window is exactly where orphaned removals happen, so
        # seeding and strictness would be a contradiction.
        book = Book.seeded(Snapshot(asks=(), bids=()))
        book.apply(message(CANCEL, BUY, 999000, 10))
        self.assertEqual(book.orphans, 1)

    def test_orphans_are_counted_not_hidden(self):
        book = Book(strict=False)
        book.apply(message(SUBMIT, SELL, 1000000, 100))
        book.apply(message(EXECUTE, SELL, 1000000, 150))
        self.assertEqual(book.orphans, 1)


class Invariants(unittest.TestCase):
    def test_clear_empties_both_sides(self):
        book = Book()
        book.apply(message(SUBMIT, SELL, 1000000, 100))
        book.apply(message(SUBMIT, BUY, 999000, 100))
        book.apply(message(Action.CLEAR, BUY, 0, 0))
        self.assertIsNone(book.best_bid)
        self.assertIsNone(book.best_ask)

    def test_modify_is_rejected_rather_than_guessed_at(self):
        book = Book()
        book.apply(message(SUBMIT, SELL, 1000000, 100))
        with self.assertRaises(BookError):
            book.apply(message(Action.MODIFY, SELL, 1000000, 50))

    def test_crossed_detects_an_impossible_book(self):
        book = Book()
        book.apply(message(SUBMIT, SELL, 999000, 100))
        self.assertFalse(book.crossed)
        book.apply(message(SUBMIT, BUY, 1000000, 100))
        self.assertTrue(book.crossed)

if __name__ == "__main__":
    unittest.main()
