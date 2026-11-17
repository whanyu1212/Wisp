"""Check the exact recovered report row."""

from pathlib import Path
import sys

answer = Path("ANSWER.txt")
expected = f"row 4500: {(4500 * 7919 + 104729) % 999983}\n"
sys.exit(0 if answer.is_file() and answer.read_text() == expected else 1)
