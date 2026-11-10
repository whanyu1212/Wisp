"""Pass only for exactly the listed services with an open high or critical alert.

The traps: an open medium alert, a fixed critical alert, an open low alert, and a
critical alert in a repository the team's list does not include.
"""

import re
import sys
from pathlib import Path

expected = {"billing-gateway", "ledger-sync", "notify-hub", "search-indexer"}
# Every repository name the answer could name: the team's list and the one outside it.
names = set(Path("services.txt").read_text(encoding="utf-8").split()) | {"legacy-billing"}
try:
    text = Path("ANSWER.txt").read_text(encoding="utf-8").lower()
    # Only repository names count, so bullets, `acme/` prefixes and notes do not.
    found = set(re.findall(r"[a-z]+(?:-[a-z]+)+", text)) & names
except OSError:
    found = set()
print("ok" if found == expected else f"expected {sorted(expected)}, found {sorted(found)}")
sys.exit(0 if found == expected else 1)
