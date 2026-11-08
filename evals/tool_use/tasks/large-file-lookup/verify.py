"""Pass only for 12.5; the file's decoys give 30, 45 or 3."""

import sys
from pathlib import Path

try:
    correct = float(Path("ANSWER.txt").read_text(encoding="utf-8").strip()) == 12.5
except (OSError, ValueError):
    correct = False
sys.exit(0 if correct else 1)
