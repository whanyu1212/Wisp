import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from inventory.pricing import line_total, order_total  # noqa: E402


class PricingTest(unittest.TestCase):
    def test_line_total_rounds_to_cents(self) -> None:
        self.assertEqual(line_total(Decimal("3.33"), 3, 10), Decimal("8.99"))

    def test_order_total(self) -> None:
        lines = [(Decimal("3.33"), 3, 10), (Decimal("1.25"), 2, 0)]
        self.assertEqual(order_total(lines), Decimal("11.49"))


if __name__ == "__main__":
    unittest.main()
