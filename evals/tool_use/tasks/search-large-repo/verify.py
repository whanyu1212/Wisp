"""Pass only when the catalog message is fixed and nothing else changed or was added."""

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
for added in Path().rglob("*"):
    if added.is_file() and ".git" not in added.parts and not (fixture / added).exists():
        changed.append(f"{added} (new)")
fixed = catalog.is_file() and filecmp.cmp(expected, catalog, shallow=False)
print("changed:", ", ".join(changed) or "nothing else")
sys.exit(0 if fixed and not changed else 1)
