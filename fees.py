"""Exchange fee and rebate schedules.

Small numbers that decide large questions. US equity venues charge for taking
liquidity and pay for posting it, both per share, and the amounts are a
material fraction of the edge a short-horizon strategy claims. A model that
omits them cannot tell a signal from a rebate-capture scheme wearing one, and
the two look identical in a P&L that counts only price differences.

The units are nanodollars per share, matching prices, so a fill's cost is an
integer and stays one. Rebates are negative costs rather than a separate
concept — netting them anywhere else invites a sign error in the one place
where a sign error changes the conclusion rather than the magnitude.
"""

from __future__ import annotations

from dataclasses import dataclass

from messages import PRICE_SCALE

#: A tenth of a cent per share, the unit venue schedules are quoted in.
MIL = PRICE_SCALE // 10_000


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """What a venue charges per share.

    Both fields are costs: positive money leaving. A maker rebate is therefore
    negative, which is the whole point of the sign convention — `taker + maker`
    is the round-trip cost of crossing and posting, with no special-casing.
    """

    name: str

    taker: int
    """Cost per share for removing liquidity. Positive."""

    maker: int
    """
    Cost per share for adding liquidity. Negative where the venue pays you.

    Note that being paid to post is exactly what makes queue position worth
    modelling: a rebate is only collected on orders that actually fill, and
    an order that never reaches the front of the queue collects nothing while
    still bearing the risk of the position it was trying to open.
    """

    def cost(self, shares: int, *, maker: bool) -> int:
        """Total cost in nanodollars for a fill of `shares`.

        Negative when a rebate exceeds nothing else — that is a credit.
        """
        if shares < 0:
            raise ValueError(f"shares must not be negative, got {shares}")
        return shares * (self.maker if maker else self.taker)

    def round_trip(self, shares: int, *, entry_maker: bool, exit_maker: bool) -> int:
        """Cost of opening and closing a position of `shares`.

        The number worth comparing a strategy's gross edge against. Posting
        both sides on a rebate venue is a credit; crossing both is roughly
        six mils a share, which is more than many claimed edges survive.
        """
        return self.cost(shares, maker=entry_maker) + self.cost(
            shares, maker=exit_maker
        )


#: Nasdaq's standard equity schedule: 30 mils to take, 20 back to post.
#:
#: These are the headline rates. Real schedules are tiered on monthly volume
#: and the top tiers are meaningfully better, so a firm doing size pays less
#: than this — which means results computed here are, again, pessimistic.
NASDAQ = FeeSchedule(name="nasdaq", taker=30 * MIL, maker=-20 * MIL)

#: A venue that charges nothing either way.
#:
#: Not realistic, and not meant to be. It is the control: run a strategy under
#: this and under NASDAQ, and the difference is exactly what the fee structure
#: contributed. A strategy whose profit appears only under a rebate schedule
#: has not found an edge, it has found a subsidy.
FREE = FeeSchedule(name="free", taker=0, maker=0)

#: An inverted venue, paying takers and charging makers.
#:
#: Real ones exist. Included because they invert the queue-position argument:
#: where posting costs money, the reason to wait in a queue has to come from
#: the spread alone.
INVERTED = FeeSchedule(name="inverted", taker=-10 * MIL, maker=30 * MIL)

SCHEDULES = {schedule.name: schedule for schedule in (NASDAQ, FREE, INVERTED)}
