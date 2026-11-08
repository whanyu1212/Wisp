import unittest

from pricing import total_with_tax


class TotalWithTax(unittest.TestCase):
    def test_adds_tax(self) -> None:
        self.assertEqual(total_with_tax(100.0, 0.08), 108.0)

    def test_zero_rate(self) -> None:
        self.assertEqual(total_with_tax(19.99, 0.0), 19.99)

    def test_rounds_to_cents(self) -> None:
        self.assertEqual(total_with_tax(12.5, 0.1), 13.75)
