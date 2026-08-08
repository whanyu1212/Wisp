"""Bounded parsing and validation for Agent Skills frontmatter."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from yaml.tokens import AliasToken, AnchorToken, TagToken

from wisp.skills.models import SkillDiagnostic, SkillEntry, SkillSource

type SkillMetadataErrorCode = Literal["invalid-frontmatter", "invalid-metadata", "invalid-yaml"]

_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SUPPORTED_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)
_MISSING = object()
_YAML_BOOL_TAG = "tag:yaml.org,2002:bool"
_YAML_FLOAT_TAG = "tag:yaml.org,2002:float"
_YAML_INT_TAG = "tag:yaml.org,2002:int"
_YAML_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_YAML_12_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_YAML_12_FLOAT = re.compile(
    r"^(?:"
    r"[-+]?(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|[0-9]+[eE][-+]?[0-9]+)"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)"
    r")$"
)
_YAML_12_INT = re.compile(
    r"^(?:[-+]?0b[0-1]+|[-+]?0o[0-7]+|[-+]?(?:0|[1-9][0-9]*)|[-+]?0x[0-9a-fA-F]+)$"
)


class SkillMetadataError(ValueError):
    """A bounded, user-actionable failure isolated to one SKILL.md."""

    def __init__(
        self,
        code: SkillMetadataErrorCode,
        message: str,
        *,
        bytes_read: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.bytes_read = bytes_read


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects duplicate mapping keys."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._document_root: yaml.nodes.Node | None = None

    def construct_document(self, node: yaml.nodes.Node) -> object:
        self._document_root = node
        try:
            construct = cast(
                Callable[[yaml.nodes.Node], object],
                super().construct_document,
            )
            return construct(node)
        finally:
            self._document_root = None

    def construct_mapping(
        self,
        node: yaml.nodes.MappingNode,
        deep: bool = False,
    ) -> dict[object, object]:
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "mapping keys must be scalar values",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key: {key!r}",
                    key_node.start_mark,
                )
            if (
                node is self._document_root
                and key == "name"
                and isinstance(value_node, yaml.nodes.ScalarNode)
            ):
                mapping[key] = self.construct_scalar(value_node)
            else:
                mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _construct_yaml_12_int(loader: _StrictSafeLoader, node: yaml.nodes.ScalarNode) -> int:
    value = loader.construct_scalar(node)
    sign = -1 if value.startswith("-") else 1
    value = value.removeprefix("-").removeprefix("+")
    if value.startswith("0b"):
        return sign * int(value[2:], 2)
    if value.startswith("0o"):
        return sign * int(value[2:], 8)
    if value.startswith("0x"):
        return sign * int(value[2:], 16)
    return sign * int(value, 10)


_StrictSafeLoader.yaml_implicit_resolvers = {
    key: [
        (tag, resolver)
        for tag, resolver in resolvers
        if tag not in {_YAML_BOOL_TAG, _YAML_FLOAT_TAG, _YAML_INT_TAG, _YAML_TIMESTAMP_TAG}
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _first_character in "tTfF":
    _StrictSafeLoader.yaml_implicit_resolvers.setdefault(_first_character, []).append(
        (_YAML_BOOL_TAG, _YAML_12_BOOL)
    )
for _first_character in "-+0123456789":
    _StrictSafeLoader.yaml_implicit_resolvers.setdefault(_first_character, []).append(
        (_YAML_INT_TAG, _YAML_12_INT)
    )
for _first_character in "-+0123456789.":
    _StrictSafeLoader.yaml_implicit_resolvers.setdefault(_first_character, []).append(
        (_YAML_FLOAT_TAG, _YAML_12_FLOAT)
    )
_StrictSafeLoader.add_constructor(_YAML_INT_TAG, _construct_yaml_12_int)


def read_skill_metadata(
    metadata_fd: int,
    *,
    source: SkillSource,
    skill_root: Path,
    directory_name: str,
    skill_file: Path,
    max_frontmatter_bytes: int,
) -> tuple[SkillEntry, int, tuple[SkillDiagnostic, ...]]:
    """Read only frontmatter from an open SKILL.md descriptor and validate it."""

    frontmatter, consumed = _read_frontmatter(
        metadata_fd,
        max_frontmatter_bytes=max_frontmatter_bytes,
    )
    try:
        entry, diagnostics = _parse_entry(
            frontmatter,
            source=source,
            skill_root=skill_root,
            directory_name=directory_name,
            skill_file=skill_file,
        )
    except SkillMetadataError as exc:
        exc.bytes_read = consumed
        raise
    return entry, consumed, diagnostics


def _read_frontmatter(metadata_fd: int, *, max_frontmatter_bytes: int) -> tuple[str, int]:
    consumed = 0
    lines: list[bytes] = []
    with os.fdopen(os.dup(metadata_fd), "rb", buffering=0) as stream:
        opening = stream.readline(max_frontmatter_bytes + 1)
        consumed += len(opening)
        if opening not in {b"---\n", b"---\r\n"}:
            raise SkillMetadataError(
                "invalid-frontmatter",
                "SKILL.md must start with a YAML frontmatter delimiter",
                bytes_read=consumed,
            )
        while True:
            remaining = max_frontmatter_bytes - consumed
            line = stream.readline(max(0, remaining) + 1)
            consumed += len(line)
            if consumed > max_frontmatter_bytes:
                raise SkillMetadataError(
                    "invalid-frontmatter",
                    f"SKILL.md frontmatter exceeds {max_frontmatter_bytes} bytes",
                    bytes_read=consumed,
                )
            if not line:
                raise SkillMetadataError(
                    "invalid-frontmatter",
                    "SKILL.md has no closing YAML frontmatter delimiter",
                    bytes_read=consumed,
                )
            if line in {b"---\n", b"---\r\n", b"---"}:
                break
            lines.append(line)
    try:
        return b"".join(lines).decode("utf-8"), consumed
    except UnicodeDecodeError as exc:
        raise SkillMetadataError(
            "invalid-frontmatter",
            "SKILL.md frontmatter is not valid UTF-8",
            bytes_read=consumed,
        ) from exc


def _parse_entry(
    frontmatter: str,
    *,
    source: SkillSource,
    skill_root: Path,
    directory_name: str,
    skill_file: Path,
) -> tuple[SkillEntry, tuple[SkillDiagnostic, ...]]:
    try:
        for token in yaml.scan(frontmatter):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise SkillMetadataError(
                    "invalid-yaml",
                    "YAML aliases, anchors, and explicit tags are not supported",
                )
        raw: Any = yaml.load(frontmatter, Loader=_StrictSafeLoader)
    except SkillMetadataError:
        raise
    except RecursionError as exc:
        raise SkillMetadataError(
            "invalid-yaml",
            "YAML frontmatter exceeds the supported nesting depth",
        ) from exc
    except ValueError as exc:
        raise SkillMetadataError(
            "invalid-yaml",
            f"invalid YAML scalar value: {exc}",
        ) from exc
    except yaml.YAMLError as exc:
        raise SkillMetadataError("invalid-yaml", f"invalid YAML frontmatter: {exc}") from exc

    if not isinstance(raw, dict):
        raise SkillMetadataError("invalid-metadata", "frontmatter must be a YAML mapping")
    if not all(type(key) is str for key in raw):
        raise SkillMetadataError("invalid-metadata", "frontmatter keys must be strings")

    name = _required_string(raw, "name", strip=False)
    if len(name) > 64 or _SKILL_NAME.fullmatch(name) is None:
        raise SkillMetadataError(
            "invalid-metadata",
            "name must be 1-64 lowercase letters, digits, or single hyphen-separated words",
        )
    if name != directory_name:
        raise SkillMetadataError(
            "invalid-metadata",
            f"declared name {name!r} does not match parent directory {directory_name!r}",
        )

    description = _required_string(raw, "description")
    if len(description) > 1024:
        raise SkillMetadataError("invalid-metadata", "description exceeds 1024 characters")

    license_name = _optional_string(raw, "license")
    compatibility = _optional_string(raw, "compatibility")
    if compatibility is not None and len(compatibility) > 500:
        raise SkillMetadataError("invalid-metadata", "compatibility exceeds 500 characters")
    allowed_tools = _optional_string(raw, "allowed-tools")
    metadata_value = raw.get("metadata", _MISSING)
    metadata = () if metadata_value is _MISSING else _metadata_items(metadata_value)

    diagnostics = tuple(
        SkillDiagnostic(
            code="unsupported-field",
            severity="warning",
            message=f"unsupported frontmatter field {field!r} was ignored",
            source=source,
            path=skill_file,
        )
        for field in sorted(set(raw) - _SUPPORTED_FIELDS)
    )
    return (
        SkillEntry(
            name=name,
            description=description,
            source=source,
            root=skill_root,
            license=license_name,
            compatibility=compatibility,
            metadata=metadata,
            allowed_tools=allowed_tools,
        ),
        diagnostics,
    )


def _required_string(raw: dict[object, object], field: str, *, strip: bool = True) -> str:
    value = raw.get(field)
    if type(value) is not str or not value.strip():
        raise SkillMetadataError(
            "invalid-metadata",
            f"{field} is required and must be a non-empty string",
        )
    return value.strip() if strip else value


def _optional_string(raw: dict[object, object], field: str) -> str | None:
    value = raw.get(field, _MISSING)
    if value is _MISSING:
        return None
    if type(value) is not str or not value.strip():
        raise SkillMetadataError(
            "invalid-metadata",
            f"{field} must be a non-empty string when provided",
        )
    return value.strip()


def _metadata_items(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise SkillMetadataError("invalid-metadata", "metadata must be a string mapping")
    items: list[tuple[str, str]] = []
    for key, item in value.items():
        if type(key) is not str or type(item) is not str:
            raise SkillMetadataError("invalid-metadata", "metadata must map strings to strings")
        items.append((key, item))
    return tuple(sorted(items))
