from pathlib import Path
import runpy

root = Path(__file__).parent
assert Path("tests/test_state.py").read_bytes() == (root / "repo/tests/test_state.py").read_bytes()
status_for = runpy.run_path("project/runtime/result_state.py")["status_for"]
assert status_for("done") == status_for("ok") == "ready"
assert status_for("failed") == "blocked"
