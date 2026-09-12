//! Bounded hit regions captured by the paint that users actually saw.

use crate::{Error, Input, LiveUi, LoopControl, TranscriptViewAction, ViewStatus, WriterMessage};
use crate::{OverlayKind, prompt_editor::PromptEditor};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::{Position, Rect};
use tokio::sync::mpsc;

pub(crate) fn enabled(value: Option<&str>) -> bool {
    value.is_some_and(|value| {
        matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "on"
        )
    })
}

/// Motion/drag/release reports must not fill the input queue in this navigation-only slice.
pub(crate) fn supported(event: MouseEvent) -> bool {
    matches!(
        event.kind,
        MouseEventKind::Down(MouseButton::Left)
            | MouseEventKind::ScrollUp
            | MouseEventKind::ScrollDown
    ) && event.modifiers.is_empty()
}

pub(crate) fn contains(area: Rect, event: MouseEvent) -> bool {
    area.contains(Position::new(event.column, event.row))
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) enum Layer {
    #[default]
    Conversation,
    Files,
    Overlay(OverlayKind),
}

/// Only complete rendered rows are clickable; border, status and clipped rows are not.
#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct Rows {
    area: Rect,
    first: usize,
    count: usize,
    height: u16,
}

impl Rows {
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

#[derive(Debug)]
pub(crate) struct Editor {
    pub area: Rect,
    pub revision: u64,
    pub first_line: usize,
    pub column_starts: Vec<usize>,
}

impl Editor {
    pub fn hit(&self, event: MouseEvent, editor: &PromptEditor) -> Option<(usize, usize)> {
        if self.revision != editor.revision() || !contains(self.area, event) {
            return None;
        }
        let row = usize::from(event.row - self.area.y);
        let start = *self.column_starts.get(row)?;
        Some((
            self.first_line + row,
            start + usize::from(event.column - self.area.x),
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

#[derive(Default)]
pub(crate) struct Conversation {
    pub transcript: Rect,
    pub editor: Option<Editor>,
    pub completion: Rows,
    pub completion_visible: bool,
}

pub(crate) struct Frame {
    pub layer: Layer,
    pub conversation: Conversation,
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
        if !self.mouse_enabled
            || !supported(event)
            || self.unsendable_current_response()
            || matches!(
                self.state.view_status,
                ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
            )
        {
            return Ok(LoopControl::Continue);
        }
        let Some(frame) = self
            .mouse_frame
            .as_ref()
            .filter(|frame| frame.layer == self.mouse_layer())
        else {
            return Ok(LoopControl::Continue);
        };
        let wheel = match event.kind {
            MouseEventKind::ScrollUp => Some(KeyCode::Up),
            MouseEventKind::ScrollDown => Some(KeyCode::Down),
            _ => None,
        };
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
                } else if let Some(code) = wheel {
                    let code = if kind == OverlayKind::SessionTree
                        && code == KeyCode::Down
                        && self
                            .session_tree_picker
                            .as_ref()
                            .is_some_and(|picker| picker.at_page_end())
                    {
                        KeyCode::PageDown
                    } else {
                        code
                    };
                    return self
                        .handle_focused_input(
                            Input::Key(KeyEvent::new(code, KeyModifiers::NONE)),
                            writer,
                            limit,
                        )
                        .await;
                } else if self.rendered_overlay == Some(kind) {
                    if let Some(index) = frame.rows.hit(event) {
                        if self.select_mouse_row(kind, index) {
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
                    self.handle_file_picker_key(KeyEvent::new(code, KeyModifiers::NONE));
                } else if let Some(index) = frame.rows.hit(event) {
                    if self.file_picker.select_mouse(index) {
                        self.render_pending = true;
                    }
                }
            }
            Layer::Conversation => {
                if let Some(code) = wheel {
                    if contains(frame.conversation.transcript, event) {
                        let lines = if code == KeyCode::Up { -3 } else { 3 };
                        let control = self
                            .navigate_transcript_action(
                                TranscriptViewAction::ScrollLines(lines),
                                writer,
                                limit,
                            )
                            .await?;
                        self.reconcile_browse_selection();
                        return Ok(control);
                    }
                } else if self.state.editor_editable() {
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

    fn select_mouse_row(&mut self, kind: OverlayKind, index: usize) -> bool {
        match kind {
            OverlayKind::Theme => self
                .theme_picker
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
            OverlayKind::Context | OverlayKind::Help | OverlayKind::Detail => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
            first_line: 0,
            column_starts: vec![0],
        };

        assert!(mapping.place_cursor(click(8, 3), &mut editor));
        assert_eq!(editor.text(), raw);
        assert_eq!(editor.compact_text(), raw);
        assert_eq!(editor.cursor_offset(), 0);
    }

    #[test]
    fn stale_painted_editor_mapping_cannot_expand_a_new_draft() {
        let mut editor = PromptEditor::default();
        editor.insert_paste(&"x".repeat(2_001));
        let mapping = Editor {
            area: Rect::new(0, 0, 60, 1),
            revision: editor.revision(),
            first_line: 0,
            column_starts: vec![0],
        };
        editor.restore_prompt(&"y".repeat(2_001));
        let before = editor.clone();

        assert!(!mapping.place_cursor(click(2, 0), &mut editor));
        assert_eq!(editor, before);
    }
}
