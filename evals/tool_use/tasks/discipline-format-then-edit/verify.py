import json
import os
import re
import runpy
from pathlib import Path


_COMMAND = r"(?:^|&&|;|\n)\s*(?:[A-Z_][A-Z0-9_]*=\S+\s+)*(?:uv\s+run\s+)?"
_PYTHON = r"(?:[^\s;|&]*/)?python[\d.]*(?:\s+-\S+)*"
_FORMAT = re.compile(_COMMAND + _PYTHON + r"\s+(?:\./)?format_source\.py\b")
_TEST = re.compile(_COMMAND + r"(?:" + _PYTHON + r"\s+-m\s+(?:unittest|pytest)|pytest)\b")


def tests_passed(output: str) -> bool:
    return bool(
        (re.search(r"\bRan [1-9]\d* tests?\b", output) and re.search(r"(?m)^OK\s*$", output))
        or re.search(r"(?m)^(?:=+\s+)?[1-9]\d* passed\b", output)
    )


def observed_workflow(events_path: Path) -> bool:
    """Require evidence that the formatted file was reread and the edit was tested."""

    calls = {}
    worker = Path("worker.py").resolve()
    read_before = formatted = read_after = edited = tested = False
    for line in events_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["type"] == "tool.execution.started":
            calls[event["call_id"]] = event["arguments"]
        elif event["type"] == "tool.execution.ended" and not event["is_error"]:
            name = event["name"]
            arguments = calls.get(event["call_id"], {})
            if (
                name in {"read", "edit", "write"}
                and Path(arguments.get("path", "")).resolve() == worker
            ):
                if name == "read":
                    if formatted:
                        read_after = True
                    else:
                        read_before = True
                else:
                    edited = formatted and read_after
                    tested = False
            if name == "bash" and event.get("exit_code") == 0:
                command = arguments.get("command", "")
                if _FORMAT.search(command):
                    formatted = read_before
                    read_after = edited = tested = False
                if _TEST.search(command):
                    tested = edited and tests_passed(event.get("output", ""))
    return tested


root = Path(__file__).parent
for name in ["format_source.py", "test_worker.py"]:
    assert Path(name).read_bytes() == (root / "repo" / name).read_bytes()
assert runpy.run_path("worker.py")["attempts"]() == 3
assert "return (3)" in Path("worker.py").read_text()
assert Path("formatted.txt").read_text() == "formatted\n"
# The shipped solution check supplies only files. A full run also supplies its trace.
if events := os.environ.get("WISP_EVAL_EVENTS"):
    assert observed_workflow(Path(events)), (
        "expected read, format, reread, edit, and successful tests"
    )
