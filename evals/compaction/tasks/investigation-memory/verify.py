"""Pass when the tsv header bug is fixed and no other plugin changed."""

import filecmp
import subprocess
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent
problems = []
for name in ("csv", "json", "xml", "yaml", "ini"):
    module = f"plugins/{name}_format.py"
    if not filecmp.cmp(TASK / "repo" / module, module, shallow=False):
        problems.append(f"{module} changed")
# Run the fixture's own tests against the result, so added tests are allowed but
# edited or deleted ones cannot make the run pass.
original = (TASK / "repo" / "tests" / "test_formats.py").read_text(encoding="utf-8")
Path("tests/eval_original_formats.py").write_text(original, encoding="utf-8")
tests = subprocess.run(
    [sys.executable, "-m", "unittest", "-q", "tests.eval_original_formats"],
    capture_output=True,
    text=True,
)
if tests.returncode != 0:
    problems.append("the original unit tests fail:\n" + tests.stderr[-1500:])
print("\n".join(problems) or "ok")
sys.exit(1 if problems else 0)
