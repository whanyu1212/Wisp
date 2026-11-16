"""How an MCP tool's output schema becomes the type a codemode script sees."""

from __future__ import annotations

import ast

import pytest

from wisp.tools.codemode.result_types import STUB_IMPORTS, result_type


def _wrapped(value: dict[str, object]) -> dict[str, object]:
    return {"type": "object", "properties": {"result": value}, "required": ["result"]}


@pytest.mark.parametrize(
    "schema",
    [None, _wrapped({"type": "string"})],
    ids=["no-schema", "wrapped-string"],
)
def test_text_tools_have_no_result_type(schema: dict[str, object] | None) -> None:
    assert result_type("t", schema, taken=frozenset()) is None


def test_wrapped_value_is_unwrapped() -> None:
    returns = result_type(
        "t", _wrapped({"type": "array", "items": {"type": "integer"}}), taken=frozenset()
    )

    assert returns is not None
    assert (returns.kind, returns.annotation, returns.declarations) == (
        "unwrapped",
        "list[int]",
        (),
    )


def test_object_becomes_typed_dict() -> None:
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "owner": {"type": "object", "properties": {"login": {"type": "string"}}},
            "tags": {"type": ["array", "null"], "items": {"type": "string"}},
        },
        "required": ["id", "owner"],
    }

    returns = result_type("t", schema, taken=frozenset())

    assert returns is not None
    assert returns.annotation == "t_result"
    assert returns.declarations == (
        "t_result = TypedDict('t_result', {'id': int, 'owner': t_result_1, "
        "'tags': NotRequired[list[str] | None]})",
        "t_result_1 = TypedDict('t_result_1', {'login': NotRequired[str]})",
    )
    assert returns.summary == "a dict: id, owner, tags"


def test_hostile_schema_stays_valid_python() -> None:
    """Keys are quoted, and depth and size fall back to `Any`, so stubs always parse."""

    deep: dict[str, object] = {"type": "object", "properties": {"x": {"type": "integer"}}}
    for _ in range(20):
        deep = {"type": "object", "properties": {"n": deep}}
    schema = {
        "type": "object",
        "properties": {
            "a'}) \nimport os  #": {"type": "string"},
            "deep": deep,
            **{
                f"f{index}": {"type": "object", "properties": {"x": {"type": "integer"}}}
                for index in range(100)
            },
            "odd": {"anyOf": [{"type": "string"}], "$ref": "#"},
        },
    }

    returns = result_type("t", schema, taken=frozenset())

    assert returns is not None
    stubs = STUB_IMPORTS + "\n".join(returns.declarations)
    tree = ast.parse(stubs)
    # Only the import and assignments: no key escaped into code of its own.
    assert all(isinstance(node, ast.ImportFrom | ast.Assign) for node in tree.body)
    assert len(returns.declarations) == 32
    assert "'odd': NotRequired[Any]" in returns.declarations[0]
    assert '"a\'}) \\nimport os  #": NotRequired[str]' in returns.declarations[0]


def test_declared_names_avoid_taken_ones() -> None:
    schema = {"type": "object", "properties": {"id": {"type": "integer"}}}

    returns = result_type("t", schema, taken=frozenset({"t_result"}))

    assert returns is not None
    assert returns.annotation == "_t_result"


@pytest.mark.parametrize(
    ("field", "annotation"),
    [
        # No `type`: any value is valid, so the script is promised nothing.
        ({"properties": {"login": {"type": "string"}}}, "NotRequired[Any]"),
        (
            {"type": "array", "prefixItems": [{"type": "string"}], "items": {"type": "integer"}},
            "NotRequired[list[Any]]",
        ),
        ({"type": "array", "items": [{"type": "string"}]}, "NotRequired[list[Any]]"),
        # Other keys are valid, so a closed `TypedDict` would reject them.
        (
            {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "additionalProperties": {"type": "integer"},
            },
            "NotRequired[dict[str, Any]]",
        ),
        (
            {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "patternProperties": {"^y": {"type": "integer"}},
            },
            "NotRequired[dict[str, Any]]",
        ),
    ],
    ids=["untyped-object", "prefix-items", "draft-4-tuple", "additional", "pattern"],
)
def test_loose_schemas_promise_any(field: dict[str, object], annotation: str) -> None:
    schema = {"type": "object", "properties": {"f": field}}

    returns = result_type("t", schema, taken=frozenset())

    assert returns is not None
    assert returns.declarations[0] == f"t_result = TypedDict('t_result', {{'f': {annotation}}})"


def test_closed_object_stays_typed() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "additionalProperties": False,
    }

    returns = result_type("t", schema, taken=frozenset())

    assert returns is not None
    assert returns.annotation == "t_result"


def test_open_result_is_plain_dict() -> None:
    schema = {
        "type": "object",
        "properties": {"fixed": {"type": "integer"}},
        "additionalProperties": True,
    }

    returns = result_type("t", schema, taken=frozenset())

    assert returns is not None
    assert (returns.annotation, returns.declarations) == ("dict[str, Any]", ())


def test_odd_required_and_long_names_tolerated() -> None:
    """Older drafts nest lists in `required`; long or multi-line names stay out of prose."""

    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "x" * 41: {"type": "integer"}, "b\nc": {}},
        "required": [["a"], "a"],
    }

    returns = result_type("t", schema, taken=frozenset())

    assert returns is not None
    assert "'a': int," in returns.declarations[0]
    assert returns.summary == "a dict: a"
