"""Passive composite project-file picker for inline ``@`` references.

The picker owns one mention session and projects one immutable project snapshot into
fuzzy and tree presentations.  Neither presentation is focusable; the prompt editor
remains the authoritative draft and caret owner.  Tree expansion only consults the
snapshot adjacency captured by :mod:`wisp.tui.file_index` and never touches the
filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.content import Content, Span
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from wisp.tui.file_index import (
    ProjectDirectory,
    ProjectEntry,
    ProjectSnapshot,
    ScoredPath,
    filter_paths,
)
from wisp.tui.rendering import _truncate_to_cell_width


class FilePickerMode(StrEnum):
    """The active project-file presentation."""

    FUZZY = "fuzzy"
    TREE = "tree"


@dataclass(frozen=True)
class FilePickerActivation:
    """Result of activating the authoritative selection."""

    handled: bool
    insertion_path: str | None = None


class _PassiveOptions(OptionList):
    """An OptionList which can receive mouse events without taking editor focus."""

    can_focus = False


class FileSuggest(Vertical):
    """Inline ``@`` picker with passive fuzzy and snapshot-tree presentations."""

    can_focus = False

    DEFAULT_CSS = """
    FileSuggest {
        overlay: screen;
        constrain: inside;
        display: none;
        width: auto;
        max-width: 72;
        height: auto;
        max-height: 11;
        offset: 0 -100%;
        border: round $accent;
        background: $background;
        padding: 0 1;
    }
    FileSuggest .file-picker-header {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    FileSuggest .file-picker-options {
        width: 1fr;
        height: auto;
        max-height: 7;
        padding: 0;
        scrollbar-size-vertical: 1;
        background: $background;
    }
    FileSuggest .file-picker-options > .option-list--option-highlighted {
        background: transparent;
        color: $accent;
        text-style: bold;
    }
    FileSuggest .file-picker-status {
        width: 1fr;
        height: auto;
        max-height: 2;
        color: $text-muted;
    }
    """

    _MAX_WIDTH_CEILING = 72

    class ActivationRequested(Message):
        """A mouse row requested activation through the app's shared seam."""

        def __init__(self, path: str) -> None:
            super().__init__()
            self.path = path

    def __init__(self, id: str | None = None) -> None:  # noqa: A002 - Textual's param name
        super().__init__(id=id)
        self._snapshot: ProjectSnapshot | None = None
        self._entries: dict[str, ProjectEntry] = {}
        self._corpus: tuple[str, ...] = ()
        self._children: dict[str, tuple[str, ...]] = {}
        self._expanded: set[str] = set()
        # This is the cached projection for the current immutable corpus/query.
        # It is refreshed only when either input changes, avoiding duplicate scoring
        # during selection repair and rendering.
        self._visible_fuzzy: tuple[ScoredPath, ...] = ()
        self._visible_tree: tuple[str, ...] = ()
        self._mode = FilePickerMode.FUZZY
        self._query = ""
        self._mention_active = False
        self._dismissed = False
        self._suspended = False
        self._selected_path: str | None = None
        self._max_width = self._MAX_WIDTH_CEILING
        self._header: Static | None = None
        self._fuzzy: _PassiveOptions | None = None
        self._tree: _PassiveOptions | None = None
        self._status: Static | None = None

    def compose(self) -> ComposeResult:
        yield Static(classes="file-picker-header", id="file-picker-header")
        yield _PassiveOptions(classes="file-picker-options", id="file-picker-fuzzy")
        yield _PassiveOptions(classes="file-picker-options", id="file-picker-tree")
        yield Static(classes="file-picker-status", id="file-picker-status")

    def on_mount(self) -> None:
        self._header = self.query_one("#file-picker-header", Static)
        self._fuzzy = self.query_one("#file-picker-fuzzy", _PassiveOptions)
        self._tree = self.query_one("#file-picker-tree", _PassiveOptions)
        self._status = self.query_one("#file-picker-status", Static)
        self._update_max_width()
        self._render_presentations()

    def on_resize(self, event: events.Resize) -> None:
        self._update_max_width()
        self._render_presentations()

    def _update_max_width(self) -> None:
        self._max_width = min(self._MAX_WIDTH_CEILING, max(1, self.screen.size.width - 4))
        self.styles.max_width = self._max_width

    @staticmethod
    def query_from_value(value: str, cursor: int) -> str | None:
        """Return the cursor-relative path fragment after ``@``, if one is active."""

        if cursor < 0 or cursor > len(value):
            return None
        head = value[:cursor]
        at_index = head.rfind("@")
        if at_index == -1:
            return None
        if at_index > 0 and not value[at_index - 1].isspace():
            return None
        fragment = head[at_index + 1 :]
        if any(character.isspace() for character in fragment):
            return None
        return fragment

    def set_snapshot(self, snapshot: ProjectSnapshot | None) -> None:
        """Atomically replace both projections and repair the active selection."""

        old_selection = self._selected_path
        self._snapshot = snapshot
        self._entries = (
            {entry.display_path: entry for entry in snapshot.entries}
            if snapshot is not None
            else {}
        )
        self._corpus = tuple(self._entries)
        self._children = self._snapshot_children(snapshot)
        valid_directories = {
            entry.path for entry in self._entries.values() if isinstance(entry, ProjectDirectory)
        }
        self._expanded.intersection_update(valid_directories)
        self._refresh_fuzzy_projection()

        if self._mode is FilePickerMode.FUZZY:
            candidates = tuple(match.path for match in self._visible_fuzzy)
        else:
            if old_selection in self._entries:
                self._reveal(old_selection)
            candidates = self._tree_projection()
        self._selected_path = (
            old_selection if old_selection in candidates else next(iter(candidates), None)
        )
        self._render_presentations()

    @staticmethod
    def _snapshot_children(snapshot: ProjectSnapshot | None) -> dict[str, tuple[str, ...]]:
        if snapshot is None:
            return {}
        if snapshot.child_adjacency:
            return {row.parent: row.children for row in snapshot.child_adjacency}

        # Compatibility for typed snapshots assembled by embedded callers: derive
        # adjacency from immutable entry paths, never from disk.
        children: dict[str, list[str]] = {"": []}
        for entry in snapshot.entries:
            raw_path = entry.path
            parent = raw_path.rpartition("/")[0]
            children.setdefault(parent, []).append(raw_path)
            if isinstance(entry, ProjectDirectory):
                children.setdefault(raw_path, [])
        return {parent: tuple(sorted(paths)) for parent, paths in children.items()}

    @property
    def snapshot(self) -> ProjectSnapshot | None:
        return self._snapshot

    @property
    def has_paths(self) -> bool:
        return bool(self._entries)

    @property
    def mode(self) -> FilePickerMode:
        return self._mode

    @property
    def is_tree_mode(self) -> bool:
        return self._mode is FilePickerMode.TREE

    @property
    def current_query(self) -> str:
        return self._query

    @property
    def mention_active(self) -> bool:
        return self._mention_active

    @property
    def selected_path(self) -> str | None:
        return self._selected_path

    @property
    def is_active(self) -> bool:
        """Whether contextual picker keys belong to this mention session."""

        return self._mention_active and not self._dismissed and not self._suspended

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    @property
    def option_count(self) -> int:
        active = self._active_options
        return active.option_count if active is not None else 0

    @property
    def visible_paths(self) -> tuple[str, ...]:
        if self._mode is FilePickerMode.FUZZY:
            return tuple(match.path for match in self._visible_fuzzy)
        return self._visible_tree

    def show_for(self, value: str, cursor: int) -> int:
        """Update mention/query state and render the active passive presentation."""

        query = self.query_from_value(value, cursor)
        if query is None:
            self.end_mention()
            return 0

        new_session = not self._mention_active
        query_changed = query != self._query
        if new_session:
            self._mode = FilePickerMode.FUZZY
            self._dismissed = False
            self._suspended = False
        elif query_changed:
            self._dismissed = False
            self._suspended = False
        self._mention_active = True
        self._query = query

        if query_changed:
            self._refresh_fuzzy_projection()
        matches = self._visible_fuzzy
        if query_changed and self._selected_path not in {match.path for match in matches}:
            self._selected_path = matches[0].path if matches else self._tree_fallback()
        elif self._selected_path is None:
            self._selected_path = matches[0].path if matches else self._tree_fallback()

        self._render_presentations()
        if not self._dismissed:
            self.display = True
        return len(matches)

    def end_mention(self) -> None:
        """Close and reset contextual mention state without changing the editor draft."""

        self._mention_active = False
        self._dismissed = False
        self._suspended = False
        self._query = ""
        self.display = False

    def hide(self) -> None:
        """Suspend the picker while an overlay owns the composer."""

        self._suspended = True
        self.display = False

    def dismiss(self) -> None:
        """Dismiss this mention until its query changes, preserving the draft."""

        self._dismissed = True
        self.display = False

    def toggle_mode(self) -> None:
        if not self.is_active:
            return
        self._mode = (
            FilePickerMode.TREE if self._mode is FilePickerMode.FUZZY else FilePickerMode.FUZZY
        )
        if self._mode is FilePickerMode.TREE:
            if self._selected_path is not None:
                self._reveal(self._selected_path)
            candidates = self._tree_projection()
        else:
            candidates = tuple(match.path for match in self._visible_fuzzy)
        if self._selected_path not in candidates:
            self._selected_path = next(iter(candidates), None)
        self._render_presentations()
        self.display = True

    def move_selection(self, direction: int) -> None:
        options = self._active_options
        if not self.is_active or options is None or options.option_count == 0:
            return
        if options.highlighted is None:
            options.highlighted = 0 if direction >= 0 else options.option_count - 1
        elif direction < 0:
            options.action_cursor_up()
        else:
            options.action_cursor_down()
        self._sync_selected(options)

    def move_tree_horizontal(self, *, expand: bool) -> None:
        """Expand or collapse the selected tree directory without filesystem I/O."""

        if not self.is_active or self._mode is not FilePickerMode.TREE:
            return
        selected = self._selected_path
        entry = self._entries.get(selected or "")
        if not isinstance(entry, ProjectDirectory):
            return
        raw_path = entry.path
        if expand:
            self._expanded.add(raw_path)
        else:
            self._expanded.discard(raw_path)
        self._render_tree()

    def activate(self, requested_path: str | None = None) -> FilePickerActivation:
        """Activate a row for keyboard or mouse through one semantic seam."""

        if not self.is_active:
            return FilePickerActivation(False)
        if requested_path is not None:
            if requested_path not in self._entries or requested_path not in self.visible_paths:
                return FilePickerActivation(False)
            self._selected_path = requested_path
        selected = self._selected_path
        if requested_path is None and selected not in self.visible_paths:
            # A preserved cross-mode selection may not match the current fuzzy
            # query. It remains authoritative state, but an invisible row cannot
            # be activated until navigation selects a visible result.
            return FilePickerActivation(False)
        entry = self._entries.get(selected or "")
        if entry is None:
            return FilePickerActivation(False)
        if self._mode is FilePickerMode.TREE and isinstance(entry, ProjectDirectory):
            raw_path = entry.path
            if raw_path in self._expanded:
                self._expanded.remove(raw_path)
            else:
                self._expanded.add(raw_path)
            self._render_tree()
            return FilePickerActivation(True)
        return FilePickerActivation(True, selected)

    def highlighted_path(self) -> str | None:
        """Compatibility alias for the single authoritative selection."""

        return self._selected_path

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list not in {self._fuzzy, self._tree}:
            return
        self._sync_selected(event.option_list)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list not in {self._fuzzy, self._tree}:
            return
        event.stop()
        path = event.option.id
        if path is None:
            return
        self.post_message(self.ActivationRequested(path))

    @property
    def _active_options(self) -> _PassiveOptions | None:
        return self._fuzzy if self._mode is FilePickerMode.FUZZY else self._tree

    def _sync_selected(self, options: OptionList) -> None:
        highlighted = options.highlighted
        if highlighted is None or highlighted >= options.option_count:
            return
        path = options.get_option_at_index(highlighted).id
        if path is not None:
            self._selected_path = path

    def _tree_fallback(self) -> str | None:
        root_children = self._children.get("", ())
        for raw_path in root_children:
            display_path = self._display_path(raw_path)
            if display_path is not None:
                return display_path
        return next(iter(self._entries), None)

    def _display_path(self, raw_path: str) -> str | None:
        directory_path = f"{raw_path}/"
        if directory_path in self._entries:
            return directory_path
        return raw_path if raw_path in self._entries else None

    def _refresh_fuzzy_projection(self) -> None:
        """Score the current immutable corpus exactly once per corpus/query change."""

        self._visible_fuzzy = filter_paths(self._corpus, self._query)

    def _tree_rows(self) -> list[tuple[str, int]]:
        rows: list[tuple[str, int]] = []

        def visit(parent: str, depth: int) -> None:
            for raw_path in self._children.get(parent, ()):
                display_path = self._display_path(raw_path)
                if display_path is None:
                    continue
                rows.append((display_path, depth))
                entry = self._entries[display_path]
                if isinstance(entry, ProjectDirectory) and raw_path in self._expanded:
                    visit(raw_path, depth + 1)

        visit("", 0)
        return rows

    def _tree_projection(self) -> tuple[str, ...]:
        return tuple(path for path, _depth in self._tree_rows())

    def _reveal(self, display_path: str) -> None:
        raw_path = display_path.rstrip("/")
        parts = raw_path.split("/")
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if f"{ancestor}/" in self._entries:
                self._expanded.add(ancestor)

    def _render_presentations(self) -> None:
        if (
            self._header is None
            or self._fuzzy is None
            or self._tree is None
            or self._status is None
        ):
            return
        self._render_fuzzy()
        if self._mode is FilePickerMode.TREE and self._selected_path is not None:
            self._reveal(self._selected_path)
        self._render_tree()
        self._fuzzy.display = self._mode is FilePickerMode.FUZZY
        self._tree.display = self._mode is FilePickerMode.TREE
        mode_name = "Fuzzy" if self._mode is FilePickerMode.FUZZY else "Tree"
        other_name = "Tree" if self._mode is FilePickerMode.FUZZY else "Fuzzy"
        self._header.update(f"[{mode_name}]  Tab: {other_name}")
        self._status.update(self._status_text())

    def _render_fuzzy(self) -> None:
        fuzzy = self._fuzzy
        if fuzzy is None:
            return
        matches = self._visible_fuzzy
        fuzzy.clear_options()
        content_width = max(1, self._max_width - 4)
        if matches:
            fuzzy.add_options(
                [
                    Option(self._render_fuzzy_path(match, content_width), id=match.path)
                    for match in matches
                ]
            )
        selected_index = next(
            (index for index, match in enumerate(matches) if match.path == self._selected_path),
            None,
        )
        fuzzy.highlighted = selected_index

    def _render_tree(self) -> None:
        tree = self._tree
        if tree is None:
            return
        rows = self._tree_rows()
        self._visible_tree = tuple(path for path, _depth in rows)
        tree.clear_options()
        width = max(1, self._max_width - 4)
        tree.add_options(
            [Option(self._render_tree_path(path, depth, width), id=path) for path, depth in rows]
        )
        selected_index = next(
            (index for index, (path, _depth) in enumerate(rows) if path == self._selected_path),
            None,
        )
        tree.highlighted = selected_index

    def _render_fuzzy_path(self, match: ScoredPath, width: int) -> Content:
        text = _truncate_to_cell_width(match.path, width)
        content = Content(text)
        spans = [
            Span(offset, offset + 1, "underline") for offset in match.offsets if offset < len(text)
        ]
        return content.add_spans(spans) if spans else content

    def _render_tree_path(self, path: str, depth: int, width: int) -> str:
        entry = self._entries[path]
        prefix = "  " * depth
        if isinstance(entry, ProjectDirectory):
            marker = "▾ " if entry.path in self._expanded else "▸ "
            label = path.rsplit("/", 2)[-2] + "/"
        else:
            marker = "  "
            label = path.rsplit("/", 1)[-1]
        return _truncate_to_cell_width(f"{prefix}{marker}{label}", width)

    def _status_text(self) -> str:
        snapshot = self._snapshot
        if snapshot is None:
            return "Indexing project… (tree remains available)"
        notes: list[str] = []
        if self._mode is FilePickerMode.FUZZY and not self._visible_fuzzy:
            notes.append("No fuzzy matches")
        elif self._mode is FilePickerMode.TREE and not self._visible_tree:
            notes.append("No indexed entries")
        if snapshot.truncation.entry_limit_reached:
            notes.append("entry limit reached; indexed view may omit entries")
        if snapshot.truncation.depth_limit_reached:
            notes.append("depth limit reached; descendants may be omitted")
        return " · ".join(notes)
