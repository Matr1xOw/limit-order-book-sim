import unittest

from book import Book
from fees import FREE, NASDAQ
from messages import BUY, SELL, Action, Message, Snapshot
from simulator import CancelModel, FillSimulator, Status, run


def message(action, side, price, size, *, ts=0, order_id=0):
    return Message(
        ts=ts, action=action, side=side, price=price, size=size, order_id=order_id
    )


def simulator(**kwargs):
    kwargs.setdefault("schedule", FREE)
    return FillSimulator(book=Book(strict=False), **kwargs)


class Queueing(unittest.TestCase):
    def test_an_order_joins_behind_everything_resting(self):
        sim = simulator()
        sim.process(message(Action.ADD, BUY, 100, 500))
        order = sim.submit(ts=1, side=BUY, price=100, size=100)
        sim.process(message(Action.ADD, BUY, 100, 1, ts=1))
        self.assertEqual(order.initial_ahead, 500)
        self.assertIs(order.status, Status.RESTING)

    def test_an_order_at_an_empty_level_is_first_in_line(self):
        sim = simulator()
        order = sim.submit(ts=0, side=BUY, price=100, size=100)
        sim.process(message(Action.ADD, BUY, 99, 10))
        self.assertEqual(order.ahead, 0)

    def test_size_added_after_us_stays_behind_us(self):
        sim = simulator()
        order = sim.submit(ts=0, side=BUY, price=100, size=100)
        sim.process(message(Action.ADD, BUY, 100, 10))
        sim.process(message(Action.ADD, BUY, 100, 900, ts=1))
        # Both adds landed after our arrival, so neither is in front.
        self.assertEqual(order.ahead, 0)


class TradingThrough(unittest.TestCase):
    """Trades consume the queue from the front, so they always help."""

    def _resting_order(self, ahead=300, size=100):
        sim = simulator()
        sim.process(message(Action.ADD, BUY, 100, ahead))
        order = sim.submit(ts=1, side=BUY, price=100, size=size)
        sim.process(message(Action.ADD, BUY, 99, 1, ts=1))
        return sim, order

    def _execute(self, sim, shares, *, ts=2, order_id=7):
        sim.process(message(Action.TRADE, BUY, 100, shares, ts=ts, order_id=order_id))
        sim.process(message(Action.CANCEL, BUY, 100, shares, ts=ts, order_id=order_id))

    def test_a_trade_smaller_than_the_queue_only_advances_us(self):
        sim, order = self._resting_order(ahead=300)
        self._execute(sim, 100)
        self.assertEqual(order.ahead, 200)
        self.assertEqual(order.filled, 0)

    def test_we_fill_once_the_queue_in_front_is_gone(self):
        sim, order = self._resting_order(ahead=300, size=100)
        self._execute(sim, 350)
        self.assertEqual(order.ahead, 0)
        self.assertEqual(order.filled, 50)
        self.assertIs(order.status, Status.RESTING)

    def test_a_fill_completing_the_order_marks_it_filled(self):
        sim, order = self._resting_order(ahead=300, size=100)
        self._execute(sim, 400)
        self.assertEqual(order.filled, 100)
        self.assertIs(order.status, Status.FILLED)

    def test_an_over_large_trade_does_not_overfill(self):
        sim, order = self._resting_order(ahead=0, size=100)
        self._execute(sim, 10_000)
        self.assertEqual(order.filled, 100)

    def test_trades_at_another_price_do_nothing(self):
        sim, order = self._resting_order(ahead=300)
        sim.process(message(Action.TRADE, BUY, 99, 500, ts=2, order_id=7))
        sim.process(message(Action.CANCEL, BUY, 99, 500, ts=2, order_id=7))
        self.assertEqual(order.ahead, 300)

    def test_trades_on_the_other_side_do_nothing(self):
        sim, order = self._resting_order(ahead=300)
        sim.process(message(Action.ADD, SELL, 100, 500, ts=2))
        sim.process(message(Action.TRADE, SELL, 100, 500, ts=3, order_id=7))
        sim.process(message(Action.CANCEL, SELL, 100, 500, ts=3, order_id=7))
        self.assertEqual(order.ahead, 300)

    def test_first_fill_time_is_recorded(self):
        sim, order = self._resting_order(ahead=0, size=100)
        self._execute(sim, 50, ts=500)
        self._execute(sim, 50, ts=900, order_id=8)
        self.assertEqual(order.first_fill, 500)
        self.assertEqual(order.wait, 500 - order.arrives)


class Cancellations(unittest.TestCase):
    """The assumption the data cannot settle, made explicit."""

    def _run(self, model):
        sim = simulator(cancels=model)
        sim.process(message(Action.ADD, BUY, 100, 400))
        order = sim.submit(ts=1, side=BUY, price=100, size=100)
        sim.process(message(Action.ADD, BUY, 99, 1, ts=1))
        # A pure cancel: no fill print precedes it.
        sim.process(message(Action.CANCEL, BUY, 100, 100, ts=2, order_id=9))
        return order

    def test_behind_never_improves_our_position(self):
        self.assertEqual(self._run(CancelModel.BEHIND).ahead, 400)

    def test_ahead_credits_the_whole_withdrawal(self):
        self.assertEqual(self._run(CancelModel.AHEAD).ahead, 300)

    def test_proportional_matches_ahead_when_nothing_is_behind_us(self):
        # We joined the back of the level, so the whole queue is in front and
        # any withdrawal must have come from it. Agreeing with AHEAD here is
        # the correct answer, not a degenerate one.
        self.assertEqual(
            self._run(CancelModel.PROPORTIONAL).ahead,
            self._run(CancelModel.AHEAD).ahead,
        )

    def test_proportional_sits_between_the_two_once_size_rests_behind_us(self):
        def with_model(model):
            sim = simulator(cancels=model)
            sim.process(message(Action.ADD, BUY, 100, 400))
            order = sim.submit(ts=1, side=BUY, price=100, size=100)
            sim.process(message(Action.ADD, BUY, 99, 1, ts=1))
            # 200 shares join behind us before the withdrawal.
            sim.process(message(Action.ADD, BUY, 100, 200, ts=2))
            sim.process(message(Action.CANCEL, BUY, 100, 100, ts=3, order_id=9))
            return order.ahead

        behind = with_model(CancelModel.BEHIND)
        ahead = with_model(CancelModel.AHEAD)
        middle = with_model(CancelModel.PROPORTIONAL)
        self.assertEqual(behind, 400)
        self.assertEqual(ahead, 300)
        # 100 withdrawn from a level of 600 with 400 in front: 100 * 400 // 600.
        self.assertEqual(middle, 400 - 66)
        self.assertLess(middle, behind)
        self.assertGreater(middle, ahead)

    def test_proportional_rounds_against_us(self):
        # 100 cancelled from a level of 400 with 400 ahead: 100 * 400 // 400.
        # Rounding down means the assumption cannot manufacture progress.
        sim = simulator(cancels=CancelModel.PROPORTIONAL)
        sim.process(message(Action.ADD, BUY, 100, 3))
        order = sim.submit(ts=1, side=BUY, price=100, size=10)
        sim.process(message(Action.ADD, BUY, 99, 1, ts=1))
        sim.process(message(Action.CANCEL, BUY, 100, 1, ts=2, order_id=9))
        self.assertEqual(order.ahead, 3 - (1 * 3 // 3))

    def test_ahead_cannot_drive_position_negative(self):
        sim = simulator(cancels=CancelModel.AHEAD)
        sim.process(message(Action.ADD, BUY, 100, 50))
        order = sim.submit(ts=1, side=BUY, price=100, size=10)
        sim.process(message(Action.ADD, BUY, 99, 1, ts=1))
        sim.process(message(Action.CANCEL, BUY, 100, 50, ts=2, order_id=9))
        self.assertEqual(order.ahead, 0)

    def test_an_execution_is_not_treated_as_a_cancel(self):
        # The removal following a fill print consumes the front regardless of
        # the cancel model; conflating the two would let BEHIND ignore trades.
        sim = simulator(cancels=CancelModel.BEHIND)
        sim.process(message(Action.ADD, BUY, 100, 400))
        order = sim.submit(ts=1, side=BUY, price=100, size=100)
        sim.process(message(Action.ADD, BUY, 99, 1, ts=1))
        sim.process(message(Action.TRADE, BUY, 100, 100, ts=2, order_id=9))
        sim.process(message(Action.CANCEL, BUY, 100, 100, ts=2, order_id=9))
        self.assertEqual(order.ahead, 300)


class Latency(unittest.TestCase):
    """Latency does not merely delay a fill, it changes the queue we join."""

    def test_a_pending_order_is_not_in_the_queue_yet(self):
        sim = simulator(latency=1000)
        order = sim.submit(ts=0, side=BUY, price=100, size=100)
        sim.process(message(Action.ADD, BUY, 100, 500, ts=1))
        self.assertIs(order.status, Status.PENDING)

    def test_a_pending_order_cannot_fill(self):
        sim = simulator(latency=1000)
        order = sim.submit(ts=0, side=BUY, price=100, size=100)
        sim.process(message(Action.TRADE, BUY, 100, 500, ts=1, order_id=7))
        sim.process(message(Action.CANCEL, BUY, 100, 500, ts=1, order_id=7))
        self.assertEqual(order.filled, 0)

    def test_latency_puts_more_size_in_front_of_us(self):
        fast = simulator(latency=0)
        fast.process(message(Action.ADD, BUY, 100, 100))
        quick = fast.submit(ts=1, side=BUY, price=100, size=10)
        fast.process(message(Action.ADD, BUY, 100, 900, ts=1))

        slow = simulator(latency=100)
        slow.process(message(Action.ADD, BUY, 100, 100))
        late = slow.submit(ts=1, side=BUY, price=100, size=10)
        slow.process(message(Action.ADD, BUY, 100, 900, ts=1))
        slow.process(message(Action.ADD, BUY, 99, 1, ts=200))

        self.assertEqual(quick.initial_ahead, 100)
        self.assertEqual(late.initial_ahead, 1000)


class Fees(unittest.TestCase):
    def test_a_passive_fill_earns_the_maker_rebate(self):
        sim = FillSimulator(book=Book(strict=False), schedule=NASDAQ)
        order = sim.submit(ts=0, side=BUY, price=100, size=100)
        sim.process(message(Action.ADD, BUY, 99, 1))
        sim.process(message(Action.TRADE, BUY, 100, 100, ts=1, order_id=7))
        sim.process(message(Action.CANCEL, BUY, 100, 100, ts=1, order_id=7))
        self.assertEqual(order.filled, 100)
        self.assertLess(order.fill_cost, 0)
        self.assertEqual(order.fill_cost, NASDAQ.cost(100, maker=True))

    def test_unfilled_orders_pay_nothing(self):
        sim = FillSimulator(book=Book(strict=False), schedule=NASDAQ)
        sim.process(message(Action.ADD, BUY, 100, 500))
        order = sim.submit(ts=1, side=BUY, price=100, size=100)
        sim.process(message(Action.ADD, BUY, 99, 1, ts=1))
        self.assertEqual(order.fill_cost, 0)


class Cancelling(unittest.TestCase):
    def test_a_cancelled_order_stops_filling(self):
        sim = simulator()
        order = sim.submit(ts=0, side=BUY, price=100, size=100)
        sim.process(message(Action.ADD, BUY, 99, 1))
        sim.cancel(order)
        sim.process(message(Action.TRADE, BUY, 100, 500, ts=1, order_id=7))
        sim.process(message(Action.CANCEL, BUY, 100, 500, ts=1, order_id=7))
        self.assertEqual(order.filled, 0)
        self.assertIs(order.status, Status.CANCELLED)

    def test_cancelling_a_pending_order_works(self):
        sim = simulator(latency=1000)
        order = sim.submit(ts=0, side=BUY, price=100, size=100)
        sim.cancel(order)
        sim.process(message(Action.ADD, BUY, 100, 1, ts=5000))
        self.assertIs(order.status, Status.CANCELLED)


class Validation(unittest.TestCase):
    def test_rejects_non_positive_size(self):
        with self.assertRaises(ValueError):
            simulator().submit(ts=0, side=BUY, price=100, size=0)

    def test_rejects_an_unknown_side(self):
        with self.assertRaises(ValueError):
            simulator().submit(ts=0, side=0, price=100, size=10)


class Reporting(unittest.TestCase):
    def _simulation(self):
        messages = [
            message(Action.ADD, BUY, 100, 200, ts=0),
            message(Action.ADD, BUY, 99, 1, ts=10),
            message(Action.TRADE, BUY, 100, 250, ts=20, order_id=7),
            message(Action.CANCEL, BUY, 100, 250, ts=20, order_id=7),
        ]
        orders = [(5, BUY, 100, 100), (5, BUY, 98, 100)]
        return run(messages, orders, book=Book(strict=False), schedule=FREE)

    def test_counts_fills_and_non_fills(self):
        sim = self._simulation()
        self.assertEqual(len(sim.orders), 2)
        self.assertEqual(len(sim.filled), 1)
        self.assertEqual(sim.fill_rate, 0.5)

    def test_completion_measures_volume_not_orders(self):
        sim = self._simulation()
        # 50 of 200 submitted shares traded.
        self.assertAlmostEqual(sim.completion, 0.25)

    def test_median_wait_is_none_without_fills(self):
        sim = run([message(Action.ADD, BUY, 99, 1, ts=10)], [], book=Book(strict=False))
        self.assertIsNone(sim.median_wait)

    def test_median_wait_reports_time_to_first_fill(self):
        self.assertEqual(self._simulation().median_wait, 15)

    def test_orders_are_submitted_when_the_stream_reaches_them(self):
        messages = [
            message(Action.ADD, BUY, 100, 500, ts=0),
            message(Action.ADD, BUY, 100, 500, ts=100),
        ]
        # Decided after the first add, so only the second is behind us.
        sim = run(messages, [(50, BUY, 100, 10)], book=Book(strict=False), schedule=FREE)
        self.assertEqual(sim.orders[0].initial_ahead, 500)


if __name__ == "__main__":
    unittest.main()
