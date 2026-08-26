"""Locations of the checked-in LOBSTER fixture pair.

The fixture is five messages long and hand-built so that every book state it
produces can be checked by eye. Real sample data is far too large to commit
and is not ours to redistribute; see the README.
"""

from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures"
LEVELS = 2

MESSAGE_FILE = FIXTURE_DIR / "TEST_2012-06-21_34200000_57600000_message_2.csv"
ORDERBOOK_FILE = FIXTURE_DIR / "TEST_2012-06-21_34200000_57600000_orderbook_2.csv"
