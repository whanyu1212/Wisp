"""Conformance traces for the TUI frontend state machine (#459)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError

from wisp.events import (
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
)
from wisp.tui.trace_runner import RecordingTraceRenderer, TraceReplayError, load_trace, run_trace
from wisp.tui.trace_schema import (
    DEFAULT_TRACE_SCHEMA_DIRECTORY,
    TraceFileAdapter,
    generate_trace_artifacts,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = _REPO_ROOT / "tests/fixtures/tui_traces"
SCHEMA_PATH = _REPO_ROOT / "schemas/tui-traces/v1/trace.schema.json"
_TRACE_SCHEMA_DIR = _REPO_ROOT / DEFAULT_TRACE_SCHEMA_DIRECTORY


def _all_trace_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def test_trace_schema_is_current() -> None:
    """Generated schema artifacts must be up to date."""
    artifacts = generate_trace_artifacts()
    for filename, expected_content in artifacts.items():
        path = _TRACE_SCHEMA_DIR / filename
        assert path.exists(), f"missing generated trace artifact: {filename}"
        actual = path.read_text(encoding="utf-8")
        assert actual == expected_content, f"stale generated trace artifact: {filename}"


def test_trace_schema_is_valid_json_schema() -> None:
    assert SCHEMA_PATH.exists()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


def test_initial_view_schema_excludes_derived_state() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    initial = _default_initial()
    initial["view"] = {"input_ready": True, "mode": "build", "provider": "other"}
    data = _inline_trace(
        "derived_initial_view_field",
        [{"type": "local.submit", "content": "hello", "clock_ms": 0}],
        initial,
    )
    with pytest.raises(JsonSchemaValidationError, match="provider"):
        Draft202012Validator(schema).validate(data)


def test_trace_schema_rejects_out_of_range_tool_exit_codes() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = _inline_trace(
        "out_of_range_exit_code",
        [
            {
                "type": "rpc.event",
                "event": {
                    "type": "tool.result",
                    "call_id": "call-1",
                    "name": "read",
                    "output": "",
                    "is_error": False,
                    "exit_code": 2**63,
                },
                "clock_ms": 0,
            }
        ],
        _default_initial(),
    )

    with pytest.raises(JsonSchemaValidationError, match="not valid"):
        Draft202012Validator(schema).validate(data)
    with pytest.raises(ValueError, match="signed 64-bit"):
        TraceFileAdapter.validate_python(data)


def test_trace_schema_rejects_coerced_tool_booleans() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = _inline_trace(
        "coerced_tool_boolean",
        [
            {
                "type": "rpc.event",
                "event": {
                    "type": "tool.result",
                    "call_id": "call-1",
                    "name": "read",
                    "output": "",
                    "is_error": 0,
                },
                "clock_ms": 0,
            }
        ],
        _default_initial(),
    )

    with pytest.raises(JsonSchemaValidationError, match="not valid"):
        Draft202012Validator(schema).validate(data)
    with pytest.raises(ValueError, match="trace boolean"):
        TraceFileAdapter.validate_python(data)


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "queue.updated",
            "schema_version": 34,
            "steering_mode": "invalid",
        },
        {
            "type": "queue.updated",
            "schema_version": 34,
            "timestamp": "not-a-timestamp",
        },
        {
            "type": "queue.items.removed",
            "schema_version": 34,
            "timestamp": "not-a-timestamp",
            "command_id": "clear-1",
            "operation": "clear",
        },
        {
            "type": "queue.message.injected",
            "schema_version": 34,
            "timestamp": "not-a-timestamp",
            "kind": "steering",
            "content": "steer",
        },
        {
            "type": "queue.updated",
            "schema_version": 34,
            "timestamp": 0,
        },
        {
            "type": "queue.items.removed",
            "schema_version": 34,
            "timestamp": 0,
            "command_id": "clear-1",
            "operation": "clear",
        },
        {
            "type": "queue.message.injected",
            "schema_version": 34,
            "timestamp": 0,
            "kind": "steering",
            "content": "steer",
        },
        {
            "type": "queue.items.removed",
            "schema_version": 34,
            "command_id": "pop-1",
            "operation": "pop",
        },
        {
            "type": "queue.items.removed",
            "schema_version": 34,
            "command_id": "pop-1",
            "operation": "pop",
            "kind": "steering",
            "follow_up": ["wrong queue"],
        },
        {
            "type": "queue.message.injected",
            "schema_version": 34,
            "kind": "steering",
            "content": "expanded",
            "skill_invocation": {"original_content": "/skill request"},
        },
        {
            "type": "queue.message.injected",
            "schema_version": 34,
            "kind": "steering",
            "content": "expanded",
            "skill_invocation": {
                "name": "skill",
                "original_content": "/skill request",
                "request": "request",
                "content_sha256": "0" * 64,
                "instructions_truncated": 0,
            },
        },
        {
            "type": "queue.message.injected",
            "schema_version": 34,
            "kind": "steering",
            "content": "expanded",
            "skill_invocation": {
                "name": "skill",
                "original_content": "/skill request",
                "request": "request",
                "content_sha256": "not-a-sha",
                "unexpected": True,
            },
        },
    ],
)
def test_trace_schema_and_model_reject_invalid_queue_events(event: dict[str, Any]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = _inline_trace(
        "invalid_queue_event",
        [{"type": "rpc.event", "event": event, "clock_ms": 0}],
        _default_initial(),
    )

    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema).validate(data)
    with pytest.raises(ValueError):
        TraceFileAdapter.validate_python(data)


@pytest.mark.parametrize("kind", [pytest.param("omitted"), pytest.param(None)])
def test_trace_schema_accepts_clear_all_queue_events_without_a_kind(kind: str | None) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    event: dict[str, Any] = {
        "type": "queue.items.removed",
        "schema_version": 34,
        "command_id": "clear-1",
        "operation": "clear",
        "steering": ["steer"],
        "follow_up": ["later"],
    }
    if kind is None:
        event["kind"] = None
    data = _inline_trace(
        "clear_all_queue_event",
        [{"type": "rpc.event", "event": event, "clock_ms": 0}],
        _default_initial(),
    )

    Draft202012Validator(schema).validate(data)
    TraceFileAdapter.validate_python(data)


def test_trace_schema_keeps_unknown_rpc_events_generic() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = _inline_trace(
        "unknown_rpc_event",
        [
            {
                "type": "rpc.event",
                "event": {"type": "future.queue.event", "payload": {"value": True}},
                "clock_ms": 0,
            }
        ],
        _default_initial(),
    )

    Draft202012Validator(schema).validate(data)
    trace = TraceFileAdapter.validate_python(data)

    async def replay() -> None:
        result = await run_trace(trace)
        assert result.commands == ()

    anyio.run(replay)


def test_trace_schema_rejects_integers_beyond_the_finite_json_number_range() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = _inline_trace(
        "oversized_generic_integer",
        [
            {
                "type": "rpc.event",
                "event": {
                    "type": "tool.call",
                    "call_id": "call-1",
                    "name": "extension",
                    "arguments": {"value": 10**400},
                },
                "clock_ms": 0,
            }
        ],
        _default_initial(),
    )

    with pytest.raises(JsonSchemaValidationError, match="not valid"):
        Draft202012Validator(schema).validate(data)
    with pytest.raises(ValueError, match="finite JSON number range"):
        TraceFileAdapter.validate_python(data)


def test_trace_model_rejects_integral_float_exit_codes_before_event_coercion() -> None:
    data = _inline_trace(
        "float_exit_code",
        [
            {
                "type": "rpc.event",
                "event": {
                    "type": "tool.result",
                    "call_id": "call-1",
                    "name": "bash",
                    "output": "",
                    "is_error": False,
                    "exit_code": 1.0,
                },
                "clock_ms": 0,
            }
        ],
        _default_initial(),
    )

    with pytest.raises(ValueError, match="signed 64-bit integer"):
        TraceFileAdapter.validate_python(data)


def test_trace_schema_rejects_out_of_range_dropped_byte_counts() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = _inline_trace(
        "out_of_range_dropped_bytes",
        [
            {
                "type": "rpc.event",
                "event": {
                    "type": "tool.result",
                    "call_id": "call-1",
                    "name": "bash",
                    "output": "",
                    "is_error": False,
                    "stdout_dropped_bytes": 2**64,
                },
                "clock_ms": 0,
            }
        ],
        _default_initial(),
    )

    with pytest.raises(JsonSchemaValidationError, match="not valid"):
        Draft202012Validator(schema).validate(data)
    with pytest.raises(ValueError, match="unsigned 64-bit"):
        TraceFileAdapter.validate_python(data)


@pytest.mark.parametrize("path", _all_trace_paths(), ids=lambda p: p.name)
def test_trace_fixture_validates_against_schema(path: Path) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(data)
    # Also validate via Pydantic strict model for cross-field invariants.
    TraceFileAdapter.validate_python(data)


@pytest.mark.parametrize("path", _all_trace_paths(), ids=lambda p: p.name)
def test_trace_fixtures_are_bounded_and_safe(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = path.read_text(encoding="utf-8")
    # Bounded size: each fixture reviewable.
    assert len(raw) < 8192, f"fixture too large: {path.name} {len(raw)} bytes"

    text = json.dumps(data)
    # No credentials or machine paths.
    assert "sk-" not in text.lower(), "fixture may contain credential"
    assert "/Users/" not in text, "fixture contains absolute host path"
    assert "/home/" not in text, "fixture contains absolute host path"
    # No unstable identifiers: uuid hex is 32 chars; our ids are short deterministic.
    assert "prompt-" in text or "approval-" in text or "get_session_stats-" in text

    trace = TraceFileAdapter.validate_python(data)
    assert trace.name == path.stem, "trace name must match filename"
    assert 1 <= len(trace.inputs) <= 64
    assert 1 <= len(trace.expected.commands) <= 32


@pytest.mark.parametrize("path", _all_trace_paths(), ids=lambda p: p.name)
def test_trace_replay_matches_expected_projection(path: Path) -> None:
    async def run() -> None:
        trace = load_trace(path)
        result = await run_trace(trace)

        # Compare every expected payload field, not just (type, id): actual
        # commands may carry more detail, but each expected key/value pair must
        # match exactly so wrong call_id/scope/reason/prompt regressions fail.
        raw = json.loads(path.read_text(encoding="utf-8"))
        expected_commands: list[dict[str, object]] = list(raw["expected"]["commands"])
        fail_details = _diff_commands(result.commands, expected_commands)
        if fail_details:
            pytest.fail(f"command mismatch in {path.name}\n{fail_details}")

        assert result.view == trace.expected.view, (
            f"view mismatch in {path.name}\n"
            f"  actual:   {result.view.model_dump()}\n"
            f"  expected: {trace.expected.view.model_dump()}"
        )
        assert result.interaction == trace.expected.interaction, (
            f"interaction mismatch in {path.name}\n"
            f"  actual:   {result.interaction.model_dump()}\n"
            f"  expected: {trace.expected.interaction.model_dump()}"
        )
        assert result.retained_text == trace.expected.retained_text, (
            f"retained_text mismatch in {path.name}\n"
            f"  actual:   {result.retained_text!r}\n"
            f"  expected: {trace.expected.retained_text!r}"
        )
        assert result.restored_drafts == trace.expected.restored_drafts, (
            f"restored_drafts mismatch in {path.name}\n"
            f"  actual:   {result.restored_drafts!r}\n"
            f"  expected: {trace.expected.restored_drafts!r}"
        )
        if trace.expected.tool_cards is not None:
            assert result.tool_cards == trace.expected.tool_cards, (
                f"tool_cards mismatch in {path.name}\n"
                f"  actual:   {[card.model_dump() for card in result.tool_cards]!r}\n"
                f"  expected: {[card.model_dump() for card in trace.expected.tool_cards]!r}"
            )

    anyio.run(run)


def _diff_commands(actual: tuple[dict[str, Any], ...], expected: list[dict[str, object]]) -> str:
    """Return a structural diff string when outbound commands diverge."""

    if len(actual) != len(expected):
        return (
            f"  count differs:\n"
            f"    actual ({len(actual)}):   {[(c.get('type'), c.get('id')) for c in actual]}\n"
            f"    expected ({len(expected)}): "
            f"{[(c.get('type'), c.get('id')) for c in expected]}"
        )
    lines: list[str] = []
    for index, expected_command in enumerate(expected):
        actual_command = actual[index]
        for key, value in expected_command.items():
            if key not in actual_command:
                lines.append(
                    f"  command[{index}] {actual_command.get('type')} "
                    f"missing expected field {key!r}"
                )
            elif actual_command[key] != value:
                lines.append(
                    f"  command[{index}] {actual_command.get('type')} field {key!r}: "
                    f"actual {actual_command[key]!r} != expected {value!r}"
                )
    return "\n".join(lines)


@pytest.mark.parametrize("path", _all_trace_paths(), ids=lambda p: p.name)
def test_trace_replay_is_deterministic(path: Path) -> None:
    async def run_twice() -> None:
        trace = load_trace(path)
        r1 = await run_trace(trace)
        r2 = await run_trace(trace)
        assert r1.commands == r2.commands
        assert r1.view == r2.view
        assert r1.interaction == r2.interaction
        assert r1.retained_text == r2.retained_text
        assert r1.restored_drafts == r2.restored_drafts
        assert r1.tokens == r2.tokens

    anyio.run(run_twice)


def test_command_diff_distinguishes_missing_fields_from_null() -> None:
    diff = _diff_commands(({"type": "approval"},), [{"type": "approval", "reason": None}])
    assert "missing expected field 'reason'" in diff


def test_trace_replay_determinism_across_repeated_runs() -> None:
    """Run the same trace many times to catch nondeterministic id/clock leakage."""
    path = FIXTURE_DIR / "prompt_stream_completion.json"

    async def many() -> None:
        trace = load_trace(path)
        first = await run_trace(trace)
        for _ in range(19):
            result = await run_trace(trace)
            assert result.commands == first.commands
            assert result.view == first.view
            assert result.restored_drafts == first.restored_drafts

    anyio.run(many)


def test_clock_advancement_is_monotonic() -> None:
    from wisp.tui.trace_runner import DeterministicClock

    clock = DeterministicClock()
    clock.advance_to(10)
    clock.advance_to(20)
    with pytest.raises(ValueError, match="cannot go backwards"):
        clock.advance_to(5)


def test_id_factory_is_deterministic_per_prefix() -> None:
    from wisp.tui.trace_runner import DeterministicIdFactory

    factory = DeterministicIdFactory()
    assert factory.next("prompt") == "prompt-1"
    assert factory.next("prompt") == "prompt-2"
    assert factory.next("approval") == "approval-1"
    assert factory.next("prompt") == "prompt-3"


def _inline_trace(
    name: str, inputs: list[dict[str, Any]], initial: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": name,
        "description": f"synthetic trace for {name}",
        "initial": initial,
        "inputs": inputs,
        "expected": {
            "commands": [],
            "view": {
                "status": "idle",
                "input_mode": "idle",
                "input_ready": True,
                "queued_steering": 0,
                "queued_follow_ups": 0,
                "provider": "fake",
                "model": None,
                "mode": "build",
                "last_session": None,
            },
            "interaction": {
                "status": "idle",
                "current_command_id": None,
                "current_command_type": None,
                "pending_approval_call_id": None,
                "pending_trust_request_id": None,
                "cancel_requested": False,
                "exit_requested": False,
            },
            "retained_text": None,
        },
    }


def _default_initial() -> dict[str, Any]:
    return {"provider": "fake", "model": None, "effort": None, "view": None, "interaction": None}


def test_unprefixed_slash_command_is_normalized_before_parsing() -> None:
    async def run() -> None:
        data = _inline_trace(
            "slash_normalization",
            [{"type": "local.slash", "command": "mcp", "args": [], "clock_ms": 0}],
            _default_initial(),
        )
        trace = TraceFileAdapter.validate_python(data)
        result = await run_trace(trace)

        command_types = [command["type"] for command in result.commands]
        assert command_types == ["get_mcp_status"], (
            f"unprefixed slash must dispatch the slash command, got {command_types}"
        )

    anyio.run(run)


def test_slash_arguments_keep_token_boundaries() -> None:
    async def run() -> None:
        spaced_argument = "sess id with spaces"
        data = _inline_trace(
            "slash_arg_boundaries",
            [
                {
                    "type": "local.slash",
                    "command": "resume",
                    "args": [spaced_argument],
                    "clock_ms": 0,
                }
            ],
            _default_initial(),
        )
        trace = TraceFileAdapter.validate_python(data)
        result = await run_trace(trace)

        selections = [command for command in result.commands if command["type"] == "select_session"]
        assert len(selections) == 1, (
            f"one quoted argument must stay one token, got {result.commands}"
        )
        assert selections[0]["session_id"] == spaced_argument

    anyio.run(run)


def test_slash_argument_vectors_are_bounded() -> None:
    too_many = _inline_trace(
        "slash_arg_limit",
        [
            {
                "type": "local.slash",
                "command": "resume",
                "args": [f"arg-{index}" for index in range(9)],
                "clock_ms": 0,
            }
        ],
        _default_initial(),
    )
    with pytest.raises(ValueError, match="inputs.0.*args"):
        TraceFileAdapter.validate_python(too_many)


@pytest.mark.parametrize("input_type", ["local.steer", "local.follow_up"])
def test_local_queue_content_is_bounded(input_type: str) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = _inline_trace(
        "queue_content_limit",
        [{"type": input_type, "content": "x" * 4001, "clock_ms": 0}],
        _default_initial(),
    )

    with pytest.raises(JsonSchemaValidationError, match="not valid"):
        Draft202012Validator(schema).validate(data)
    with pytest.raises(ValueError, match="inputs.0.*content"):
        TraceFileAdapter.validate_python(data)


def test_restored_drafts_are_bounded() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = _inline_trace(
        "restored_draft_limit",
        [{"type": "local.restore_queue", "clock_ms": 0}],
        _default_initial(),
    )
    data["expected"]["restored_drafts"] = ["x" * 4001]

    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema).validate(data)
    with pytest.raises(ValueError, match="expected.restored_drafts"):
        TraceFileAdapter.validate_python(data)


def test_replay_stops_at_exit_and_rejects_trailing_inputs() -> None:
    async def run() -> None:
        data = _inline_trace(
            "trailing_after_exit",
            [
                {"type": "local.submit", "content": "one", "clock_ms": 0},
                {"type": "rpc.closed", "error": None, "clock_ms": 10},
                {"type": "local.submit", "content": "two", "clock_ms": 20},
            ],
            _default_initial(),
        )
        trace = TraceFileAdapter.validate_python(data)
        with pytest.raises(TraceReplayError, match="trailing input"):
            await run_trace(trace)

    anyio.run(run)


def test_initial_pending_requests_are_seeded_and_resolvable() -> None:
    async def run() -> None:
        initial = _default_initial()
        initial["interaction"] = {
            "status": "waiting_for_approval",
            "current_command_id": "prompt-1",
            "current_command_type": "prompt",
            "pending_approval_call_id": "call-seed",
            "pending_trust_request_id": None,
            "cancel_requested": False,
            "exit_requested": False,
        }
        data = _inline_trace(
            "seeded_pending",
            [
                {
                    "type": "local.approve",
                    "call_id": "call-seed",
                    "approved": False,
                    "clock_ms": 0,
                }
            ],
            initial,
        )
        trace = TraceFileAdapter.validate_python(data)
        result = await run_trace(trace)

        approvals = [command for command in result.commands if command["type"] == "approval"]
        assert len(approvals) == 1
        assert approvals[0]["call_id"] == "call-seed"
        assert approvals[0]["approved"] is False

    anyio.run(run)


def test_invalid_initial_interaction_status_is_rejected() -> None:
    initial = _default_initial()
    initial["interaction"] = {
        "status": "paused",
        "current_command_id": None,
        "current_command_type": None,
        "pending_approval_call_id": None,
        "pending_trust_request_id": None,
        "cancel_requested": False,
        "exit_requested": False,
    }
    data = _inline_trace(
        "invalid_interaction_status",
        [{"type": "local.submit", "content": "hello", "clock_ms": 0}],
        initial,
    )
    with pytest.raises(ValueError, match="interaction.status"):
        TraceFileAdapter.validate_python(data)


def test_invalid_initial_command_type_is_rejected() -> None:
    initial = _default_initial()
    initial["interaction"] = {
        "status": "running",
        "current_command_id": "prompt-1",
        "current_command_type": "submit",
        "pending_approval_call_id": None,
        "pending_trust_request_id": None,
        "cancel_requested": False,
        "exit_requested": False,
    }
    data = _inline_trace(
        "invalid_command_type",
        [{"type": "local.submit", "content": "hello", "clock_ms": 0}],
        initial,
    )
    with pytest.raises(ValueError, match="current_command_type"):
        TraceFileAdapter.validate_python(data)


def test_interaction_status_is_preserved_when_initial_view_is_omitted() -> None:
    async def run() -> None:
        initial = _default_initial()
        initial["interaction"] = {
            "status": "running",
            "current_command_id": "prompt-1",
            "current_command_type": "prompt",
            "pending_approval_call_id": None,
            "pending_trust_request_id": None,
            "cancel_requested": False,
            "exit_requested": False,
        }
        data = _inline_trace(
            "interaction_without_view",
            [{"type": "local.slash", "command": "help", "args": [], "clock_ms": 0}],
            initial,
        )
        trace = TraceFileAdapter.validate_python(data)
        result = await run_trace(trace)
        assert result.interaction.status == "running"

    anyio.run(run)


def test_configure_recorder_captures_every_argument() -> None:
    from wisp.tui.trace_runner import DeterministicIdFactory, TraceController

    async def run() -> None:
        controller = TraceController(DeterministicIdFactory())
        await controller.configure(
            provider="fake-provider",
            model="fake-model",
            effort="high",
            clear_effort=True,
            auto_compaction_enabled=False,
            mode="plan",
        )
        assert controller.commands == [
            {
                "type": "configure",
                "id": "configure-1",
                "provider": "fake-provider",
                "model": "fake-model",
                "effort": "high",
                "clear_effort": True,
                "auto_compaction_enabled": False,
                "mode": "plan",
            }
        ]

    anyio.run(run)


def test_get_messages_recorder_captures_every_argument() -> None:
    from wisp.tui.trace_runner import DeterministicIdFactory, TraceController

    async def run() -> None:
        controller = TraceController(DeterministicIdFactory())
        await controller.get_messages(
            session_id="session-1",
            limit=1,
            before_entry_id="before-1",
            entry_ids=("entry-1",),
            complete_structure=True,
            full_content=True,
            allow_during_prompt=True,
        )
        assert controller.commands == [
            {
                "type": "get_messages",
                "id": "get_messages-1",
                "session_id": "session-1",
                "limit": 1,
                "before_entry_id": "before-1",
                "after_entry_id": None,
                "entry_ids": ["entry-1"],
                "complete_structure": True,
                "full_content": True,
                "allow_during_prompt": True,
            }
        ]

    anyio.run(run)


def test_completed_response_without_deltas_is_retained() -> None:
    async def run() -> None:
        data = _inline_trace(
            "completed_without_deltas",
            [
                {"type": "local.submit", "content": "hello", "clock_ms": 0},
                {
                    "type": "rpc.event",
                    "event": {
                        "type": "message.completed",
                        "turn": 1,
                        "content": "complete response",
                        "finish_reason": "stop",
                    },
                    "clock_ms": 10,
                },
            ],
            _default_initial(),
        )
        trace = TraceFileAdapter.validate_python(data)
        result = await run_trace(trace)
        assert result.tokens == ()
        assert result.retained_text == "complete response"

    anyio.run(run)


def test_completed_response_content_overrides_streamed_deltas() -> None:
    async def run() -> None:
        data = _inline_trace(
            "completed_reconciles_deltas",
            [
                {"type": "local.submit", "content": "hello", "clock_ms": 0},
                {
                    "type": "rpc.event",
                    "event": {"type": "message.delta", "turn": 1, "delta": "partial"},
                    "clock_ms": 5,
                },
                {
                    "type": "rpc.event",
                    "event": {
                        "type": "message.completed",
                        "turn": 1,
                        "content": "authoritative response",
                        "finish_reason": "stop",
                    },
                    "clock_ms": 10,
                },
            ],
            _default_initial(),
        )
        trace = TraceFileAdapter.validate_python(data)
        result = await run_trace(trace)
        assert result.tokens == ("partial",)
        assert result.retained_text == "authoritative response"

    anyio.run(run)


def test_later_partial_response_overrides_older_completion() -> None:
    async def run() -> None:
        data = _inline_trace(
            "partial_after_completion",
            [
                {
                    "type": "rpc.event",
                    "event": {
                        "type": "message.completed",
                        "turn": 1,
                        "content": "older response",
                        "finish_reason": "stop",
                    },
                    "clock_ms": 0,
                },
                {
                    "type": "rpc.event",
                    "event": {"type": "message.delta", "turn": 2, "delta": "new partial"},
                    "clock_ms": 5,
                },
                {"type": "rpc.closed", "error": "stream lost", "clock_ms": 10},
            ],
            _default_initial(),
        )
        trace = TraceFileAdapter.validate_python(data)
        result = await run_trace(trace)
        assert result.retained_text == "new partial"

    anyio.run(run)


def test_finalized_partial_stream_does_not_absorb_later_response() -> None:
    renderer = RecordingTraceRenderer()
    renderer.token_delta("first partial")
    renderer.end_token_stream()
    renderer.token_delta("second response")
    assert renderer.retained_text == "second response"


def test_partial_response_retention_is_bounded_across_deltas() -> None:
    renderer = RecordingTraceRenderer()
    renderer.token_delta("x" * 3000)
    with pytest.raises(TraceReplayError, match="retained characters"):
        renderer.token_delta("y" * 1001)


def test_denied_process_result_does_not_create_a_generic_trace_card() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="poll-denied",
            name="bash",
            arguments={"operation": "poll", "process_id": "process-1"},
        )
    )
    renderer.event(
        ToolApprovalResolved(
            call_id="poll-denied",
            name="bash",
            approved=False,
            reason="denied",
        )
    )
    renderer.event(
        ToolResultReady(
            call_id="poll-denied",
            name="bash",
            output="Denied by user",
            is_error=True,
        )
    )

    assert renderer.tool_card_projection() == ()


def test_trace_tool_card_projection_bounds_display_identities() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="c" * 256,
            name="n" * 256,
            arguments={},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert len(card.call_id) <= 128
    assert len(card.name) == 128
    assert card.call_id == f"h-{hashlib.sha256(('c' * 256).encode()).hexdigest()}"
    assert card.name == f"{'n' * 127}…"

    for unsafe in ("bad/id", ""):
        renderer = RecordingTraceRenderer()
        renderer.event(ToolCallRequested(call_id=unsafe, name="read", arguments={}))
        (card,) = renderer.tool_card_projection()
        assert card.call_id == f"h-{hashlib.sha256(unsafe.encode()).hexdigest()}"


def test_settlement_tombstones_unresolved_process_call_ids() -> None:
    renderer = RecordingTraceRenderer()
    process_call = ToolCallRequested(
        call_id="settled-process",
        name="bash",
        arguments={"operation": "poll", "process_id": "process-1"},
    )
    renderer.event(process_call)
    renderer._settle_tool_cards()
    renderer.event(process_call)

    (card,) = renderer.tool_card_projection()
    assert card.status == "cancelled"
    assert card.arguments_available


def test_reused_process_call_id_projects_an_ambiguity_card() -> None:
    renderer = RecordingTraceRenderer()
    process_call = ToolCallRequested(
        call_id="poll-reused",
        name="bash",
        arguments={"operation": "poll", "process_id": "process-1"},
    )
    renderer.event(process_call)
    renderer.event(
        ToolResultReady(
            call_id="poll-reused",
            name="bash",
            output="running",
            is_error=False,
            process_id="process-1",
            process_state="running",
        )
    )
    renderer.rpc_stream_ended_unexpectedly()
    renderer.event(process_call)

    (card,) = renderer.tool_card_projection()
    assert card.call_id == "poll-reused"
    assert card.name == "bash"
    assert card.status == "cancelled"
    assert card.arguments_available


def test_duplicate_metadata_is_compared_after_presentation_bounds() -> None:
    renderer = RecordingTraceRenderer()
    shared_name = "n" * 127
    renderer.event(
        ToolCallRequested(
            call_id="bounded-name",
            name=f"{shared_name}aa",
            arguments={"value": "x" * 64 + "a"},
        )
    )
    renderer.event(
        ToolCallRequested(
            call_id="bounded-name",
            name=f"{shared_name}ab",
            arguments={"value": "x" * 64 + "b"},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "requested"
    assert card.name == f"{'n' * 127}…"


def test_unresolved_metadata_conflict_survives_lifecycle_starts() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(ToolCallRequested(call_id="conflict", name="read", arguments={"path": "first"}))
    renderer.event(ToolCallRequested(call_id="conflict", name="read", arguments={"path": "second"}))
    renderer.event(ToolCallRequested(call_id="conflict", name="read", arguments={"path": "second"}))
    renderer.approval_request(
        ToolApprovalRequested(
            call_id="conflict",
            name="read",
            arguments={"path": "second"},
            safety="read",
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "error"

    renderer.event(ToolResultReady(call_id="conflict", name="read", output="late", is_error=True))
    renderer.event(ToolCallRequested(call_id="conflict", name="read", arguments={"path": "third"}))
    original, reuse = renderer.tool_card_projection()
    assert original.status == "error"
    assert reuse.status == "cancelled"


def test_out_of_range_integer_metadata_matches_rust_number_rounding() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="large-number",
            name="extension",
            arguments={"value": 2**64},
        )
    )
    renderer.event(
        ToolCallRequested(
            call_id="large-number",
            name="extension",
            arguments={"value": 2**64 + 1},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "requested"


def test_blank_process_ids_share_canonical_generic_metadata() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="blank-process",
            name="bash",
            arguments={"operation": "poll", "process_id": ""},
        )
    )
    renderer.event(
        ToolCallRequested(
            call_id="blank-process",
            name="bash",
            arguments={"operation": "poll", "process_id": " "},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "requested"


def test_control_separator_process_id_uses_rust_whitespace_semantics() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="control-process",
            name="bash",
            arguments={"operation": "poll", "process_id": "\u001c\u001d\u001e\u001f"},
        )
    )

    assert renderer.tool_card_projection() == ()


def test_ignored_denial_preserves_unresolved_metadata_conflict() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(call_id="denied-conflict", name="read", arguments={"path": "a"})
    )
    renderer.event(
        ToolCallRequested(call_id="denied-conflict", name="read", arguments={"path": "b"})
    )
    renderer.event(
        ToolApprovalResolved(
            call_id="denied-conflict", name="read", approved=False, reason="policy"
        )
    )
    renderer.event(
        ToolCallRequested(call_id="denied-conflict", name="read", arguments={"path": "c"})
    )

    (conflict,) = renderer.tool_card_projection()
    assert conflict.status == "error"


def test_generic_to_process_conflict_remains_unresolved_across_starts() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(ToolCallRequested(call_id="cross-kind", name="read", arguments={"path": "a"}))
    renderer.event(
        ToolCallRequested(
            call_id="cross-kind",
            name="bash",
            arguments={"operation": "poll", "process_id": "process"},
        )
    )
    renderer.event(ToolCallRequested(call_id="cross-kind", name="read", arguments={"path": "b"}))

    (card,) = renderer.tool_card_projection()
    assert card.status == "error"


def test_action_summaries_use_rust_whitespace_semantics() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="separator",
            name="extension",
            arguments={"value": "left\u001cright"},
        )
    )
    renderer.event(
        ToolCallRequested(
            call_id="separator",
            name="extension",
            arguments={"value": "left right"},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "error"


def test_generic_float_metadata_uses_rust_exponent_formatting() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="float-format",
            name="extension",
            arguments={"value": 1e-7},
        )
    )
    renderer.event(
        ToolCallRequested(
            call_id="float-format",
            name="extension",
            arguments={"value": "1e-07"},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "error"


def test_generic_float_metadata_uses_rust_fixed_decimal_threshold() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="fixed-float-format",
            name="extension",
            arguments={"value": 1e-5},
        )
    )
    renderer.event(
        ToolCallRequested(
            call_id="fixed-float-format",
            name="extension",
            arguments={"value": "0.00001"},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "requested"


def test_clipped_generic_key_collisions_count_as_omissions() -> None:
    renderer = RecordingTraceRenderer()
    prefix = "k" * 64
    renderer.event(
        ToolCallRequested(
            call_id="key-collision",
            name="extension",
            arguments={f"{prefix}a": 1, f"{prefix}b": 2},
        )
    )
    renderer.event(
        ToolCallRequested(
            call_id="key-collision",
            name="extension",
            arguments={f"{prefix}b": 2},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "error"


def test_generic_argument_omission_count_affects_canonical_metadata() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="omitted-fields",
            name="extension",
            arguments={f"key-{index}": index for index in range(9)},
        )
    )
    renderer.event(
        ToolCallRequested(
            call_id="omitted-fields",
            name="extension",
            arguments={f"key-{index}": index for index in range(10)},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "error"


def test_duplicate_metadata_uses_the_rendered_action_summary() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="timing-only",
            name="bash",
            arguments={"command": "echo ok", "wait_seconds": 1},
        )
    )
    renderer.event(
        ToolCallRequested(
            call_id="timing-only",
            name="bash",
            arguments={"command": "echo ok", "wait_seconds": 99},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "requested"


def test_delayed_duplicate_approval_request_does_not_regress_running_card() -> None:
    renderer = RecordingTraceRenderer()
    request = ToolApprovalRequested(
        call_id="approved",
        name="read",
        arguments={},
        safety="read",
    )
    renderer.approval_request(request)
    renderer.event(ToolApprovalResolved(call_id="approved", name="read", approved=True))
    renderer.approval_request(request)

    (card,) = renderer.tool_card_projection()
    assert card.status == "running"


def test_duplicate_denial_after_approval_does_not_suppress_result() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(ToolCallRequested(call_id="approved", name="read", arguments={}))
    renderer.event(ToolApprovalResolved(call_id="approved", name="read", approved=True))
    renderer.event(ToolApprovalResolved(call_id="approved", name="read", approved=False))
    renderer.event(ToolResultReady(call_id="approved", name="read", output="done", is_error=False))

    (card,) = renderer.tool_card_projection()
    assert card.status == "done"


def test_approval_request_reusing_terminal_call_id_is_a_conflict() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(ToolCallRequested(call_id="reused", name="read", arguments={}))
    renderer.event(ToolResultReady(call_id="reused", name="read", output="done", is_error=False))
    renderer.approval_request(
        ToolApprovalRequested(
            call_id="reused",
            name="read",
            arguments={},
            safety="read",
        )
    )

    first, conflict = renderer.tool_card_projection()
    assert first.status == "done"
    assert conflict.status == "cancelled"


def test_requestless_approval_resolution_does_not_create_a_card() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(ToolApprovalResolved(call_id="orphan", name="read", approved=True))

    assert renderer.tool_card_projection() == ()


def test_result_and_approval_updates_preserve_the_requested_name() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(ToolCallRequested(call_id="name-result", name="read", arguments={}))
    renderer.event(
        ToolResultReady(call_id="name-result", name="grep", output="done", is_error=False)
    )
    renderer.event(ToolCallRequested(call_id="name-approval", name="read", arguments={}))
    renderer.event(ToolApprovalResolved(call_id="name-approval", name="grep", approved=True))

    result, approval = renderer.tool_card_projection()
    assert result.name == "read"
    assert approval.name == "read"


def test_generic_approval_reusing_resolved_process_id_is_cancelled() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="resolved-process",
            name="bash",
            arguments={"operation": "poll", "process_id": "process-1"},
        )
    )
    renderer.event(
        ToolResultReady(
            call_id="resolved-process",
            name="bash",
            output="running",
            is_error=False,
            process_id="process-1",
            process_state="running",
        )
    )
    renderer.approval_request(
        ToolApprovalRequested(
            call_id="resolved-process",
            name="read",
            arguments={"path": "README.md"},
            safety="read",
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.status == "cancelled"
    assert card.name == "read"


def test_conflicting_unresolved_tool_calls_project_an_error() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(ToolCallRequested(call_id="call-conflict", name="read", arguments={"path": "a"}))
    renderer.event(ToolCallRequested(call_id="call-conflict", name="grep", arguments={"path": "b"}))

    (card,) = renderer.tool_card_projection()
    assert card.call_id == "call-conflict"
    assert card.name == "read"
    assert card.status == "error"
    assert card.arguments_available


def test_empty_and_multibyte_tool_names_have_bounded_trace_fields() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(ToolCallRequested(call_id="empty", name="", arguments={}))
    renderer.event(ToolCallRequested(call_id="multibyte", name="🦀" * 129, arguments={}))

    empty, multibyte = renderer.tool_card_projection()
    assert empty.name == "(unnamed)"
    assert multibyte.name == f"{'🦀' * 127}…"


def test_unresolved_generic_to_process_crossing_projects_an_error() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(ToolCallRequested(call_id="call-cross", name="read", arguments={"path": "a"}))
    renderer.event(
        ToolCallRequested(
            call_id="call-cross",
            name="bash",
            arguments={"operation": "poll", "process_id": "process-1"},
        )
    )

    (card,) = renderer.tool_card_projection()
    assert card.call_id == "call-cross"
    assert card.name == "read"
    assert card.status == "error"
    assert card.arguments_available


def test_changed_process_metadata_resolves_the_ambiguous_binding() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="changed-process",
            name="bash",
            arguments={"operation": "poll", "process_id": "process-1"},
        )
    )
    renderer.event(
        ToolCallRequested(
            call_id="changed-process",
            name="bash",
            arguments={"operation": "cancel", "process_id": "process-2"},
        )
    )
    renderer.event(ToolCallRequested(call_id="changed-process", name="read", arguments={}))

    (card,) = renderer.tool_card_projection()
    assert card.status == "cancelled"


def test_unresolved_process_to_generic_crossing_does_not_rebind_the_call() -> None:
    renderer = RecordingTraceRenderer()
    renderer.event(
        ToolCallRequested(
            call_id="call-cross",
            name="bash",
            arguments={"operation": "poll", "process_id": "process-1"},
        )
    )
    renderer.event(ToolCallRequested(call_id="call-cross", name="read", arguments={"path": "a"}))

    assert renderer.tool_card_projection() == ()


def test_rpc_event_payloads_are_bounded() -> None:
    oversized = _inline_trace(
        "oversized_rpc_event",
        [
            {
                "type": "rpc.event",
                "event": {"type": "error", "message": "x" * 4001},
                "clock_ms": 0,
            }
        ],
        _default_initial(),
    )
    with pytest.raises(ValueError, match="event"):
        TraceFileAdapter.validate_python(oversized)

    nested: object = "value"
    for _ in range(8):
        nested = [nested]
    too_deep = _inline_trace(
        "deep_rpc_event",
        [
            {
                "type": "rpc.event",
                "event": {"type": "error", "nested": nested},
                "clock_ms": 0,
            }
        ],
        _default_initial(),
    )
    with pytest.raises(ValueError, match="depth 8"):
        TraceFileAdapter.validate_python(too_deep)


def test_expected_command_payloads_are_bounded() -> None:
    data = _inline_trace(
        "oversized_expected_command",
        [{"type": "local.submit", "content": "hello", "clock_ms": 0}],
        _default_initial(),
    )
    data["expected"]["commands"] = [{"type": "prompt", "id": "prompt-1", "prompt": "x" * 4001}]
    with pytest.raises(ValueError, match="expected.commands.0.prompt"):
        TraceFileAdapter.validate_python(data)


def test_local_approve_with_wrong_call_id_is_rejected() -> None:
    async def run() -> None:
        data = _inline_trace(
            "wrong_approval_target",
            [
                {"type": "local.submit", "content": "read file", "clock_ms": 0},
                {
                    "type": "rpc.event",
                    "event": {
                        "type": "tool.approval.requested",
                        "call_id": "call-1",
                        "name": "read",
                        "arguments": {},
                        "safety": "read",
                    },
                    "clock_ms": 10,
                },
                {
                    "type": "local.approve",
                    "call_id": "call-wrong",
                    "approved": False,
                    "clock_ms": 20,
                },
            ],
            _default_initial(),
        )
        trace = TraceFileAdapter.validate_python(data)
        with pytest.raises(TraceReplayError, match="call-wrong"):
            await run_trace(trace)

    anyio.run(run)


def test_local_trust_with_wrong_request_id_is_rejected() -> None:
    async def run() -> None:
        data = _inline_trace(
            "wrong_trust_target",
            [
                {
                    "type": "rpc.event",
                    "event": {
                        "type": "trust.requested",
                        "request_id": "trust-1",
                        "project_path": "/tmp/proj",
                    },
                    "clock_ms": 0,
                },
                {
                    "type": "local.trust",
                    "request_id": "trust-wrong",
                    "trusted": True,
                    "clock_ms": 10,
                },
            ],
            _default_initial(),
        )
        trace = TraceFileAdapter.validate_python(data)
        with pytest.raises(TraceReplayError, match="trust-wrong"):
            await run_trace(trace)

    anyio.run(run)


def test_initial_view_mode_and_last_session_are_applied() -> None:
    async def run() -> None:
        initial = _default_initial()
        initial["view"] = {
            "input_ready": True,
            "mode": "plan",
            "last_session": "session-1",
        }
        data = _inline_trace(
            "seeded_view",
            [{"type": "local.submit", "content": "hello", "clock_ms": 0}],
            initial,
        )
        trace = TraceFileAdapter.validate_python(data)
        result = await run_trace(trace)

        assert result.view.mode == "plan"
        assert result.view.last_session == "session-1"
        # The run itself still advances status through prompt submission.
        first_prompt = result.commands[0]
        assert first_prompt["type"] == "prompt"

    anyio.run(run)
