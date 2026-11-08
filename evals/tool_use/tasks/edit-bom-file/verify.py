"""Pass only when FAREWELL changed and the byte-order mark and other line survived."""

import sys
from pathlib import Path

expected = '﻿GREETING = "Hello"\nFAREWELL = "See you soon"\n'.encode()
sys.exit(0 if Path("messages.py").read_bytes() == expected else 1)
