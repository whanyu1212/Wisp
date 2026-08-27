"""Versioned, bounded JSON trace format for TUI frontend conformance."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_FORMAT_VERSION = 1
TRACE_SCHEMA_VERSION = 1
DEFAULT_TRACE_SCHEMA_ROOT = Path("schemas/tui-traces")

_TRACE_SCHEMA = "trace.schema.json"
_MANIFEST = "manifest.json"

MAX_TRACE_CONTENT_CHARS = 4000
_MAX_TRACE_DESCRIPTION_CHARS = 500
_MAX_TRACE_INPUTS = 64
_MAX_TRACE_COMMANDS = 32
_MAX_TRACE_EVENTS = 32
_MAX_JSON_COLLECTION_ITEMS = 64
_MAX_JSON_DEPTH = 8
_MAX_JSON_NODES = 1024
_TRACE_NAME_PATTERN = r"^[a-z][a-z0-9_.-]*$"
_TRACE_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
_SlashArgument = Annotated[str, StringConstraints(min_length=1, max_length=512)]
_JsonString = Annotated[str, StringConstraints(max_length=MAX_TRACE_CONTENT_CHARS)]
_JsonKey = Annotated[str, StringConstraints(max_length=128)]
type JsonValue = (
    None
    | bool
    | int
    | float
    | _JsonString
    | Annotated[list["JsonValue"], Field(max_length=_MAX_JSON_COLLECTION_ITEMS)]
    | Annotated[dict[_JsonKey, "JsonValue"], Field(max_length=_MAX_JSON_COLLECTION_ITEMS)]
)
type JsonObject = Annotated[dict[_JsonKey, JsonValue], Field(max_length=_MAX_JSON_COLLECTION_ITEMS)]


def _bound_json_structure(value: Any, *, label: str) -> Any:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise ValueError(f"{label} exceeds {_MAX_JSON_NODES} JSON nodes")
        if depth > _MAX_JSON_DEPTH:
            raise ValueError(f"{label} exceeds JSON depth {_MAX_JSON_DEPTH}")
        if isinstance(node, dict):
            if len(node) > _MAX_JSON_COLLECTION_ITEMS:
                raise ValueError(f"{label} object exceeds {_MAX_JSON_COLLECTION_ITEMS} properties")
            if any(not isinstance(key, str) or len(key) > 128 for key in node):
                raise ValueError(f"{label} has an invalid JSON object key")
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            if len(node) > _MAX_JSON_COLLECTION_ITEMS:
                raise ValueError(f"{label} array exceeds {_MAX_JSON_COLLECTION_ITEMS} items")
            stack.extend((child, depth + 1) for child in node)
    return value


class _TraceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TraceViewProjection(_TraceModel):
    """Semantic view projection verified by traces; not terminal cells."""

    status: str = Field(min_length=1, max_length=64)
    input_mode: str = Field(min_length=1, max_length=32)
    input_ready: bool
    queued_steering: int = Field(ge=0, le=32, strict=True)
    queued_follow_ups: int = Field(ge=0, le=32, strict=True)
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    mode: Literal["build", "plan"] = "build"
    last_session: str | None = Field(default=None, min_length=1, max_length=128)


class TraceInteractionProjection(_TraceModel):
    status: Literal[
        "idle",
        "running",
        "compacting",
        "waiting_for_approval",
        "waiting_for_trust",
        "exiting",
    ]
    current_command_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=_TRACE_ID_PATTERN
    )
    current_command_type: Literal["prompt", "init", "compact"] | None = None
    pending_approval_call_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=_TRACE_ID_PATTERN
    )
    pending_trust_request_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=_TRACE_ID_PATTERN
    )
    cancel_requested: bool = False
    exit_requested: bool = False


class TraceInitialState(_TraceModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        json_schema_extra={
            "x-wisp-invariants": {
                "view.provider": {"equalsField": "provider"},
                "view.model": {"equalsField": "model"},
                "view.queued_steering": {"const": 0},
                "view.queued_follow_ups": {"const": 0},
                "view.status": {
                    "derivedFrom": "interaction.status ?? 'idle'",
                    "valueMap": {
                        "waiting_for_approval": "waiting for approval",
                        "waiting_for_trust": "waiting for trust",
                    },
                    "defaultIdentity": True,
                },
                "view.input_mode": {
                    "derivedFrom": "interaction.status ?? 'idle'",
                    "valueMap": {
                        "idle": "idle",
                        "running": "running",
                        "compacting": "running",
                        "waiting_for_approval": "approval",
                        "waiting_for_trust": "trust",
                        "exiting": "exiting",
                    },
                },
            }
        },
    )

    provider: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    effort: str | None = Field(default=None, min_length=1, max_length=64)
    view: TraceViewProjection | None = None
    interaction: TraceInteractionProjection | None = None

    @model_validator(mode="after")
    def _require_representable_view(self) -> TraceInitialState:
        view = self.view
        if view is None:
            return self
        if view.provider != self.provider or view.model != self.model:
            raise ValueError("initial view provider/model must match initial shell configuration")
        if view.queued_steering or view.queued_follow_ups:
            raise ValueError("initial view queues require submissions and must be empty")

        status = self.interaction.status if self.interaction is not None else "idle"
        expected_view_status = {
            "waiting_for_approval": "waiting for approval",
            "waiting_for_trust": "waiting for trust",
        }.get(status, status)
        expected_input_mode = {
            "running": "running",
            "compacting": "running",
            "waiting_for_approval": "approval",
            "waiting_for_trust": "trust",
            "exiting": "exiting",
        }.get(status, "idle")
        if view.status != expected_view_status or view.input_mode != expected_input_mode:
            raise ValueError("initial view status/input mode must match initial interaction")
        return self


class TraceLocalSubmit(_TraceModel):
    type: Literal["local.submit"] = "local.submit"
    content: str = Field(min_length=1, max_length=MAX_TRACE_CONTENT_CHARS)
    clock_ms: int = Field(ge=0, le=3_600_000, strict=True)


class TraceLocalSlash(_TraceModel):
    type: Literal["local.slash"] = "local.slash"
    command: str = Field(min_length=1, max_length=64, pattern=r"^[a-z/][a-z0-9/_-]*$")
    args: tuple[_SlashArgument, ...] = Field(default=(), max_length=8, strict=False)
    clock_ms: int = Field(ge=0, le=3_600_000, strict=True)


class TraceLocalCancel(_TraceModel):
    type: Literal["local.cancel"] = "local.cancel"
    clock_ms: int = Field(ge=0, le=3_600_000, strict=True)


class TraceLocalApprove(_TraceModel):
    type: Literal["local.approve"] = "local.approve"
    call_id: str = Field(min_length=1, max_length=128, pattern=_TRACE_ID_PATTERN)
    approved: bool
    reason: str | None = Field(default=None, min_length=1, max_length=512)
    scope: Literal["once", "tool_session", "all_session"] | None = None
    clock_ms: int = Field(ge=0, le=3_600_000, strict=True)


class TraceLocalTrust(_TraceModel):
    type: Literal["local.trust"] = "local.trust"
    request_id: str = Field(min_length=1, max_length=128, pattern=_TRACE_ID_PATTERN)
    trusted: bool
    transient: bool | None = None
    clock_ms: int = Field(ge=0, le=3_600_000, strict=True)


class TraceRpcEvent(_TraceModel):
    type: Literal["rpc.event"] = "rpc.event"
    event: JsonObject = Field(
        json_schema_extra={
            "required": ["type"],
            "x-wisp-max-depth": _MAX_JSON_DEPTH,
            "x-wisp-max-nodes": _MAX_JSON_NODES,
        }
    )
    clock_ms: int = Field(ge=0, le=3_600_000, strict=True)

    @field_validator("event", mode="before")
    @classmethod
    def _bound_event_structure(cls, value: Any) -> Any:
        return _bound_json_structure(value, label="RPC event")


class TraceRpcClosed(_TraceModel):
    type: Literal["rpc.closed"] = "rpc.closed"
    error: str | None = Field(default=None, min_length=1, max_length=512)
    clock_ms: int = Field(ge=0, le=3_600_000, strict=True)


TraceInput = Annotated[
    TraceLocalSubmit
    | TraceLocalSlash
    | TraceLocalCancel
    | TraceLocalApprove
    | TraceLocalTrust
    | TraceRpcEvent
    | TraceRpcClosed,
    Field(discriminator="type"),
]
TraceInputAdapter: TypeAdapter[TraceInput] = TypeAdapter(TraceInput)


class TraceExpectedCommand(_TraceModel):
    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        strict=True,
        json_schema_extra={
            "x-wisp-max-depth": _MAX_JSON_DEPTH,
            "x-wisp-max-nodes": _MAX_JSON_NODES,
        },
    )

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    type: str = Field(min_length=1, max_length=64)
    id: str | None = Field(default=None, min_length=1, max_length=128, pattern=_TRACE_ID_PATTERN)

    @model_validator(mode="before")
    @classmethod
    def _bound_command_payload(cls, value: Any) -> Any:
        return _bound_json_structure(value, label="expected command")


class TraceExpected(_TraceModel):
    commands: tuple[TraceExpectedCommand, ...] = Field(max_length=_MAX_TRACE_COMMANDS, strict=False)
    view: TraceViewProjection
    interaction: TraceInteractionProjection
    retained_text: str | None = Field(default=None, max_length=MAX_TRACE_CONTENT_CHARS)


class TraceFile(_TraceModel):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=64, pattern=_TRACE_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=_MAX_TRACE_DESCRIPTION_CHARS)
    initial: TraceInitialState
    inputs: tuple[TraceInput, ...] = Field(min_length=1, max_length=_MAX_TRACE_INPUTS, strict=False)
    expected: TraceExpected


TraceFileAdapter: TypeAdapter[TraceFile] = TypeAdapter(TraceFile)


def trace_schema_directory(
    root: Path = DEFAULT_TRACE_SCHEMA_ROOT,
    *,
    schema_version: int = TRACE_SCHEMA_VERSION,
) -> Path:
    return root / f"v{schema_version}"


DEFAULT_TRACE_SCHEMA_DIRECTORY = trace_schema_directory()


def generate_trace_artifacts() -> dict[str, str]:
    trace_schema = _schema_for_model(TraceFileAdapter, title="Wisp TUI transition trace")
    # The inputs array is already bounded via TraceFile, but ensure discriminator is present.
    trace_schema["x-wisp-trace-schema-version"] = TRACE_SCHEMA_VERSION
    schemas = {_TRACE_SCHEMA: _serialize(trace_schema)}
    schema_hashes: JsonObject = {
        filename: _sha256(content) for filename, content in schemas.items()
    }
    manifest: JsonObject = {
        "schema_dialect": JSON_SCHEMA_DIALECT,
        "schema_format_version": SCHEMA_FORMAT_VERSION,
        "schema_hashes": schema_hashes,
        "schemas": {"trace": _TRACE_SCHEMA},
        "trace_schema_version": TRACE_SCHEMA_VERSION,
    }
    return {**schemas, _MANIFEST: _serialize(manifest)}


def write_trace_artifacts(directory: Path = DEFAULT_TRACE_SCHEMA_DIRECTORY) -> None:
    _require_compatible_target(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in generate_trace_artifacts().items():
        (directory / filename).write_text(content, encoding="utf-8")


def stale_trace_artifacts(directory: Path = DEFAULT_TRACE_SCHEMA_DIRECTORY) -> tuple[str, ...]:
    expected = generate_trace_artifacts()
    stale: list[str] = []
    for filename, expected_content in expected.items():
        path = directory / filename
        try:
            actual = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            stale.append(filename)
            continue
        if actual != expected_content:
            stale.append(filename)
    try:
        actual_names = {entry.name for entry in directory.iterdir()}
    except OSError:
        actual_names = set()
    stale.extend(sorted(actual_names.difference(expected)))
    return tuple(stale)


def _schema_for_model(adapter: TypeAdapter[Any], *, title: str) -> JsonObject:
    schema = cast(JsonObject, deepcopy(adapter.json_schema(mode="validation")))
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["title"] = title
    return schema


def _require_compatible_target(directory: Path) -> None:
    if directory.name.startswith("v") and directory.name[1:].isdigit():
        directory_version = int(directory.name[1:])
        if directory_version != TRACE_SCHEMA_VERSION:
            raise RuntimeError(
                f"refusing to write trace v{TRACE_SCHEMA_VERSION} into {directory.name}"
            )
    manifest_path = directory / _MANIFEST
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_version = manifest["trace_schema_version"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"cannot verify existing trace manifest: {manifest_path}") from exc
    if manifest_version != TRACE_SCHEMA_VERSION:
        raise RuntimeError(
            f"refusing to replace trace v{manifest_version} "
            f"with v{TRACE_SCHEMA_VERSION} in {directory}"
        )


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _serialize(value: JsonObject) -> str:
    return f"{json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)}\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="write generated trace artifacts")
    action.add_argument("--check", action="store_true", help="fail if trace artifacts are stale")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_TRACE_SCHEMA_DIRECTORY,
        help=f"artifact directory (default: {DEFAULT_TRACE_SCHEMA_DIRECTORY})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_directory = cast(Path, args.output_dir)
    if args.write:
        write_trace_artifacts(output_directory)
        return 0
    stale = stale_trace_artifacts(output_directory)
    if not stale:
        return 0
    for filename in stale:
        print(f"stale generated trace artifact: {filename}", file=sys.stderr)
    print("regenerate with: uv run python -m wisp.tui.trace_schema --write", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
