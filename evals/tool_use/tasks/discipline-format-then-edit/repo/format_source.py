from pathlib import Path

path = Path("worker.py")
path.write_text(path.read_text().replace("return 1", "return (1)"))
Path("formatted.txt").write_text("formatted\n")
