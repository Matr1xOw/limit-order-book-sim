import unittest

from fees import FREE, MIL, NASDAQ
from messages import BUY, PRICE_SCALE, SELL, Action, Message, Snapshot
from simulator import CancelModel
from sweep import Outcome, Timeline, build_timeline, sweep_one


def message(action, side, price, size, *, ts=0, order_id=0):
    return Message(
        ts=ts, action=action, side=side, price=price, size=size, order_id=order_id
    )


class MidTimeline(unittest.TestCase):
    def test_records_the_mid_when_the_touch_moves(self):
        messages = [
            message(Action.ADD, BUY, 100, 10, ts=0),
            message(Action.ADD, SELL, 102, 10, ts=1),
            message(Action.ADD, BUY, 101, 10, ts=2),
        ]
        timeline = build_timeline(messages, None)
        # Nothing until both sides exist. The later bid raises the mid to
        # 101.5, which floors back to 101, so it is not a new value.
        self.assertEqual(timeline.times, [1])
        self.assertEqual(timeline.mids, [101])

    def test_records_a_mid_that_actually_changes(self):
        messages = [
            message(Action.ADD, BUY, 100, 10, ts=0),
            message(Action.ADD, SELL, 102, 10, ts=1),
            message(Action.ADD, SELL, 101, 10, ts=2),
        ]
        timeline = build_timeline(messages, None)
        self.assertEqual(timeline.times, [1, 2])
        self.assertEqual(timeline.mids, [101, 100])

    def test_ignores_messages_that_do_not_move_the_mid(self):
        messages = [
            message(Action.ADD, BUY, 100, 10, ts=0),
            message(Action.ADD, SELL, 102, 10, ts=1),
            message(Action.ADD, BUY, 99, 10, ts=2),
            message(Action.ADD, SELL, 103, 10, ts=3),
        ]
        self.assertEqual(build_timeline(messages, None).times, [1])

    def test_looks_up_the_most_recent_mid(self):
        timeline = Timeline(times=[10, 20, 30], mids=[100, 200, 300])
        self.assertEqual(timeline.mid_at(10), 100)
        self.assertEqual(timeline.mid_at(25), 200)
        self.assertEqual(timeline.mid_at(1000), 300)

    def test_returns_none_before_the_first_mid(self):
        self.assertIsNone(Timeline(times=[10], mids=[100]).mid_at(5))

    def test_does_not_interpolate(self):
        # A mid between two states is a price that never existed, and
        # inventing one would smooth exactly the jumps that adverse
        # selection consists of.
        timeline = Timeline(times=[0, 100], mids=[100, 200])
        self.assertEqual(timeline.mid_at(50), 100)

    def test_empty_timeline_is_safe(self):
        self.assertIsNone(Timeline(times=[], mids=[]).mid_at(1))


class Accounting(unittest.TestCase):
    """Fees are applied after the fact, so the same fills price under any venue."""

    # Not named _outcome: unittest.TestCase keeps its own attribute by that
    # name, and shadowing it replaces this helper with the runner's result.
    def _make(self, gross, shares=100):
        return Outcome(
            latency=0,
            cancels=CancelModel.BEHIND,
            submitted=1,
            filled=1,
            shares=shares,
            gross=gross,
        )

    def test_a_free_venue_leaves_gross_untouched(self):
        self.assertEqual(self._make(gross=500).net(FREE), 500)

    def test_a_rebate_improves_the_result(self):
        outcome = self._make(gross=0, shares=100)
        # 20 mils a share, collected rather than paid.
        self.assertEqual(outcome.net(NASDAQ), 100 * 20 * MIL)
        self.assertGreater(outcome.net(NASDAQ), outcome.net(FREE))

    def test_per_share_is_reported_in_cents(self):
        outcome = self._make(gross=PRICE_SCALE, shares=100)
        # One dollar over a hundred shares is one cent a share.
        self.assertAlmostEqual(outcome.per_share(FREE), 1.0)

    def test_per_share_of_nothing_is_zero(self):
        self.assertEqual(self._make(gross=0, shares=0).per_share(FREE), 0.0)

    def test_fill_rate_counts_submissions(self):
        outcome = Outcome(
            latency=0, cancels=CancelModel.BEHIND, submitted=4, filled=1
        )
        self.assertEqual(outcome.fill_rate, 0.25)

    def test_fill_rate_without_submissions_is_zero(self):
        self.assertEqual(
            Outcome(latency=0, cancels=CancelModel.BEHIND).fill_rate, 0.0
        )

    def test_median_queue_reports_the_middle_arrival(self):
        outcome = Outcome(latency=0, cancels=CancelModel.BEHIND)
        outcome.queue_on_arrival = [10, 500, 30]
        self.assertEqual(outcome.median_queue, 30)


class Markout(unittest.TestCase):
    """Direction matters: buying below the later mid is a gain, selling is not."""

    def _run(self, side, fill_price, later_mid):
        # Seeded so both sides of the touch exist at the first decision;
        # otherwise the strategy quotes whichever side happens to appear
        # first and the other is never tested.
        seed = Snapshot(bids=((100, 10),), asks=((102, 10),))
        messages = [
            message(Action.ADD, BUY, 98, 1, ts=0),
            message(Action.TRADE, side, fill_price, 500, ts=10, order_id=7),
            message(Action.CANCEL, side, fill_price, 500, ts=10, order_id=7),
        ]
        timeline = Timeline(times=[0, 100], mids=[101, later_mid])
        return sweep_one(
            messages,
            seed,
            timeline,
            latency=0,
            cancels=CancelModel.BEHIND,
            interval=10**12,
            lifetime=10**12,
            horizon=90,
            size=100,
        )

    def test_a_buy_gains_when_the_mid_rises(self):
        outcome = self._run(BUY, 100, later_mid=110)
        self.assertGreater(outcome.gross, 0)

    def test_a_buy_loses_when_the_mid_falls(self):
        # The adverse selection case: filled just before the market left.
        outcome = self._run(BUY, 100, later_mid=90)
        self.assertLess(outcome.gross, 0)

    def test_a_sell_gains_when_the_mid_falls(self):
        outcome = self._run(SELL, 102, later_mid=90)
        self.assertGreater(outcome.gross, 0)

    def test_a_sell_loses_when_the_mid_rises(self):
        outcome = self._run(SELL, 102, later_mid=115)
        self.assertLess(outcome.gross, 0)


class Sweeping(unittest.TestCase):
    SEED = Snapshot(bids=((100, 1000),), asks=((102, 1000),))

    def _messages(self, count=400):
        return [
            message(Action.ADD, BUY, 99, 1, ts=i * 1_000_000)
            for i in range(count)
        ]

    def _sweep(self, **kwargs):
        messages = self._messages()
        timeline = build_timeline(messages, self.SEED)
        options = dict(
            latency=0,
            cancels=CancelModel.BEHIND,
            interval=100_000_000,
            lifetime=50_000_000,
            horizon=1_000_000,
            size=100,
        )
        options.update(kwargs)
        return sweep_one(messages, self.SEED, timeline, **options)

    def test_quotes_are_submitted_on_the_interval(self):
        outcome = self._sweep()
        # Both sides each time the interval elapses.
        self.assertGreater(outcome.submitted, 0)
        self.assertEqual(outcome.submitted % 2, 0)

    def test_a_one_sided_book_does_not_burn_the_decision_slot(self):
        # Quoting nothing is fine; sitting out a whole interval afterwards
        # would understate the fill rate for reasons unrelated to queues.
        seed = Snapshot(bids=((100, 10),), asks=())
        messages = [message(Action.ADD, BUY, 99, 1, ts=i) for i in range(5)]
        outcome = sweep_one(
            messages,
            seed,
            build_timeline(messages, seed),
            latency=0,
            cancels=CancelModel.BEHIND,
            interval=10**12,
            lifetime=10**12,
            horizon=1,
            size=100,
        )
        # One side available, so exactly one order, not zero and not two.
        self.assertEqual(outcome.submitted, 1)

    def test_a_deep_queue_prevents_fills(self):
        # Nothing ever trades in this stream, so nothing can fill.
        self.assertEqual(self._sweep().filled, 0)

    def test_queue_on_arrival_is_recorded(self):
        outcome = self._sweep()
        self.assertTrue(outcome.queue_on_arrival)
        # 1000 shares rest at the touch on each side before we arrive.
        self.assertEqual(outcome.median_queue, 1000)

    def test_latency_is_carried_into_the_outcome(self):
        self.assertEqual(self._sweep(latency=5_000_000).latency, 5_000_000)


if __name__ == "__main__":
    unittest.main()
