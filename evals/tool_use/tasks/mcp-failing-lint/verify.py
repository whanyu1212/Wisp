"""Pass only for exactly the open pull requests whose `lint` check failed, in any order.

The traps: a closed pull request that failed lint, one that failed `lint-docs`, one
whose lint was cancelled and one whose lint was skipped.
"""

import re
import sys
from pathlib import Path

expected = {503, 509, 516, 522, 531}
try:
    found = {int(number) for number in re.findall(r"\d+", Path("ANSWER.txt").read_text())}
except OSError:
    found = set()
print("ok" if found == expected else f"expected {sorted(expected)}, found {sorted(found)}")
sys.exit(0 if found == expected else 1)
