"""Reject branch contamination, missing output and extra claims."""
from pathlib import Path
import sys

path = Path("ANSWER.txt")
sys.exit(0 if path.is_file() and path.read_text() == "cerulean\n" else 1)
