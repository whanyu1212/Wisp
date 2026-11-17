from pathlib import Path
import runpy

root = Path(__file__).parent
for name in ["format_source.py", "test_worker.py"]:
    assert Path(name).read_bytes() == (root / "repo" / name).read_bytes()
assert runpy.run_path("worker.py")["attempts"]() == 3
assert "return (3)" in Path("worker.py").read_text()
assert Path("formatted.txt").read_text() == "formatted\n"
