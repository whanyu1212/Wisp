"""Generate deterministic schemas for the current live JSONL-RPC protocol."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import UnionType
from typing import Annotated, cast, get_args, get_origin

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, TypeAdapter

from wisp.events import EVENT_SCHEMA_VERSION, KnownWispEventAdapter
from wisp.rpc.commands import RpcCommandAdapter
from wisp.rpc.protocol import (
    LIVE_RPC_PROTOCOL_VERSION,
    MAX_HANDSHAKE_FRAME_BYTES,
    MAX_LIVE_RPC_FRAME_BYTES,
    RpcClientHello,
    RpcServerHandshakeAdapter,
)

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_FORMAT_VERSION = 1
DEFAULT_SCHEMA_ROOT = Path("schemas/live-rpc")
# On a protocol bump, pin every older manifest here before generating the new directory.
# The manifest transitively pins all schemas and version metadata in that immutable bundle.
HISTORICAL_PROTOCOL_MANIFEST_SHA256: tuple[tuple[int, str], ...] = ()

_CLIENT_HANDSHAKE_SCHEMA = "client-handshake.schema.json"
_SERVER_HANDSHAKE_SCHEMA = "server-handshake.schema.json"
_COMMAND_SCHEMA = "commands.schema.json"
_EVENT_SCHEMA = "events.schema.json"
_MANIFEST = "manifest.json"
_SCHEMA_FILENAMES = (
    _CLIENT_HANDSHAKE_SCHEMA,
    _SERVER_HANDSHAKE_SCHEMA,
    _COMMAND_SCHEMA,
    _EVENT_SCHEMA,
)

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = dict[str, JsonValue]


def protocol_schema_directory(
    root: Path = DEFAULT_SCHEMA_ROOT,
    *,
    protocol_version: int = LIVE_RPC_PROTOCOL_VERSION,
) -> Path:
    """Return the immutable directory assigned to one live protocol version."""

    return root / f"v{protocol_version}"


DEFAULT_SCHEMA_DIRECTORY = protocol_schema_directory()


def generate_protocol_artifacts() -> dict[str, str]:
    """Return every generated protocol artifact keyed by relative filename."""

    client_handshake = _schema_for_model(TypeAdapter(RpcClientHello), title="RPC client handshake")
    _harden_capability_schema(client_handshake)
    _require_root_property(client_handshake, "type")
    client_handshake["x-wisp-cross-field-invariants"] = [
        {
            "kind": "ordered-range",
            "maximum_property": "max_protocol_version",
            "minimum_property": "min_protocol_version",
        },
        {
            "kind": "ordered-range",
            "maximum_property": "max_event_schema_version",
            "minimum_property": "min_event_schema_version",
        },
        {
            "kind": "array-subset",
            "subset_property": "required_capabilities",
            "superset_property": "supported_capabilities",
        },
    ]

    server_handshake = _schema_for_model(
        RpcServerHandshakeAdapter,
        title="RPC server handshake",
    )
    _harden_capability_schema(server_handshake)
    _require_discriminator_property(server_handshake, "type")
    _annotate_server_handshake_invariants(server_handshake)

    commands = _schema_for_model(RpcCommandAdapter, title="Wisp typed-client RPC commands")
    _shape_command_output_schema(commands)

    events = _schema_for_model(KnownWispEventAdapter, title="Wisp current live event output")
    _shape_current_event_output_schema(events)

    schemas = {
        _CLIENT_HANDSHAKE_SCHEMA: _serialize(client_handshake),
        _SERVER_HANDSHAKE_SCHEMA: _serialize(server_handshake),
        _COMMAND_SCHEMA: _serialize(commands),
        _EVENT_SCHEMA: _serialize(events),
    }
    schema_hashes: JsonObject = {
        filename: _sha256(content) for filename, content in schemas.items()
    }
    manifest: JsonObject = {
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "fixed_handshake_frame_bytes": MAX_HANDSHAKE_FRAME_BYTES,
        "live_protocol_version": LIVE_RPC_PROTOCOL_VERSION,
        "maximum_application_frame_bytes": MAX_LIVE_RPC_FRAME_BYTES,
        "schema_dialect": JSON_SCHEMA_DIALECT,
        "schema_format_version": SCHEMA_FORMAT_VERSION,
        "schema_hashes": schema_hashes,
        "schemas": {
            "client_handshake": _CLIENT_HANDSHAKE_SCHEMA,
            "commands": _COMMAND_SCHEMA,
            "events": _EVENT_SCHEMA,
            "server_handshake": _SERVER_HANDSHAKE_SCHEMA,
        },
    }
    return {**schemas, _MANIFEST: _serialize(manifest)}


def write_protocol_artifacts(directory: Path = DEFAULT_SCHEMA_DIRECTORY) -> None:
    """Write the current generated artifacts without crossing a version boundary."""

    _require_compatible_target(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in generate_protocol_artifacts().items():
        (directory / filename).write_text(content, encoding="utf-8")


def write_protocol_archive(destination: Path) -> None:
    """Write a deterministic release archive for the current protocol bundle."""

    artifacts = generate_protocol_artifacts()
    prefix = f"wisp-live-rpc-v{LIVE_RPC_PROTOCOL_VERSION}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw_output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for filename, content in artifacts.items():
                    payload = content.encode("utf-8")
                    info = tarfile.TarInfo(f"{prefix}/{filename}")
                    info.size = len(payload)
                    info.mode = 0o644
                    info.mtime = 0
                    archive.addfile(info, io.BytesIO(payload))


def stale_protocol_artifacts(directory: Path = DEFAULT_SCHEMA_DIRECTORY) -> tuple[str, ...]:
    """Return missing, stale, or obsolete generated artifact names in stable order."""

    expected_artifacts = generate_protocol_artifacts()
    stale: list[str] = []
    for filename, expected in expected_artifacts.items():
        path = directory / filename
        try:
            actual = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            stale.append(filename)
            continue
        if actual != expected:
            stale.append(filename)
    try:
        actual_names = {entry.name for entry in directory.iterdir()}
    except OSError:
        actual_names = set()
    stale.extend(sorted(actual_names.difference(expected_artifacts)))
    return tuple(stale)


def invalid_protocol_history(
    root: Path = DEFAULT_SCHEMA_ROOT,
    *,
    current_protocol_version: int = LIVE_RPC_PROTOCOL_VERSION,
    historical_manifest_hashes: Mapping[int, str] | None = None,
) -> tuple[str, ...]:
    """Return malformed committed version-directory diagnostics."""

    pinned_entries = (
        HISTORICAL_PROTOCOL_MANIFEST_SHA256
        if historical_manifest_hashes is None
        else tuple(historical_manifest_hashes.items())
    )
    pinned_hashes = dict(pinned_entries)
    if len(pinned_hashes) != len(pinned_entries):
        return ("historical protocol manifest hash registry contains duplicate versions",)
    expected_historical_versions = set(range(1, current_protocol_version))
    if set(pinned_hashes) != expected_historical_versions:
        return ("historical protocol manifest hash registry is incomplete",)
    try:
        directories = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return (f"missing protocol schema root: {root}",)
    diagnostics: list[str] = []
    actual_versions: set[int] = set()
    for directory in directories:
        if not directory.name.startswith("v") or not directory.name[1:].isdigit():
            diagnostics.append(f"unexpected protocol schema directory: {directory.name}")
            continue
        directory_version = int(directory.name[1:])
        if directory_version < 1 or directory.name != f"v{directory_version}":
            diagnostics.append(f"unexpected protocol schema directory: {directory.name}")
            continue
        actual_versions.add(directory_version)
        manifest_path = directory / _MANIFEST
        try:
            manifest_content = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_content)
        except (OSError, UnicodeError, json.JSONDecodeError):
            diagnostics.append(f"invalid protocol manifest: {directory.name}/{_MANIFEST}")
            continue
        if directory_version < current_protocol_version:
            if _sha256(manifest_content) != pinned_hashes[directory_version]:
                diagnostics.append(f"historical protocol manifest changed: {directory.name}")
        elif directory_version > current_protocol_version:
            diagnostics.append(f"unexpected future protocol schema directory: {directory.name}")
        if not isinstance(manifest, dict):
            diagnostics.append(f"invalid protocol manifest object: {directory.name}/{_MANIFEST}")
            continue
        declared_version = manifest.get("live_protocol_version")
        if declared_version != directory_version:
            diagnostics.append(f"protocol directory/manifest mismatch: {directory.name}")
        hashes = manifest.get("schema_hashes")
        if not isinstance(hashes, dict):
            diagnostics.append(f"missing protocol schema hashes: {directory.name}")
            continue
        expected_names = {*_SCHEMA_FILENAMES, _MANIFEST}
        actual_names = {entry.name for entry in directory.iterdir()}
        if actual_names != expected_names:
            diagnostics.append(f"unexpected protocol artifact set: {directory.name}")
        for filename in _SCHEMA_FILENAMES:
            expected_hash = hashes.get(filename)
            try:
                content = (directory / filename).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                diagnostics.append(f"missing protocol schema: {directory.name}/{filename}")
                continue
            if not isinstance(expected_hash, str) or _sha256(content) != expected_hash:
                diagnostics.append(f"protocol schema hash mismatch: {directory.name}/{filename}")
            try:
                schema = json.loads(content)
                Draft202012Validator.check_schema(schema)
            except (json.JSONDecodeError, SchemaError):
                diagnostics.append(f"invalid protocol JSON Schema: {directory.name}/{filename}")
                continue
            if not isinstance(schema, dict) or schema.get("$schema") != JSON_SCHEMA_DIALECT:
                diagnostics.append(f"protocol schema dialect mismatch: {directory.name}/{filename}")
    missing_versions = set(range(1, current_protocol_version + 1)).difference(actual_versions)
    diagnostics.extend(
        f"missing protocol schema directory: v{version}" for version in sorted(missing_versions)
    )
    return tuple(diagnostics)


def modified_committed_protocol_artifacts(
    base_ref: str,
    *,
    root: Path = DEFAULT_SCHEMA_ROOT,
) -> tuple[str, ...]:
    """Return committed version artifacts modified since a trusted Git ref."""

    result = subprocess.run(
        (
            "git",
            "diff",
            "--name-only",
            "--diff-filter=MDRTUXB",
            base_ref,
            "HEAD",
            "--",
            str(root),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git diff failed"
        return (f"cannot verify immutable protocol history: {detail}",)
    prefix = f"{root.as_posix().rstrip('/')}/"
    modified: list[str] = []
    for path in result.stdout.splitlines():
        relative = path.removeprefix(prefix)
        version_directory = relative.partition("/")[0]
        if (
            version_directory.startswith("v")
            and version_directory[1:].isdigit()
            and version_directory == f"v{int(version_directory[1:])}"
        ):
            modified.append(path)
    return tuple(modified)


def _shape_command_output_schema(schema: JsonObject) -> None:
    """Describe exactly what typed command ``to_json_line`` may emit."""

    _require_discriminator_property(schema, "type")
    models = _adapter_models(RpcCommandAdapter)
    definitions = _object_member(schema, "$defs")
    for name, model in models.items():
        raw_definition = definitions.get(name)
        if not isinstance(raw_definition, dict):
            continue
        definition = raw_definition
        properties = _object_member(definition, "properties")
        required: list[str] = []
        for field_name, field in model.model_fields.items():
            raw_property = properties.get(field_name)
            if not isinstance(raw_property, dict):
                continue
            property_schema = raw_property
            if field.default is None:
                _remove_null(property_schema)
                property_schema.pop("default", None)
            else:
                required.append(field_name)
        definition["required"] = [cast(JsonValue, value) for value in required]
        definition["additionalProperties"] = False
    _add_command_semantic_constraints(schema)


def _harden_capability_schema(schema: JsonObject) -> None:
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return
    capability = definitions.get("RpcCapability")
    if isinstance(capability, dict):
        capability["pattern"] = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*(?![\s\S])"


def _annotate_server_handshake_invariants(schema: JsonObject) -> None:
    definitions = _object_member(schema, "$defs")
    hello = _named_definition(definitions, "RpcServerHello")
    hello["x-wisp-cross-field-invariants"] = [
        {
            "kind": "ordered-range",
            "maximum_property": "max_frontend_protocol_version",
            "minimum_property": "min_frontend_protocol_version",
        },
        {
            "kind": "value-in-range",
            "maximum_property": "max_frontend_protocol_version",
            "minimum_property": "min_frontend_protocol_version",
            "value_property": "protocol_version",
        },
    ]
    rejection = _named_definition(definitions, "RpcHandshakeRejected")
    rejection["x-wisp-cross-field-invariants"] = [
        {
            "kind": "ordered-range",
            "maximum_property": "max_protocol_version",
            "minimum_property": "min_protocol_version",
        }
    ]


def _shape_current_event_output_schema(schema: JsonObject) -> None:
    """Project readable historical models into the exact current emitted shape."""

    models = _adapter_models(KnownWispEventAdapter)
    definitions = _object_member(schema, "$defs")
    for name, model in models.items():
        raw_definition = definitions.get(name)
        if not isinstance(raw_definition, dict):
            continue
        definition = raw_definition
        raw_properties = definition.get("properties")
        if not isinstance(raw_properties, dict):
            continue
        properties = raw_properties
        for field_name, field in model.model_fields.items():
            if field.exclude is True:
                properties.pop(field_name, None)
                continue
            raw_property = properties.get(field_name)
            if isinstance(raw_property, dict) and _contains_type(field.annotation, Decimal):
                _remove_number(raw_property)
        definition["required"] = [cast(JsonValue, name) for name in properties]
        definition["additionalProperties"] = False

    mapping = _discriminator_mapping(schema)
    if not mapping:
        raise RuntimeError("generated event union has no discriminator mapping")
    for reference in mapping.values():
        definition = _local_definition(schema, reference)
        properties = _object_member(definition, "properties")
        existing = properties.get("schema_version")
        if not isinstance(existing, dict):
            raise RuntimeError(f"event definition {reference!r} has no schema_version property")
        title = existing.get("title", "Schema Version")
        properties["schema_version"] = {
            "const": EVENT_SCHEMA_VERSION,
            "default": EVENT_SCHEMA_VERSION,
            "title": title,
            "type": "integer",
        }


def _add_command_semantic_constraints(schema: JsonObject) -> None:
    definitions = _object_member(schema, "$defs")

    messages = _named_definition(definitions, "GetMessagesCommand")
    message_properties = _object_member(messages, "properties")
    entry_ids = _object_member(message_properties, "entry_ids")
    entry_ids_array = _single_non_null_variant(entry_ids)
    entry_ids_array["uniqueItems"] = True
    items = _object_member(entry_ids_array, "items")
    items["minLength"] = 1
    messages["allOf"] = cast(
        JsonValue,
        [
            {"not": {"required": ["before_entry_id", "after_entry_id"]}},
            {
                "if": {"required": ["entry_ids"]},
                "then": {
                    "not": {
                        "anyOf": [
                            {"required": ["before_entry_id"]},
                            {"required": ["after_entry_id"]},
                        ]
                    }
                },
            },
            {
                "if": {
                    "properties": {"full_content": {"const": True}},
                    "required": ["full_content"],
                },
                "then": {
                    "properties": {"entry_ids": {"maxItems": 1, "minItems": 1}},
                    "required": ["entry_ids"],
                },
            },
        ],
    )

    configure = _named_definition(definitions, "ConfigureCommand")
    configure["allOf"] = cast(
        JsonValue,
        [
            {
                "anyOf": [
                    {"required": ["provider"]},
                    {"required": ["model"]},
                    {"required": ["effort"]},
                    {"required": ["auto_compaction_enabled"]},
                    {"required": ["mode"]},
                    {
                        "properties": {"clear_effort": {"const": True}},
                        "required": ["clear_effort"],
                    },
                ]
            },
            {
                "not": {
                    "properties": {"clear_effort": {"const": True}},
                    "required": ["clear_effort", "effort"],
                }
            },
        ],
    )

    approval = _named_definition(definitions, "ApprovalCommand")
    approval["allOf"] = cast(
        JsonValue,
        [
            {
                "if": {
                    "properties": {"approved": {"const": False}},
                    "required": ["approved"],
                },
                "then": {"not": {"required": ["scope"]}},
            }
        ],
    )


def _adapter_models[T](adapter: TypeAdapter[T]) -> dict[str, type[BaseModel]]:
    models: dict[str, type[BaseModel]] = {}
    visited: set[int] = set()

    def visit(value: object) -> None:
        value_id = id(value)
        if value_id in visited:
            return
        visited.add(value_id)
        if isinstance(value, Mapping):
            model = value.get("cls")
            if isinstance(model, type) and issubclass(model, BaseModel):
                existing = models.get(model.__name__)
                if existing is not None and existing is not model:
                    raise RuntimeError(f"duplicate protocol model name: {model.__name__}")
                models[model.__name__] = model
            for member in value.values():
                visit(member)
        elif isinstance(value, (list, tuple)):
            for member in value:
                visit(member)

    visit(adapter.core_schema)
    return models


def _schema_for_model[T](adapter: TypeAdapter[T], *, title: str) -> JsonObject:
    schema = cast(JsonObject, deepcopy(adapter.json_schema(mode="validation")))
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["title"] = title
    return schema


def _require_root_property(schema: JsonObject, property_name: str) -> None:
    _require_property(schema, property_name)


def _require_discriminator_property(schema: JsonObject, property_name: str) -> None:
    mapping = _discriminator_mapping(schema)
    if not mapping:
        raise RuntimeError("generated protocol union has no discriminator mapping")
    for reference in mapping.values():
        definition = _local_definition(schema, reference)
        _require_property(definition, property_name)


def _discriminator_mapping(schema: JsonObject) -> dict[str, str]:
    discriminator = _object_member(schema, "discriminator")
    raw_mapping = discriminator.get("mapping")
    if not isinstance(raw_mapping, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_mapping.items()
    ):
        raise RuntimeError("generated protocol discriminator mapping is invalid")
    return cast(dict[str, str], raw_mapping)


def _local_definition(schema: JsonObject, reference: str) -> JsonObject:
    prefix = "#/$defs/"
    if not reference.startswith(prefix):
        raise RuntimeError(f"generated protocol schema contains a non-local reference: {reference}")
    definitions = _object_member(schema, "$defs")
    return _named_definition(definitions, reference.removeprefix(prefix))


def _named_definition(definitions: JsonObject, name: str) -> JsonObject:
    definition = definitions.get(name)
    if not isinstance(definition, dict):
        raise RuntimeError(f"generated protocol schema is missing definition: {name}")
    return definition


def _require_property(schema: JsonObject, property_name: str) -> None:
    properties = _object_member(schema, "properties")
    if property_name not in properties:
        raise RuntimeError(f"generated protocol schema is missing property: {property_name}")
    raw_required = schema.get("required", [])
    if not isinstance(raw_required, list) or not all(
        isinstance(value, str) for value in raw_required
    ):
        raise RuntimeError("generated protocol schema has an invalid required field list")
    required = cast(list[str], raw_required)
    if property_name not in required:
        required.append(property_name)
    schema["required"] = [cast(JsonValue, value) for value in required]


def _object_member(value: Mapping[str, JsonValue], key: str) -> JsonObject:
    member = value.get(key)
    if not isinstance(member, dict):
        raise RuntimeError(f"generated protocol schema is missing object member: {key}")
    return member


def _remove_null(schema: JsonObject) -> None:
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        schema["anyOf"] = [
            variant
            for variant in variants
            if not isinstance(variant, dict) or variant.get("type") != "null"
        ]


def _single_non_null_variant(schema: JsonObject) -> JsonObject:
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        object_variants = [variant for variant in variants if isinstance(variant, dict)]
        if len(object_variants) == 1:
            return object_variants[0]
    return schema


def _remove_number(schema: JsonObject) -> None:
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        schema["anyOf"] = [
            variant
            for variant in variants
            if not isinstance(variant, dict) or variant.get("type") != "number"
        ]
    elif schema.get("type") == "number":
        schema["type"] = "string"


def _contains_type(annotation: object, target: type[object]) -> bool:
    if annotation is target:
        return True
    origin = get_origin(annotation)
    if origin in {Annotated, UnionType} or origin is not None:
        return any(_contains_type(argument, target) for argument in get_args(annotation))
    return False


def _require_compatible_target(directory: Path) -> None:
    if directory.name.startswith("v") and directory.name[1:].isdigit():
        directory_version = int(directory.name[1:])
        if directory_version != LIVE_RPC_PROTOCOL_VERSION:
            raise RuntimeError(
                f"refusing to write protocol v{LIVE_RPC_PROTOCOL_VERSION} into {directory.name}"
            )
    manifest_path = directory / _MANIFEST
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_version = manifest["live_protocol_version"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"cannot verify existing protocol manifest: {manifest_path}") from exc
    if manifest_version != LIVE_RPC_PROTOCOL_VERSION:
        raise RuntimeError(
            f"refusing to replace protocol v{manifest_version} with "
            f"v{LIVE_RPC_PROTOCOL_VERSION} in {directory}"
        )


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _serialize(value: JsonObject) -> str:
    return f"{json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)}\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="write generated schema artifacts")
    action.add_argument("--check", action="store_true", help="fail if schema artifacts are stale")
    action.add_argument("--archive", type=Path, help="write a deterministic release archive")
    parser.add_argument(
        "--immutable-base",
        help="trusted Git ref whose committed version artifacts must remain unchanged",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_SCHEMA_DIRECTORY,
        help=f"artifact directory (default: {DEFAULT_SCHEMA_DIRECTORY})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_directory = cast(Path, args.output_dir)
    archive = cast(Path | None, args.archive)
    immutable_base = cast(str | None, args.immutable_base)
    if immutable_base is not None and not args.check:
        raise SystemExit("--immutable-base requires --check")
    if args.write:
        write_protocol_artifacts(output_directory)
        return 0
    if archive is not None:
        write_protocol_archive(archive)
        return 0
    stale = stale_protocol_artifacts(output_directory)
    history_errors = invalid_protocol_history(output_directory.parent)
    immutable_changes = (
        modified_committed_protocol_artifacts(
            immutable_base,
            root=output_directory.parent,
        )
        if immutable_base is not None
        else ()
    )
    if not stale and not history_errors and not immutable_changes:
        return 0
    for filename in stale:
        print(f"stale generated protocol artifact: {filename}", file=sys.stderr)
    for error in history_errors:
        print(error, file=sys.stderr)
    for path in immutable_changes:
        print(f"committed protocol artifact is immutable: {path}", file=sys.stderr)
    print(
        "regenerate with: uv run python -m wisp.rpc.protocol_schema --write",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
