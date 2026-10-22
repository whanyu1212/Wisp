//! Bounded hit regions captured by the paint that users actually saw.

use crate::transcript::TranscriptEntryId;
use crate::{Error, Input, LiveUi, LoopControl, TranscriptViewAction, ViewStatus, WriterMessage};
use crate::{OverlayKind, prompt_editor::PromptEditor};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
use ratatui::buffer::Buffer;
use ratatui::layout::{Position, Rect};
use ratatui::style::Modifier;
use std::time::Instant;
use tokio::sync::mpsc;
use unicode_width::UnicodeWidthStr;

pub(crate) fn enabled(value: Option<&str>) -> bool {
    value.is_none_or(|value| {
        matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "on"
        )
    })
}

/// Left-button press, drag, and release, plus the wheel. Other buttons are dropped by the reader
/// so they never fill the input queue.
///
/// A modified press or wheel report is dropped too. A drag or release keeps its modifiers: it
/// only continues a selection an unmodified press started, and dropping a release because a key
/// was held would leave that selection live.
pub(crate) fn supported(event: MouseEvent) -> bool {
    match event.kind {
        MouseEventKind::Drag(MouseButton::Left) | MouseEventKind::Up(MouseButton::Left) => true,
        MouseEventKind::Down(MouseButton::Left)
        | MouseEventKind::ScrollUp
        | MouseEventKind::ScrollDown => event.modifiers.is_empty(),
        _ => false,
    }
}

/// A left-button drag over painted transcript text, in screen cells.
///
/// The selection runs like a terminal's: from the earlier end to the later one in reading order,
/// covering whole rows in between. It marks screen cells, not transcript rows: the caller drops it
/// on wheel scrolling, resizes, and layout changes, and a release copies whatever those cells
/// showed in the last painted frame.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Selection {
    area: Rect,
    anchor: Position,
    head: Position,
    /// The foldable row under the press, found in the frame the user clicked on, so a
    /// repaint before the release cannot retarget a plain click.
    fold_target: Option<FoldRow>,
}

impl Selection {
    /// Start a selection at `event` when it lands inside `area`.
    pub fn start(area: Rect, event: MouseEvent) -> Option<Self> {
        let at = Position::new(event.column, event.row);
        area.contains(at).then_some(Self {
            area,
            anchor: at,
            head: at,
            fold_target: None,
        })
    }

    /// Remember the foldable row a plain click on this press would fold or unfold.
    pub fn with_fold_target(mut self, target: Option<FoldRow>) -> Self {
        self.fold_target = target;
        self
    }

    /// The transcript text area the selection was started in.
    pub fn area(&self) -> Rect {
        self.area
    }

    /// Move the free end to `event`, clamped to the selectable area.
    ///
    /// Returns:
    ///     bool: Whether the selected cells changed.
    pub fn extend(&mut self, event: MouseEvent) -> bool {
        let head = Position::new(
            event
                .column
                .clamp(self.area.x, self.area.right().saturating_sub(1)),
            event
                .row
                .clamp(self.area.y, self.area.bottom().saturating_sub(1)),
        );
        let changed = head != self.head;
        self.head = head;
        changed
    }

    /// Whether the press and the current end are the same cell, as for a plain click.
    pub fn is_empty(&self) -> bool {
        self.anchor == self.head
    }

    /// The selected column span of screen row `y`, inclusive at both ends.
    fn columns(&self, y: u16) -> Option<(u16, u16)> {
        let (start, end) = if (self.anchor.y, self.anchor.x) <= (self.head.y, self.head.x) {
            (self.anchor, self.head)
        } else {
            (self.head, self.anchor)
        };
        if self.is_empty() || y < start.y || y > end.y {
            return None;
        }
        let first = if y == start.y { start.x } else { self.area.x };
        let last = if y == end.y {
            end.x
        } else {
            self.area.right().saturating_sub(1)
        };
        Some((first, last))
    }

    /// Show the selected cells in reverse video.
    pub fn highlight(&self, buffer: &mut Buffer) {
        for y in self.area.y..self.area.bottom() {
            let Some((first, last)) = self.columns(y) else {
                continue;
            };
            for x in first..=last {
                if let Some(cell) = buffer.cell_mut((x, y)) {
                    cell.modifier.insert(Modifier::REVERSED);
                }
            }
        }
    }

    /// The selected text of a painted frame, one line per screen row.
    ///
    /// A wide character counts as selected when its first cell is. Trailing blanks on each row
    /// are dropped, as terminals do, so padding and highlighted surfaces do not leak into the
    /// copy.
    ///
    /// Args:
    ///     buffer (&Buffer): The frame the selection was drawn over.
    ///
    /// Returns:
    ///     String: The selected rows joined with `\n`; empty for a plain click.
    pub fn text(&self, buffer: &Buffer) -> String {
        let mut lines = Vec::new();
        for y in self.area.y..self.area.bottom() {
            let Some((first, last)) = self.columns(y) else {
                continue;
            };
            let mut line = String::new();
            let mut x = self.area.x;
            while x < self.area.right() && x <= last {
                let Some(cell) = buffer.cell((x, y)) else {
                    break;
                };
                let symbol = cell.symbol();
                if x >= first {
                    line.push_str(symbol);
                }
                x = x.saturating_add(u16::try_from(symbol.width().max(1)).unwrap_or(1));
            }
            lines.push(line.trim_end().to_owned());
        }
        lines.join("\n")
    }
}

pub(crate) fn contains(area: Rect, event: MouseEvent) -> bool {
    area.contains(Position::new(event.column, event.row))
}

/// One directional run at a fixed hit-test location from the terminal input reader.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Scroll {
    pub event: MouseEvent,
    pub lines: i32,
}

impl Scroll {
    pub fn from_event(event: MouseEvent) -> Option<Self> {
        if !event.modifiers.is_empty() {
            return None;
        }
        let lines = match event.kind {
            MouseEventKind::ScrollUp => -1,
            MouseEventKind::ScrollDown => 1,
            _ => return None,
        };
        Some(Self { event, lines })
    }

    pub fn merge(&mut self, event: MouseEvent) -> bool {
        let Some(next) = Self::from_event(event) else {
            return false;
        };
        if self.lines.signum() != next.lines.signum()
            || (self.event.column, self.event.row) != (event.column, event.row)
        {
            return false;
        }
        self.lines = self.lines.saturating_add(next.lines);
        self.event = event;
        true
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) enum Layer {
    #[default]
    Conversation,
    Files,
    Overlay(OverlayKind),
}

/// Only complete rendered rows are clickable; border, status and clipped rows are not.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct Rows {
    area: Rect,
    first: usize,
    count: usize,
    height: u16,
}

impl Rows {
    pub fn is_empty(&self) -> bool {
        self.count == 0 || self.area.width == 0
    }

    pub fn new(area: Rect, first: usize, total: usize, height: u16) -> Self {
        let height = height.max(1);
        Self {
            area,
            first,
            count: total
                .saturating_sub(first)
                .min(usize::from(area.height / height)),
            height,
        }
    }

    pub fn hit(self, event: MouseEvent) -> Option<usize> {
        if !contains(self.area, event) || self.count == 0 {
            return None;
        }
        let row = usize::from((event.row - self.area.y) / self.height);
        (row < self.count).then_some(self.first + row)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct EditorRow {
    pub logical_row: usize,
    pub column_start: usize,
    pub column_end: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct Editor {
    pub area: Rect,
    pub revision: u64,
    pub rows: Vec<EditorRow>,
}

impl Editor {
    pub fn hit(&self, event: MouseEvent, editor: &PromptEditor) -> Option<(usize, usize)> {
        if self.revision != editor.revision() || !contains(self.area, event) {
            return None;
        }
        let visual_row = usize::from(event.row - self.area.y);
        let row = self.rows.get(visual_row)?;
        Some((
            row.logical_row,
            row.column_start
                .saturating_add(usize::from(event.column - self.area.x))
                .min(row.column_end),
        ))
    }

    pub fn place_cursor(&self, event: MouseEvent, editor: &mut PromptEditor) -> bool {
        let Some((row, column)) = self.hit(event, editor) else {
            return false;
        };
        editor.place_projected_cursor(row, column);
        // A valid click transfers focus even when the cursor is already there.
        true
    }
}

/// A painted transcript row that a plain click folds or unfolds: a card's action line, an
/// explored group, or a thought header. Detail rows stay inert, since people press there to
/// select text.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct FoldRow {
    pub y: u16,
    pub entry: TranscriptEntryId,
    pub group: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct Conversation {
    pub transcript: Rect,
    /// Transcript text cells a drag can select: inside the side padding, so the margins and
    /// gutters are never copied.
    pub selectable: Rect,
    /// The footer's jump-to-latest label, painted only while the transcript is scrolled back.
    pub jump_to_latest: Option<Rect>,
    pub editor: Option<Editor>,
    pub completion: Rows,
    pub completion_visible: bool,
    pub fold_rows: Vec<FoldRow>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct Frame {
    pub layer: Layer,
    pub conversation: Conversation,
    /// Where a press starts resizing the docked side panel, when one is docked.
    pub panel_border: Option<Rect>,
    pub popup: Option<Rect>,
    pub rows: Rows,
}

impl LiveUi {
    pub(super) fn mouse_layer(&self) -> Layer {
        if let Some(kind) = self.active_overlay() {
            Layer::Overlay(kind)
        } else if self.file_picker.is_open()
            && self.editor_editable()
            && self.browse_selected.is_none()
        {
            Layer::Files
        } else {
            Layer::Conversation
        }
    }

    pub(super) async fn handle_mouse(
        &mut self,
        event: MouseEvent,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        self.handle_mouse_input(
            event,
            Scroll::from_event(event).map(|scroll| scroll.lines),
            writer,
            limit,
        )
        .await
    }

    pub(super) async fn handle_scroll(
        &mut self,
        scroll: Scroll,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        self.handle_mouse_input(scroll.event, Some(scroll.lines), writer, limit)
            .await
    }

    async fn handle_mouse_input(
        &mut self,
        event: MouseEvent,
        scroll_lines: Option<i32>,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        if !self.mouse_enabled
            || !supported(event)
            || matches!(event.kind, MouseEventKind::Drag(_) | MouseEventKind::Up(_))
            || self.unsendable_current_response()
        {
            return Ok(LoopControl::Continue);
        }
        // Resizing answers no pending decision, and must precede sidebar clicks
        // because its full surface includes the border.
        if self.press_panel_border(event) {
            return Ok(LoopControl::Continue);
        }
        // The visible sidebar owns its own wheel and disclosure clicks. Requiring
        // the current painted layer prevents clicks through a covering popup.
        if matches!(
            self.mouse_layer(),
            Layer::Conversation | Layer::Overlay(OverlayKind::SidePanel)
        ) && self
            .mouse_frame
            .as_ref()
            .is_some_and(|frame| frame.layer == self.mouse_layer())
            && (self.side_panel.docked() || self.rendered_overlay == Some(OverlayKind::SidePanel))
            && self.side_panel.contains(event.column, event.row)
        {
            if let Some(lines) = scroll_lines {
                self.side_panel.scroll_by(lines);
            } else if matches!(event.kind, MouseEventKind::Down(MouseButton::Left)) {
                if matches!(
                    self.state.view_status,
                    ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
                ) {
                    return Ok(LoopControl::Continue);
                }
                if let crate::side_panel::OverlayKey::Open(target) =
                    self.side_panel.click(event.column, event.row)
                {
                    self.side_panel.close_overlay();
                    self.render_pending = true;
                    return self.open_from_panel(target, writer, limit).await;
                }
            }
            self.render_pending = true;
            return Ok(LoopControl::Continue);
        }
        if let (Layer::Conversation, Some(lines)) = (self.mouse_layer(), scroll_lines) {
            // Scrolling activates nothing and needs no hit regions, so it moves the
            // transcript wherever the pointer is (over the composer too) and also in
            // the window between a resize, which clears the painted frame, and the
            // next paint. Before the first paint there is nothing to scroll. It also
            // works while an approval or trust prompt is docked, so the reader can review
            // what led to it; the prompt itself stays keyboard-only.
            if !self.transcript_viewport.is_sized() {
                return Ok(LoopControl::Continue);
            }
            // A selection marks screen cells, which no longer hold the same text once scrolled.
            if self.transcript_selection.take().is_some() {
                self.render_pending = true;
            }
            let control = self
                .navigate_transcript_action(TranscriptViewAction::ScrollLines(lines), writer, limit)
                .await?;
            self.reconcile_browse_selection();
            return Ok(control);
        }
        if matches!(
            self.state.view_status,
            ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
        ) {
            // Clicks never answer, dismiss, or move focus away from a decision.
            return Ok(LoopControl::Continue);
        }
        let Some(frame) = self
            .mouse_frame
            .as_ref()
            .filter(|frame| frame.layer == self.mouse_layer())
        else {
            return Ok(LoopControl::Continue);
        };
        let wheel = scroll_lines.map(|lines| {
            if lines < 0 {
                KeyCode::Up
            } else {
                KeyCode::Down
            }
        });
        let wheel_steps = scroll_lines.map_or(0, i32::unsigned_abs);
        match frame.layer {
            Layer::Overlay(kind) => {
                let Some(area) = frame.popup else {
                    return Ok(LoopControl::Continue);
                };
                if !contains(area, event) {
                    if wheel.is_none() {
                        // Closing owns the whole click; never dispatch it again to the background.
                        return self
                            .handle_focused_input(
                                Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
                                writer,
                                limit,
                            )
                            .await;
                    }
                } else if let Some(mut code) = wheel {
                    let mut control = LoopControl::Continue;
                    for _ in 0..wheel_steps {
                        if kind == OverlayKind::SessionTree
                            && code == KeyCode::Down
                            && self
                                .session_tree_picker
                                .as_ref()
                                .is_some_and(|picker| picker.at_page_end())
                        {
                            code = KeyCode::PageDown;
                        }
                        control = self
                            .handle_focused_input(
                                Input::Key(KeyEvent::new(code, KeyModifiers::NONE)),
                                writer,
                                limit,
                            )
                            .await?;
                        if control == LoopControl::Exit {
                            break;
                        }
                    }
                    return Ok(control);
                } else if self.rendered_overlay == Some(kind) {
                    if let Some(index) = frame.rows.hit(event) {
                        if self.select_mouse_row(kind, index) {
                            if kind == OverlayKind::Queue {
                                return self
                                    .handle_focused_input(
                                        Input::Key(KeyEvent::new(
                                            KeyCode::Enter,
                                            KeyModifiers::NONE,
                                        )),
                                        writer,
                                        limit,
                                    )
                                    .await;
                            }
                            self.invalidate_overlay(kind);
                            self.render_pending = true;
                        }
                    }
                }
            }
            Layer::Files => {
                let Some(area) = frame.popup else {
                    return Ok(LoopControl::Continue);
                };
                if !contains(area, event) {
                    if wheel.is_none() {
                        self.handle_file_picker_key(KeyEvent::new(
                            KeyCode::Esc,
                            KeyModifiers::NONE,
                        ));
                    }
                } else if let Some(code) = wheel {
                    for _ in 0..wheel_steps {
                        self.handle_file_picker_key(KeyEvent::new(code, KeyModifiers::NONE));
                    }
                } else if let Some(index) = frame.rows.hit(event) {
                    if self.file_picker.select_mouse(index) {
                        self.render_pending = true;
                    }
                }
            }
            Layer::Conversation => {
                if frame
                    .conversation
                    .jump_to_latest
                    .is_some_and(|area| contains(area, event))
                {
                    // The same jump as the Tail key, including loading an evicted newer tail.
                    let control = self
                        .navigate_transcript_action(TranscriptViewAction::FollowTail, writer, limit)
                        .await?;
                    self.reconcile_browse_selection();
                    return Ok(control);
                }
                // Wheel input was handled above, so this is a press. Over transcript text it
                // starts a selection, which a drag extends and the release copies.
                if self.transcript_selection.take().is_some() {
                    self.render_pending = true;
                }
                if self.key_help.is_none() {
                    let fold_target = frame
                        .conversation
                        .fold_rows
                        .iter()
                        .find(|row| row.y == event.row)
                        .copied();
                    self.transcript_selection =
                        Selection::start(frame.conversation.selectable, event)
                            .map(|selection| (selection.with_fold_target(fold_target), None));
                    // Paint once so even a fast drag has a frame to copy from.
                    self.render_pending |= self.transcript_selection.is_some();
                }
                if self.state.editor_editable() {
                    if let Some(index) = frame.conversation.completion.hit(event) {
                        let count = self
                            .completion
                            .view(
                                self.state.command_catalog.as_deref(),
                                self.state.skills.snapshot.as_deref(),
                            )
                            .map_or(0, |view| view.items.len());
                        if self.completion.select_mouse(index, count) {
                            self.render_pending = true;
                        }
                    } else if frame
                        .conversation
                        .editor
                        .as_ref()
                        .is_some_and(|hit| hit.place_cursor(event, &mut self.editor))
                    {
                        self.browse_selected = None;
                        self.render_pending = true;
                    }
                }
            }
        }
        Ok(LoopControl::Continue)
    }

    /// Start resizing the docked side panel on a left press over its border, or return it
    /// to its automatic width on a double-click, and say whether the press was there.
    /// The docked panel may own keyboard focus, but a covering popup blocks its border.
    fn press_panel_border(&mut self, event: MouseEvent) -> bool {
        let border = self
            .mouse_frame
            .as_ref()
            .filter(|frame| {
                frame.layer == self.mouse_layer()
                    && (frame.layer == Layer::Conversation
                        || (frame.layer == Layer::Overlay(OverlayKind::SidePanel)
                            && self.side_panel.docked()
                            && self.rendered_overlay == Some(OverlayKind::SidePanel)))
            })
            .and_then(|frame| frame.panel_border)
            .filter(|area| {
                matches!(event.kind, MouseEventKind::Down(MouseButton::Left))
                    && contains(*area, event)
            });
        let Some(border) = border else {
            self.side_panel.forget_border_click();
            return false;
        };
        self.side_panel
            .press_border(Instant::now(), event.column - border.x);
        self.render_pending = true;
        true
    }

    /// Extend the transcript selection on a drag, or copy and clear it on release.
    ///
    /// A release copies from the last frame painted during the selection, which is what the
    /// user saw, even when the final drag step has not been painted yet. A plain click, or a
    /// release before any frame was painted, copies nothing; a plain click on a card's action
    /// line, an explored group, or a thought header folds or unfolds it instead. While a response that could not be
    /// sent awaits keyboard recovery, a release only ends the selection: its notice explains
    /// what the next Ctrl+C does and must stay on screen.
    pub(super) fn continue_transcript_selection(&mut self, event: MouseEvent) {
        let Some((selection, _)) = &mut self.transcript_selection else {
            return;
        };
        let changed = selection.extend(event);
        if !matches!(event.kind, MouseEventKind::Up(_)) {
            self.render_pending |= changed;
            return;
        }
        let (selection, screen) = self.transcript_selection.take().expect("selection above");
        self.render_pending = true;
        if self.unsendable_current_response() {
            return;
        }
        if selection.is_empty() {
            if let Some(target) = selection.fold_target {
                self.toggle_clicked_fold(target.entry, target.group);
            }
            return;
        }
        let Some(text) = screen
            .map(|screen| selection.text(&screen))
            .filter(|text| !text.trim().is_empty())
        else {
            return;
        };
        self.notice = Some(match self.clipboard.copy(&text) {
            Ok(()) => format!("Copied {} chars to clipboard.", text.chars().count()),
            Err(error) => format!("Could not copy selection: {error}."),
        });
    }

    fn select_mouse_row(&mut self, kind: OverlayKind, index: usize) -> bool {
        match kind {
            OverlayKind::Theme => self
                .theme_picker
                .as_mut()
                .is_some_and(|view| view.select_mouse(index)),
            OverlayKind::Logo => self
                .logo_picker
                .as_mut()
                .is_some_and(|view| view.select_mouse(index)),
            OverlayKind::PromptHistory => self
                .prompt_history_view
                .as_mut()
                .is_some_and(|view| view.select_mouse(index)),
            OverlayKind::Discovery => self
                .discovery_view
                .as_mut()
                .is_some_and(|view| view.select_mouse(index, &self.state)),
            OverlayKind::Model => self.model_picker.as_mut().is_some_and(|view| {
                view.select_mouse(index, self.state.model_configuration_active())
            }),
            OverlayKind::Connection => self
                .connection_panel
                .as_mut()
                .is_some_and(|view| view.select_mouse(index)),
            OverlayKind::SessionTree => self
                .session_tree_picker
                .as_mut()
                .is_some_and(|view| view.select_mouse(index)),
            OverlayKind::Session => self
                .session_picker
                .as_mut()
                .is_some_and(|view| view.select_mouse(index)),
            OverlayKind::Queue => self
                .queue_view
                .as_mut()
                .is_some_and(|view| view.select_mouse(index, &self.state)),
            OverlayKind::GitDiff
            | OverlayKind::Context
            | OverlayKind::Help
            | OverlayKind::Detail
            | OverlayKind::SidePanel => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        }
    }

    #[test]
    fn selection_text_follows_reading_order_and_wide_characters() {
        let area = Rect::new(1, 0, 8, 3);
        let mut buffer = Buffer::empty(Rect::new(0, 0, 10, 3));
        buffer.set_string(0, 0, "|ab界c  ", ratatui::style::Style::default());
        buffer.set_string(0, 1, "|  pad   ", ratatui::style::Style::default());
        buffer.set_string(0, 2, "|xyz     │", ratatui::style::Style::default());
        let down = MouseEventKind::Down(MouseButton::Left);

        // Upward drag ending on a wide character's second cell: the character starts before the
        // selection, so it is left out.
        let mut selection = Selection::start(area, at(down, 2, 2)).unwrap();
        assert!(selection.extend(at(down, 4, 0)));
        assert_eq!(selection.text(&buffer), "c\n  pad\nxy");

        // Clamped to the area: the gutter and the margin column are never copied.
        let mut selection = Selection::start(area, at(down, 1, 0)).unwrap();
        selection.extend(at(down, 40, 2));
        assert_eq!(selection.text(&buffer), "ab界c\n  pad\nxyz");

        let click = Selection::start(area, at(down, 3, 1)).unwrap();
        assert!(click.is_empty());
        assert_eq!(click.text(&buffer), "");
        assert!(Selection::start(area, at(down, 0, 0)).is_none());
    }

    fn click(column: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column,
            row,
            modifiers: KeyModifiers::NONE,
        }
    }

    #[test]
    fn painted_editor_mapping_expands_a_fold_without_changing_raw_text() {
        let mut editor = PromptEditor::default();
        let raw = format!("{}\nsecond line", "界".repeat(2_001));
        editor.insert_paste(&raw);
        let mapping = Editor {
            area: Rect::new(2, 3, 60, 1),
            revision: editor.revision(),
            rows: vec![EditorRow {
                logical_row: 0,
                column_start: 0,
                column_end: 60,
            }],
        };

        assert!(mapping.place_cursor(click(8, 3), &mut editor));
        assert_eq!(editor.text(), raw);
        assert_eq!(editor.compact_text(), raw);
        assert_eq!(editor.cursor_offset(), 0);
    }

    #[test]
    fn painted_editor_mapping_clamps_clicks_to_the_visual_row_end() {
        let mut editor = PromptEditor::default();
        editor.insert_paste("alpha beta tail");
        let mapping = Editor {
            area: Rect::new(2, 3, 10, 1),
            revision: editor.revision(),
            rows: vec![EditorRow {
                logical_row: 0,
                column_start: 0,
                column_end: 6,
            }],
        };

        assert!(mapping.place_cursor(click(10, 3), &mut editor));
        assert_eq!(editor.cursor_column(), 6);
    }

    #[test]
    fn stale_painted_editor_mapping_cannot_expand_a_new_draft() {
        let mut editor = PromptEditor::default();
        editor.insert_paste(&"x".repeat(2_001));
        let mapping = Editor {
            area: Rect::new(0, 0, 60, 1),
            revision: editor.revision(),
            rows: vec![EditorRow {
                logical_row: 0,
                column_start: 0,
                column_end: 60,
            }],
        };
        editor.restore_prompt(&"y".repeat(2_001));
        let before = editor.clone();

        assert!(!mapping.place_cursor(click(2, 0), &mut editor));
        assert_eq!(editor, before);
    }
}
