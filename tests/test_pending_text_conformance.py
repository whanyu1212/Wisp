"""Cross-language conformance for bounded managed-process output retention."""

from __future__ import annotations

import json
from pathlib import Path

from wisp.tools.process_manager import _PendingText

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pending_text_conformance.json"


def test_python_pending_text_matches_shared_conformance_fixture() -> None:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))

    assert fixture["version"] == 1
    assert fixture["cases"]
    for case in fixture["cases"]:
        pending = _PendingText(max_bytes=case["max_bytes"], max_lines=case["max_lines"])
        for action_index, action in enumerate(case["actions"]):
            if action["type"] == "append":
                pending.append_bytes(
                    bytes.fromhex(action["hex"]),
                    final=action.get("final", False),
                )
                continue

            assert action["type"] == "drain"
            expected = action["expected"]
            actual = pending.drain()
            assert actual == (
                expected["text"],
                expected["dropped_bytes"],
                expected["retained_source_bytes"],
                tuple(expected["source_byte_lengths"]),
            ), f"{case['name']} action {action_index}"
