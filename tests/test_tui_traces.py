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

from wisp.events import ToolApprovalResolved, ToolCallRequested, ToolResultReady
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
