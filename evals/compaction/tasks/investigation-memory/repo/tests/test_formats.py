import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import csv_format, tsv_format  # noqa: E402


class FormatTest(unittest.TestCase):
    def test_tsv_keeps_first_data_row(self) -> None:
        text = "sku\tqty\nA1\t3\nB2\t5\n"
        self.assertEqual(tsv_format.parse_rows(text), [["A1", "3"], ["B2", "5"]])

    def test_csv(self) -> None:
        self.assertEqual(csv_format.parse_rows("sku,qty\nA1,3\n"), [["A1", "3"]])


if __name__ == "__main__":
    unittest.main()
