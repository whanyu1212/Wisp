from __future__ import annotations

from rich.segment import ControlType, Segment
from rich.style import Style
from textual._compositor import ChopsUpdate, LayoutUpdate
from textual.geometry import Region, Size
from textual.strip import Strip

from wisp.tui.textual_app import _DisplayedFrame


def _strip(text: str, *, style: Style | None = None) -> Strip:
    return Strip([Segment(text, style)], len(text))


def _update(*rows: Strip) -> ChopsUpdate:
    width = rows[0].cell_count
    return ChopsUpdate(
        [{0: row} for row in rows],
        [(y, 0, width) for y in range(len(rows))],
        [[width] for _row in rows],
    )


def test_displayed_frame_materializes_a_full_layout() -> None:
    update = LayoutUpdate([[_strip("abc")], [_strip("def")]], Region(0, 0, 3, 2))

    materialized, frame = _DisplayedFrame.from_layout(update, size=Size(3, 2))

    assert isinstance(materialized, LayoutUpdate)
    assert frame is not None
    assert frame.rows == [_strip("abc"), _strip("def")]


def test_displayed_frame_reduces_full_layout_to_changed_rows() -> None:
    frame = _DisplayedFrame(size=Size(4, 2), rows=[_strip("same"), _strip("keep")])
    update = LayoutUpdate([[_strip("same")], [_strip("news")]], Region(0, 0, 4, 2))

    filtered, next_frame, fail_open = frame.filter_layout(
        update,
        size=Size(4, 2),
        allow_suppression=True,
    )

    assert isinstance(filtered, ChopsUpdate)
    assert filtered.spans == [(1, 0, 4)]
    assert next_frame is not None
    assert next_frame.rows == [_strip("same"), _strip("news")]
    assert not fail_open


def test_displayed_frame_drops_duplicate_full_layout() -> None:
    frame = _DisplayedFrame(size=Size(4, 1), rows=[_strip("same")])
    update = LayoutUpdate([[_strip("same")]], Region(0, 0, 4, 1))

    filtered, next_frame, fail_open = frame.filter_layout(
        update,
        size=Size(4, 1),
        allow_suppression=True,
    )

    assert filtered is None
    assert next_frame is not None
    assert next_frame.rows == [_strip("same")]
    assert not fail_open


def test_displayed_frame_preserves_full_layout_when_cursor_moved() -> None:
    frame = _DisplayedFrame(size=Size(4, 1), rows=[_strip("same")])
    update = LayoutUpdate([[_strip("same")]], Region(0, 0, 4, 1))

    filtered, next_frame, fail_open = frame.filter_layout(
        update,
        size=Size(4, 1),
        allow_suppression=False,
    )

    assert isinstance(filtered, LayoutUpdate)
    assert next_frame is not None
    assert fail_open


def test_displayed_frame_drops_only_exact_duplicate_rows() -> None:
    frame = _DisplayedFrame(size=Size(4, 2), rows=[_strip("same"), _strip("keep")])
    update = _update(_strip("same"), _strip("news"))

    filtered, cache_valid, fail_open = frame.filter_chops(update, allow_suppression=True)

    assert cache_valid
    assert not fail_open
    assert isinstance(filtered, ChopsUpdate)
    assert filtered.spans == [(1, 0, 4)]
    assert frame.rows == [_strip("same"), _strip("news")]


def test_displayed_frame_reconstructs_a_sparse_partial_span() -> None:
    frame = _DisplayedFrame(size=Size(4, 1), rows=[_strip("same")])
    update = ChopsUpdate(
        [{0: None, 2: _strip("me")}],
        [(0, 2, 4)],
        [[2, 4]],
    )

    filtered, cache_valid, fail_open = frame.filter_chops(update, allow_suppression=True)

    assert cache_valid
    assert not fail_open
    assert filtered is None
    assert frame.rows == [_strip("same")]


def test_displayed_frame_preserves_duplicate_rows_when_cursor_moved() -> None:
    frame = _DisplayedFrame(size=Size(4, 1), rows=[_strip("same")])
    update = _update(_strip("same"))

    filtered, cache_valid, fail_open = frame.filter_chops(update, allow_suppression=False)

    assert cache_valid
    assert fail_open
    assert filtered is update


def test_displayed_frame_preserves_style_only_changes() -> None:
    frame = _DisplayedFrame(size=Size(3, 1), rows=[_strip("abc", style=Style(color="red"))])
    update = _update(_strip("abc", style=Style(color="blue")))

    filtered, cache_valid, fail_open = frame.filter_chops(update, allow_suppression=True)

    assert cache_valid
    assert not fail_open
    assert filtered is update
    assert frame.rows == [_strip("abc", style=Style(color="blue"))]


def test_displayed_frame_drops_interaction_metadata_only_changes() -> None:
    frame = _DisplayedFrame(
        size=Size(3, 1),
        rows=[_strip("abc", style=Style(meta={"offset": (0, 10)})).discard_meta()],
    )
    update = _update(_strip("abc", style=Style(meta={"offset": (0, 20)})))

    filtered, cache_valid, fail_open = frame.filter_chops(update, allow_suppression=True)

    assert cache_valid
    assert not fail_open
    assert filtered is None


def test_displayed_frame_preserves_equal_control_segments() -> None:
    control = Strip(
        [Segment("abc", control=[(ControlType.BELL,)])],
        3,
    )
    frame = _DisplayedFrame(size=Size(3, 1), rows=[control])
    update = _update(control)

    filtered, cache_valid, fail_open = frame.filter_chops(update, allow_suppression=True)

    assert cache_valid
    assert fail_open
    assert filtered is update


def test_displayed_frame_fails_open_for_incomplete_chops() -> None:
    frame = _DisplayedFrame(size=Size(4, 1), rows=[_strip("same")])
    update = ChopsUpdate([{0: _strip("sa")}], [(0, 0, 4)], [[2]])

    filtered, cache_valid, fail_open = frame.filter_chops(update, allow_suppression=True)

    assert not cache_valid
    assert fail_open
    assert filtered is update
