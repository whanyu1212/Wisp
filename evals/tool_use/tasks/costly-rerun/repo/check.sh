#!/usr/bin/env bash
# Fast consistency check: exits 0 when every balance matches its entries. Prints no details.
python3 - <<'PY'
import csv
from collections import defaultdict

totals = defaultdict(float)
with open("data/entries.csv", newline="") as handle:
    for row in csv.DictReader(handle):
        totals[row["account"]] += float(row["amount"])
with open("data/accounts.csv", newline="") as handle:
    bad = sum(abs(float(r["balance"]) - round(totals[r["account"]], 2)) > 0.005 for r in csv.DictReader(handle))
print("consistent" if not bad else "inconsistent")
raise SystemExit(1 if bad else 0)
PY
