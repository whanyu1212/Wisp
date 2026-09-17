"""Cross-language conformance for bounded managed-process output retention."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import wisp.tools.shell.supervisor as supervisor_module
from wisp.tools.shell.supervisor import _pending_text_backends

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pending_text_conformance.json"


@pytest.mark.parametrize(
    ("backend_name", "pending_text_type"),
    _pending_text_backends(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_pending_text_matches_shared_conformance_fixture(
    backend_name: str,
    pending_text_type: type[Any],
) -> None:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))

    assert fixture["version"] == 1
    assert fixture["cases"]
    for case in fixture["cases"]:
        pending = pending_text_type(max_bytes=case["max_bytes"], max_lines=case["max_lines"])
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
            ), f"{backend_name}: {case['name']} action {action_index}"


@pytest.mark.parametrize(
    ("backend_name", "pending_text_type"),
    _pending_text_backends(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_pending_text_exposes_incremental_api(
    backend_name: str,
    pending_text_type: type[Any],
) -> None:
    pending = pending_text_type(max_bytes=100, max_lines=10)

    assert pending.max_bytes == 100, backend_name
    assert pending.max_lines == 10, backend_name
    assert pending.dropped_bytes == 0, backend_name
    assert pending.retained_source_bytes == 0, backend_name
    assert pending.has_text is False, backend_name
    assert pending.text == "", backend_name

    pending.append("a🙂")

    assert pending.text == "a🙂", backend_name
    assert pending.retained_source_bytes == 5, backend_name
    assert pending.has_text is True, backend_name


@pytest.mark.parametrize(
    ("backend_name", "pending_text_type"),
    _pending_text_backends(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_pending_text_negative_limits_drop_all(
    backend_name: str,
    pending_text_type: type[Any],
) -> None:
    pending = pending_text_type(max_bytes=-1, max_lines=10)
    pending.append_bytes(b"discard")

    assert pending.max_bytes == -1, backend_name
    assert pending.drain() == ("", 7, 0, ()), backend_name


@pytest.mark.parametrize(
    ("backend_name", "pending_text_type"),
    _pending_text_backends(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_pending_text_preserves_unbounded_python_integer_limits(
    backend_name: str,
    pending_text_type: type[Any],
) -> None:
    unlimited = 2**100
    pending = pending_text_type(max_bytes=unlimited, max_lines=unlimited)
    pending.append_bytes(b"retained")

    assert pending.max_bytes == unlimited, backend_name
    assert pending.max_lines == unlimited, backend_name
    assert pending.drain() == ("retained", 0, 8, (1,) * 8), backend_name


def test_native_loader_only_falls_back_for_absent_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_extension(_name: str) -> None:
        raise ModuleNotFoundError("No module named 'wisp._native'", name="wisp._native")

    monkeypatch.setattr(supervisor_module, "import_module", missing_extension)
    assert supervisor_module._load_native_pending_text() is None

    def broken_dependency(_name: str) -> None:
        raise ModuleNotFoundError("No module named 'broken_dependency'", name="broken_dependency")

    monkeypatch.setattr(supervisor_module, "import_module", broken_dependency)
    with pytest.raises(ModuleNotFoundError, match="broken_dependency"):
        supervisor_module._load_native_pending_text()
