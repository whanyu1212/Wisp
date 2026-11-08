"""Pass only for the reorder point; the server's decoy tools give other numbers."""

import sys
from pathlib import Path

try:
    correct = int(Path("ANSWER.txt").read_text(encoding="utf-8").strip()) == 140
except (OSError, ValueError):
    correct = False
sys.exit(0 if correct else 1)
