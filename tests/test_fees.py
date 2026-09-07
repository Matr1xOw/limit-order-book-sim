import unittest

from fees import FREE, INVERTED, MIL, NASDAQ, SCHEDULES, FeeSchedule
from messages import PRICE_SCALE


class Units(unittest.TestCase):
    def test_a_mil_is_a_tenth_of_a_cent(self):
        self.assertEqual(MIL * 10_000, PRICE_SCALE)
        self.assertEqual(MIL * 1_000, PRICE_SCALE // 10)  # a mil is $0.0001

    def test_costs_stay_integers(self):
        self.assertIsInstance(NASDAQ.cost(100, maker=True), int)


class Signs(unittest.TestCase):
    """Costs are positive, rebates negative. Everything else follows."""

    def test_taking_liquidity_costs_money(self):
        self.assertGreater(NASDAQ.cost(100, maker=False), 0)

    def test_posting_liquidity_earns_money_on_nasdaq(self):
        self.assertLess(NASDAQ.cost(100, maker=True), 0)

    def test_headline_nasdaq_rates(self):
        # 30 mils to take, 20 back to post, per share.
        self.assertEqual(NASDAQ.cost(1, maker=False), 30 * MIL)
        self.assertEqual(NASDAQ.cost(1, maker=True), -20 * MIL)

    def test_inverted_venue_reverses_both(self):
        self.assertLess(INVERTED.cost(100, maker=False), 0)
        self.assertGreater(INVERTED.cost(100, maker=True), 0)

    def test_cost_scales_with_size(self):
        self.assertEqual(NASDAQ.cost(200, maker=False), 2 * NASDAQ.cost(100, maker=False))

    def test_zero_shares_cost_nothing(self):
        self.assertEqual(NASDAQ.cost(0, maker=True), 0)

    def test_negative_size_is_rejected(self):
        # A short is still a positive number of shares; a negative one here
        # would silently flip a fee into a rebate.
        with self.assertRaises(ValueError):
            NASDAQ.cost(-100, maker=False)


class RoundTrip(unittest.TestCase):
    def test_crossing_both_ways_is_the_expensive_case(self):
        crossing = NASDAQ.round_trip(100, entry_maker=False, exit_maker=False)
        posting = NASDAQ.round_trip(100, entry_maker=True, exit_maker=True)
        self.assertGreater(crossing, posting)
        self.assertEqual(crossing, 100 * 60 * MIL)

    def test_posting_both_ways_is_a_credit_on_nasdaq(self):
        self.assertLess(NASDAQ.round_trip(100, entry_maker=True, exit_maker=True), 0)

    def test_mixed_round_trip_nets_the_two(self):
        self.assertEqual(
            NASDAQ.round_trip(100, entry_maker=True, exit_maker=False),
            100 * 10 * MIL,
        )

    def test_a_free_venue_costs_nothing_either_way(self):
        self.assertEqual(FREE.round_trip(100, entry_maker=True, exit_maker=False), 0)


class Control(unittest.TestCase):
    def test_free_is_the_control_for_isolating_fee_effects(self):
        # Running under both and differencing is how you tell an edge from a
        # subsidy, so the two must differ for an otherwise identical fill.
        self.assertNotEqual(
            FREE.cost(100, maker=True), NASDAQ.cost(100, maker=True)
        )

    def test_schedules_are_registered_by_name(self):
        self.assertEqual(SCHEDULES["nasdaq"], NASDAQ)
        self.assertEqual(set(SCHEDULES), {"nasdaq", "free", "inverted"})

    def test_schedules_are_immutable(self):
        with self.assertRaises(AttributeError):
            NASDAQ.taker = 0  # type: ignore[misc]

    def test_a_custom_schedule_can_be_built(self):
        venue = FeeSchedule(name="custom", taker=25 * MIL, maker=-15 * MIL)
        self.assertEqual(venue.round_trip(100, entry_maker=True, exit_maker=False), 1000 * MIL)


if __name__ == "__main__":
    unittest.main()
