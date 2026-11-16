"""Declare what an MCP tool with an output schema returns to a codemode script.

A script calls each tool as an async function. A tool that declares an output
schema returns its structured result, typed in the stubs the script is checked
against, so a field the schema lacks fails before the script runs. Every other
tool returns its text. Schemas come from MCP servers, so nothing in one is put
into the stubs unquoted, and large or odd schemas fall back to `Any`.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from typing import Literal

from typing_extensions import TypeAliasType

# The imports every stub file starts with, for the declarations below.
STUB_IMPORTS = "from typing import Any, NotRequired, TypedDict\n"

# How deep a schema is followed, and how many dicts one tool may declare, before
# the rest is typed `Any`. They keep a hostile schema from bloating the stubs.
_MAX_DEPTH = 6
_MAX_DICTS = 32
# The most top-level fields the codemode description names for one tool, and the
# longest name it shows: names come from the server, unbounded.
_MAX_LISTED_FIELDS = 12
_MAX_LISTED_FIELD_CHARS = 40

_SCALARS: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "null": "None",
}

ResultKind = TypeAliasType("ResultKind", Literal["dict", "unwrapped"])


@dataclass(frozen=True, slots=True)
class ResultType:
    """What a tool's structured result is, as a script receives it.

    Attributes:
        kind (ResultKind): ``dict`` returns the structured result itself;
            ``unwrapped`` returns its `result` field, for a schema that only wraps
            one value, as the MCP Python SDK does for a tool returning a list.
        annotation (str): The return type the stub declares.
        declarations (tuple[str, ...]): The `TypedDict` declarations it needs.
        summary (str): A short description for the codemode tool's description.
    """

    kind: ResultKind
    annotation: str
    declarations: tuple[str, ...]
    summary: str


def result_type(
    tool_name: str, schema: Mapping[str, object] | None, *, taken: Set[str]
) -> ResultType | None:
    """Return how ``tool_name``'s structured result is typed, or None to return text.

    A tool returns text when it declares no output schema, or one that only wraps a
    string in a `result` field: the MCP Python SDK gives every tool returning a
    string that schema, and its text is the same string.

    Args:
        tool_name (str): The tool's name as a script calls it.
        schema (Mapping[str, object] | None): Its output schema, already copied and
            checked to be a valid JSON Schema object.
        taken (Set[str]): Names the stubs already use, which a declaration must
            not reuse.

    Returns:
        ResultType | None: The return type and its declarations, or None.
    """

    if schema is None:
        return None
    builder = _Builder(f"{tool_name}_result", taken)
    wrapped = _wrapped_value(schema)
    if wrapped is not None:
        if wrapped.get("type") == "string":
            return None
        annotation = builder.annotation(wrapped, depth=0)
        return ResultType(
            kind="unwrapped",
            annotation=annotation,
            declarations=tuple(builder.declarations),
            summary=_short(annotation),
        )
    annotation = builder.annotation({**schema, "type": "object"}, depth=0)
    fields = [
        name
        for name in _properties(schema)
        if isinstance(name, str) and name.isprintable() and len(name) <= _MAX_LISTED_FIELD_CHARS
    ]
    listed = ", ".join(fields[:_MAX_LISTED_FIELDS])
    more = ", …" if len(fields) > _MAX_LISTED_FIELDS else ""
    return ResultType(
        kind="dict",
        annotation=annotation,
        declarations=tuple(builder.declarations),
        summary=f"a dict: {listed}{more}" if fields else "a dict",
    )


def script_receives(tool_name: str, schema: Mapping[str, object] | None) -> str | None:
    """Say what a codemode script calling ``tool_name`` by name receives, or None.

    Used where the model reads a tool's output schema, as `mcp_describe` shows it,
    so it knows whether a script gets text, a dict, or an unwrapped value.
    """

    if schema is None:
        return None
    returns = result_type(tool_name, schema, taken=frozenset())
    if returns is None:
        return "A codemode script calling it by name receives its text."
    if returns.kind == "unwrapped":
        return (
            f"A codemode script calling it by name receives the `result` value: {returns.summary}."
        )
    return f"A codemode script calling it by name receives {returns.summary}, not text."


def _wrapped_value(schema: Mapping[str, object]) -> Mapping[str, object] | None:
    """The schema of the one `result` field ``schema`` wraps, or None."""

    properties = _properties(schema)
    required = schema.get("required")
    if list(properties) != ["result"] or not isinstance(required, list):
        return None
    value = properties["result"]
    return value if "result" in required and isinstance(value, Mapping) else None


def _properties(schema: Mapping[str, object]) -> Mapping[str, object]:
    properties = schema.get("properties")
    return properties if isinstance(properties, Mapping) else {}


def _short(annotation: str) -> str:
    return annotation if len(annotation) <= 60 else "a structured value"


class _Builder:
    """Translates one tool's schema into an annotation and `TypedDict` declarations."""

    def __init__(self, base: str, taken: Set[str]) -> None:
        self._base = base
        self._taken = taken
        self.declarations: list[str] = []

    def annotation(self, schema: object, *, depth: int) -> str:
        """The Python type for ``schema``; `Any` where it cannot say more."""

        if not isinstance(schema, Mapping) or depth > _MAX_DEPTH:
            return "Any"
        kinds = schema.get("type")
        if isinstance(kinds, list):
            members = [self._kind(kind, schema, depth) for kind in kinds]
            return "Any" if "Any" in members or not members else " | ".join(dict.fromkeys(members))
        # Without a `type`, a schema with `properties` still accepts any value, so it
        # promises nothing a script could rely on.
        return self._kind(kinds, schema, depth)

    def _kind(self, kind: object, schema: Mapping[str, object], depth: int) -> str:
        if not isinstance(kind, str):
            return "Any"
        if kind in _SCALARS:
            return _SCALARS[kind]
        if kind == "array":
            # Positional items (`prefixItems`, or a draft-4 list) differ per index.
            if "prefixItems" in schema or isinstance(schema.get("items"), list):
                return "list[Any]"
            return f"list[{self.annotation(schema.get('items'), depth=depth + 1)}]"
        if kind == "object":
            return self._dict(schema, depth)
        return "Any"

    def _dict(self, schema: Mapping[str, object], depth: int) -> str:
        properties = _properties(schema)
        # A schema that explicitly admits other keys cannot be a closed `TypedDict`
        # without rejecting keys a valid result may carry.
        admits_other_keys = "patternProperties" in schema or schema.get(
            "additionalProperties", False
        ) not in (False, None)
        if not properties or admits_other_keys or len(self.declarations) >= _MAX_DICTS:
            return "dict[str, Any]"
        name = self._name()
        # Reserved before the fields are translated, so nested dicts number after it.
        self.declarations.append("")
        index = len(self.declarations) - 1
        required = schema.get("required")
        # Older drafts nest lists in `required`; only names count.
        required_names = (
            {name for name in required if isinstance(name, str)}
            if isinstance(required, list)
            else set()
        )
        fields = []
        for key, value in properties.items():
            if not isinstance(key, str):
                continue
            field = self.annotation(value, depth=depth + 1)
            if key not in required_names:
                field = f"NotRequired[{field}]"
            # `repr` quotes the key, so no key can change the declaration's code.
            fields.append(f"{key!r}: {field}")
        self.declarations[index] = f"{name} = TypedDict({name!r}, {{{', '.join(fields)}}})"
        return name

    def _name(self) -> str:
        count = len(self.declarations)
        name = self._base if count == 0 else f"{self._base}_{count}"
        while name in self._taken:
            name = f"_{name}"
        return name
