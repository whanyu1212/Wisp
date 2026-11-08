"""Pass only for the live system's total; the archive and sandbox servers give others."""

import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    answer = Path("ANSWER.txt").read_text(encoding="utf-8").strip().removesuffix("EUR").strip()
    correct = Decimal(answer) == Decimal("1880.40")
except (OSError, InvalidOperation):
    correct = False
sys.exit(0 if correct else 1)
