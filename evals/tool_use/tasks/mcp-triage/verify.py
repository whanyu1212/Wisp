"""Pass only for the right issue, pull request and check; each trap gives another answer."""

import sys
from pathlib import Path

expected = {"issue": "412", "pr": "418", "check": "e2e-safari"}
try:
    found = {}
    for line in Path("TRIAGE.md").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            found[key.strip().lower()] = value.strip().lstrip("#")
except OSError:
    found = {}
missing = {key: value for key, value in expected.items() if found.get(key) != value}
print("ok" if not missing else f"expected {missing}, found {found}")
sys.exit(1 if missing else 0)
