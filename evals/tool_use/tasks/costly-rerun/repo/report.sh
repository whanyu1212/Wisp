#!/usr/bin/env bash
# Nightly reconciliation report. It replays the ledger, which takes a while.
set -u
echo "reconciling ledger (this takes about 20 seconds)..."
sleep 20
python3 - <<'PY'
import csv
from collections import defaultdict

totals = defaultdict(float)
with open("data/entries.csv", newline="") as handle:
    for row in csv.DictReader(handle):
        totals[row["account"]] += float(row["amount"])
failures = 0
with open("data/accounts.csv", newline="") as handle:
    for row in csv.DictReader(handle):
        expected = round(totals[row["account"]], 2)
        if abs(float(row["balance"]) - expected) > 0.005:
            failures += 1
            print(f"account {row['account']}: MISMATCH balance {row['balance']} != entries {expected:.2f}")
        else:
            print(f"account {row['account']}: ok balance {row['balance']}")
print(f"reconciled {len(totals)} accounts, {failures} mismatched")
raise SystemExit(1 if failures else 0)
PY
