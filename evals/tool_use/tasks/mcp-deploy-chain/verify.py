"""Pass only for the pull request the failed deploy merged and the person who approved it.

The traps are newer runs that look like the answer: a failed deploy of another branch,
a failed `deploy-docs` run, a cancelled deploy and a failed deploy of a release branch.
Each merged a different pull request with a different approver, and the right pull
request also has a review that requested changes.
"""

import sys
from pathlib import Path

expected = {"pr": "493", "approved by": "rafael-o"}
try:
    found = {}
    for line in Path("ANSWER.txt").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            # Formatting is not the task: allow backticks, a leading `#` or `@`, and any case.
            found[key.strip().strip("`*- ").lower()] = value.strip().strip("`").lstrip("#@").lower()
except OSError:
    found = {}
missing = {key: value for key, value in expected.items() if found.get(key) != value}
print("ok" if not missing else f"expected {missing}, found {found}")
sys.exit(1 if missing else 0)
