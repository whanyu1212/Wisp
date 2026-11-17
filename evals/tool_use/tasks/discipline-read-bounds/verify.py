from pathlib import Path
import runpy

root = Path(__file__).parent
assert Path("test_status.py").read_bytes() == (root / "repo/test_status.py").read_bytes()
normalize = runpy.run_path("status.py")["normalize"]
assert normalize(" Done ") == "done"
assert normalize("") == ""
