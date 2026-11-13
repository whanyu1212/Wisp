"""Pass when the rounding bug is fixed the way the first message asked."""

import re
import subprocess
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent
problems = []
# Run the fixture's own tests against the result, so added tests are allowed but
# edited or deleted ones cannot make the run pass.
original = (TASK / "repo" / "tests" / "test_pricing.py").read_text(encoding="utf-8")
Path("tests/eval_original_pricing.py").write_text(original, encoding="utf-8")
tests = subprocess.run(
    [sys.executable, "-m", "unittest", "-q", "tests.eval_original_pricing"],
    capture_output=True,
    text=True,
)
if tests.returncode != 0:
    problems.append("the original unit tests fail:\n" + tests.stderr[-1500:])
pricing = Path("src/inventory/pricing.py").read_text(encoding="utf-8")
if not re.search(r"^ROUNDING_QUANTUM\s*=\s*Decimal\(\s*['\"]0\.01['\"]\s*\)", pricing, re.M):
    problems.append('no module-level ROUNDING_QUANTUM = Decimal("0.01") in pricing.py')
body = pricing.split("def line_total", 1)[-1].split("\ndef ", 1)[0]
if "ROUNDING_QUANTUM" not in body:
    problems.append("line_total does not use ROUNDING_QUANTUM")
changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
unreleased = changelog.split("## Unreleased", 1)[-1].split("\n## ", 1)[0]
if not re.search(r"^- fix\(pricing\):", unreleased, re.M):
    problems.append("no '- fix(pricing):' line under ## Unreleased")
print("\n".join(problems) or "ok")
sys.exit(1 if problems else 0)
