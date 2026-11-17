"""Require every blocked open EU release gate and reject additional claims."""

import re
import sys
from pathlib import Path

expected = {3003, 3017, 3033}
try:
    answer = Path("ANSWER.txt").read_text(encoding="utf-8")
except OSError:
    answer = ""
items = [line.strip().lstrip("-*• ").strip("`# ") for line in answer.splitlines() if line.strip()]
valid = all(re.fullmatch(r"\d+", item) for item in items)
found = {int(item) for item in items} if valid else set()
passed = valid and found == expected
print("ok" if passed else f"expected {sorted(expected)}, found {items}")
sys.exit(0 if passed else 1)
