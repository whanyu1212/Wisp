"""Pass only for the total over open `flaky` issues on every page.

The traps: closed issues counted too, issues labelled `flaky-infra` counted too, or
only the first page of 100 results added up.
"""

import re
import sys
from pathlib import Path

expected = 2325
try:
    numbers = re.findall(r"\d[\d,]*", Path("ANSWER.txt").read_text())
    found = int(numbers[0].replace(",", "")) if numbers else None
except OSError:
    found = None
print("ok" if found == expected else f"expected {expected}, found {found}")
sys.exit(0 if found == expected else 1)
