"""Pass only when the catalog message is fixed and no other file changed, frozen legacy included."""

import filecmp
import sys
from pathlib import Path

task = Path(__file__).parent
fixture = task / "repo"
expected = task / "solution" / "app" / "errors" / "catalog.py"
catalog = Path("app/errors/catalog.py")

changed = []
for original in fixture.rglob("*"):
    if original.is_file():
        current = original.relative_to(fixture)
        if current != catalog and not (current.is_file() and filecmp.cmp(original, current, shallow=False)):
            changed.append(str(current))
fixed = catalog.is_file() and filecmp.cmp(expected, catalog, shallow=False)
print("changed:", ", ".join(changed) or "nothing else")
sys.exit(0 if fixed and not changed else 1)
