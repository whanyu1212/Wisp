"""Pure tests for compact, literal tool-call header formatting."""

from __future__ import annotations

from wisp.tui.tool_call import format_tool_call_arguments


def _plain(name: str, arguments: object) -> str:
    return format_tool_call_arguments(name, arguments).plain


def test_read_header_formats_line_range() -> None:
    assert _plain("read", {"path": "src/app.py", "offset": 20, "limit": 40}) == ("src/app.py:20-59")
    assert _plain("read", {"path": "src/app.py", "limit": 5}) == "src/app.py:1-5"
    assert _plain("read", {"path": "src/app.py", "offset": 20}) == "src/app.py:20-"


def test_grep_header_formats_query_location_and_nondefault_options() -> None:
    rendered = _plain(
        "grep",
        {
            "pattern": "TODO",
            "path": "src",
            "glob": "*.py",
            "ignore_case": True,
            "literal": True,
            "context": 2,
            "max_results": 25,
        },
    )

    assert rendered == ("/TODO/ in src (*.py) · ignore case · literal · context 2 · limit 25")


def test_find_and_ls_headers_are_command_like() -> None:
    assert _plain("find", {"pattern": "*.py", "path": "src"}) == "*.py in src"
    assert _plain("find", {}) == "* in ."
    assert _plain("ls", {"path": "src", "all": True}) == "src · hidden"


def test_bash_header_is_one_line_and_bounded() -> None:
    command = "pytest tests\npython -m build\t--wheel"
    rendered = _plain("bash", {"command": command, "timeout": 30})

    assert rendered == "pytest tests ↵ python -m build ⇥ --wheel · timeout 30s"
    assert "\n" not in rendered
    assert "\t" not in rendered
    assert len(_plain("bash", {"command": "x" * 200})) == 64
    assert _plain("bash", {"command": "x" * 200}).endswith("…")


def test_bash_managed_process_headers_keep_operation_and_process_identity() -> None:
    assert (
        _plain(
            "bash",
            {
                "operation": "start",
                "command": "pytest",
                "lifetime_seconds": 300,
                "yield_seconds": 0.5,
            },
        )
        == "start pytest · lifetime 300s · yield 0.5s"
    )
    assert (
        _plain(
            "bash",
            {"operation": "poll", "process_id": "proc-1", "wait_seconds": 2},
        )
        == "poll proc-1 · wait 2s"
    )
    assert (
        _plain(
            "bash",
            {"operation": "cancel", "process_id": "proc-1"},
        )
        == "cancel proc-1"
    )


def test_built_in_headers_bound_extreme_numeric_values_without_raising() -> None:
    huge = 10**400

    started = _plain(
        "bash",
        {"operation": "start", "command": "run", "lifetime_seconds": huge},
    )
    polled = _plain(
        "bash",
        {"operation": "poll", "process_id": "proc-1", "wait_seconds": huge},
    )
    timed = _plain("bash", {"command": "run", "timeout": huge})

    assert len(started) <= 200
    assert len(polled) <= 200
    assert len(timed) == 200
    assert started.endswith("…s")
    assert polled.endswith("…s")
    assert timed.endswith("…")
    assert "nan" not in _plain(
        "bash",
        {"operation": "start", "command": "run", "lifetime_seconds": float("nan")},
    )


def test_edit_and_write_headers_never_include_file_payloads() -> None:
    injected = "[red]secret[/red]"
    edit = format_tool_call_arguments(
        "edit",
        {
            "path": "src/app.py",
            "edits": [{"oldText": injected, "newText": "replacement"}],
        },
    )
    write = format_tool_call_arguments(
        "write",
        {"path": "src/new.py", "content": injected},
    )

    assert edit.plain == "src/app.py · 1 edit"
    assert write.plain == "src/new.py"
    assert injected not in edit.plain
    assert injected not in write.plain


def test_long_path_keeps_both_ends() -> None:
    path = "root/" + "nested/" * 20 + "important.py"
    rendered = _plain("read", {"path": path})

    assert len(rendered) == 80
    assert rendered.startswith("root/")
    assert rendered.endswith("important.py")
    assert "…" in rendered


def test_extension_fallback_is_bounded_and_literal() -> None:
    rendered = format_tool_call_arguments(
        "extension",
        {"query": "[red]literal[/red]", "blob": "x" * 100},
    )

    assert rendered.plain.startswith("query=[red]literal[/red], blob=")
    assert rendered.plain.endswith("…")
    assert all("red" not in str(span.style).lower() for span in rendered.spans)


def test_extension_fallback_is_single_line_and_bounded_as_a_whole() -> None:
    rendered = _plain(
        "extension",
        {f"key-{index}\nspoof": "value\n" + "x" * 60 for index in range(20)},
    )

    assert "\n" not in rendered
    assert len(rendered) == 160
    assert rendered.endswith("…")


def test_non_mapping_arguments_degrade_to_a_bounded_literal() -> None:
    rendered = format_tool_call_arguments("extension", "[red]" + "x" * 100)

    assert len(rendered.plain) == 64
    assert rendered.plain.startswith("[red]")
    assert all("red" not in str(span.style).lower() for span in rendered.spans)
