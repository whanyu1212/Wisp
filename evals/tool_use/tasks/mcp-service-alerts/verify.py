"""Pass only for exactly the listed services with an open high or critical alert.

The traps: an open medium alert, a fixed critical alert, an open low alert, and a
critical alert in a repository the team's list does not include. Every item the
answer lists counts, so a service that does not exist fails it too.
"""

import re
import sys
from pathlib import Path

expected = {"billing-gateway", "ledger-sync", "notify-hub", "search-indexer"}
try:
    lines = Path("ANSWER.txt").read_text(encoding="utf-8").lower().splitlines()
except OSError:
    lines = []
found = set()
for line in lines:
    # Formatting is not the task: allow bullets, backticks, `acme/` prefixes, and
    # several names on a line separated by commas or "and".
    for item in re.split(r",|\band\b", line.replace("`", "")):
        name = item.strip().lstrip("-*• ").removeprefix("acme/").strip().rstrip(".")
        if name:
            found.add(name)
print("ok" if found == expected else f"expected {sorted(expected)}, found {sorted(found)}")
sys.exit(0 if found == expected else 1)
