"""Cross-language conformance for which live events a reader accepts.

Python clients decode events with Pydantic and the Rust frontend decodes them
against the v9 event schema. Both must accept exactly the same events, or a
backend and frontend built from different commits disagree about the wire.

The shared fixture records, for every canonical event fixture, each variant that
drops one field (at any depth) or adds an unknown one, with whether Python
accepts it. This test keeps the fixture in sync with Python;
`rust/wisp-protocol/tests/event_decoding_conformance.rs` checks the Rust decoder
reaches the same verdict for every case. Regenerate after an event-model change:

    UPDATE_EVENT_DECODING_FIXTURE=1 uv run pytest tests/rpc/test_event_decoding_conformance.py
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterator
from typing import Annotated, Any, cast

from pydantic import TypeAdapter, ValidationError
from pydantic_core import PydanticUndefined

from tests.support.paths import FIXTURES_DIR, REPO_ROOT
from wisp.events import KnownWispEventAdapter, wisp_event_from_dict
from wisp.rpc.protocol_schema import _adapter_models

_FIXTURE_PATH = FIXTURES_DIR / "event_decoding_conformance.json"
_EVENT_SCHEMA_PATH = REPO_ROOT / "schemas" / "live-rpc" / "events.schema.json"
# Not WISP_-prefixed: the test environment fixture clears every WISP_* variable.
_UPDATE_ENVIRONMENT_VARIABLE = "UPDATE_EVENT_DECODING_FIXTURE"
_UNKNOWN_FIELD = "x_wisp_unknown_field"

Path = list[str | int]


def _python_accepts(event: object) -> bool:
    try:
        wisp_event_from_dict(cast(Any, copy.deepcopy(event)))
    except ValidationError:
        return False
    return True


def _object_paths(value: object, prefix: Path) -> Iterator[Path]:
    """Yield the path of every JSON object inside ``value``, outermost first."""

    if isinstance(value, dict):
        yield prefix
        for key, member in value.items():
            yield from _object_paths(member, [*prefix, key])
    elif isinstance(value, list):
        for index, member in enumerate(value):
            yield from _object_paths(member, [*prefix, index])


def _object_at(event: object, path: Path) -> dict[str, object]:
    target = event
    for step in path:
        target = cast(Any, target)[step]
    return cast(dict[str, object], target)


def _variants(event: dict[str, object]) -> Iterator[tuple[str, Path, dict[str, object]]]:
    """Yield each single-field removal and unknown-field addition of ``event``."""

    for path in _object_paths(event, []):
        for key in _object_at(event, path):
            if not path and key == "type":
                continue  # the discriminator selects the model; it is never optional
            variant = copy.deepcopy(event)
            del _object_at(variant, path)[key]
            yield "remove", [*path, key], variant
        variant = copy.deepcopy(event)
        _object_at(variant, path)[_UNKNOWN_FIELD] = None
        yield "add_unknown", path, variant


def _expected_fixture() -> dict[str, object]:
    schema = json.loads(_EVENT_SCHEMA_PATH.read_text(encoding="utf-8"))
    canonical = cast(dict[str, dict[str, object]], schema["x-wisp-conformance-fixtures"])
    cases = [
        {
            "event": name,
            "operation": operation,
            "path": path,
            "accepted": _python_accepts(variant),
        }
        for name, event in sorted(canonical.items())
        for operation, path, variant in _variants(event)
    ]
    return {
        "version": 1,
        "unknown_field": _UNKNOWN_FIELD,
        "cases": cases,
    }


def _serialize(fixture: dict[str, object]) -> str:
    return json.dumps(fixture, indent=1, ensure_ascii=False) + "\n"


def test_event_decoding_fixture_matches_python_decoding() -> None:
    expected = _serialize(_expected_fixture())
    if os.environ.get(_UPDATE_ENVIRONMENT_VARIABLE) == "1":
        _FIXTURE_PATH.write_text(expected, encoding="utf-8")
    assert _FIXTURE_PATH.read_text(encoding="utf-8") == expected, (
        f"stale {_FIXTURE_PATH.name}; regenerate with {_UPDATE_ENVIRONMENT_VARIABLE}=1"
    )


def test_every_canonical_event_is_accepted_and_every_variant_is_covered() -> None:
    fixture = _expected_fixture()
    cases = cast(list[dict[str, object]], fixture["cases"])
    schema = json.loads(_EVENT_SCHEMA_PATH.read_text(encoding="utf-8"))
    canonical = cast(dict[str, object], schema["x-wisp-conformance-fixtures"])

    assert all(_python_accepts(event) for event in canonical.values())
    assert {case["event"] for case in cases} == set(canonical)
    # Both verdicts occur, so the fixture exercises optional and required fields.
    removals = [case["accepted"] for case in cases if case["operation"] == "remove"]
    assert any(removals) and not all(removals)


def test_every_event_field_default_satisfies_its_own_constraints() -> None:
    # Pydantic does not validate defaults, so a default its constraints reject
    # (like `methods=()` with `min_length=1`) is accepted when a field is omitted
    # while the schema, which Rust fills and then validates, rejects it.
    violations = []
    for name, model in _adapter_models(KnownWispEventAdapter).items():
        for field_name, field in model.model_fields.items():
            if field.default is PydanticUndefined and field.default_factory is None:
                continue
            default = field.get_default(call_default_factory=True, validated_data={})
            annotation = (
                Annotated[(field.annotation, *field.metadata)]
                if field.metadata
                else field.annotation
            )
            try:
                TypeAdapter(annotation).validate_python(default)
            except ValidationError:
                violations.append(f"{name}.{field_name}={default!r}")
    assert violations == []
