"""Require the exact open pull requests about refund retries."""
from pathlib import Path
import sys

expected = "503\n508\n509\n525\n528\n530\n533\n"
path = Path("ANSWER.txt")
sys.exit(0 if path.is_file() and path.read_text() == expected else 1)
