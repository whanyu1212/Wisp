from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from wisp.cli import app


def test_print_mode_outputs_response_and_writes_session(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["-p", "hello", "--session-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert result.output == "fake response to: hello\n"

    session_files = list(tmp_path.glob("*.jsonl"))
    assert len(session_files) == 1

    records = [
        json.loads(line) for line in session_files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert [record["message"]["role"] for record in records] == ["user", "assistant"]
