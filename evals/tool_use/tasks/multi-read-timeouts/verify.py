"""Pass only for the right service and timeout; each trap in the fixture gives another answer."""

import sys
from pathlib import Path

try:
    name, _, seconds = Path("ANSWER.txt").read_text(encoding="utf-8").strip().partition("=")
    correct = name.strip() == "search" and abs(float(seconds) - 1.35) < 1e-9
except (OSError, ValueError):
    correct = False
sys.exit(0 if correct else 1)
