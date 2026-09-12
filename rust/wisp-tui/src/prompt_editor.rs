use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use std::borrow::Cow;
use std::ops::Range;
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

pub const MAX_PROMPT_BYTES: usize = 1024 * 1024;
pub const MAX_PROMPT_LINES: usize = 10_000;
const COMPACT_PASTE_CHAR_THRESHOLD: usize = 2_000;
const MAX_PASTE_FOLDS: usize = 64;
const TAB_WIDTH: usize = 4;

#[derive(Clone, Debug, Eq, PartialEq)]
struct PasteFold {
    id: u64,
    range: Range<usize>,
    characters: usize,
    lines: usize,
    bytes: usize,
}

impl PasteFold {
    fn marker(&self) -> String {
        format!(
            "[Pasted content #{}: {} chars, {} lines, {} bytes]",
            self.id, self.characters, self.lines, self.bytes
        )
    }
}

#[derive(Clone, Debug)]
struct ProjectionPiece {
    display: Range<usize>,
    source: Range<usize>,
    fold_id: Option<u64>,
}

/// Display-only prompt projection with an exact mapping back to the raw draft.
#[derive(Clone, Debug)]
pub(crate) struct PromptProjection<'a> {
    text: Cow<'a, str>,
    cursor_row: usize,
    cursor_column: usize,
    source_len: usize,
    pieces: Vec<ProjectionPiece>,
}

impl PromptProjection<'_> {
    pub(crate) fn text(&self) -> &str {
        &self.text
    }

    pub(crate) fn cursor_row(&self) -> usize {
        self.cursor_row
    }

    pub(crate) fn cursor_column(&self) -> usize {
        self.cursor_column
    }

    pub(crate) fn line_count(&self) -> usize {
        self.text.bytes().filter(|byte| *byte == b'\n').count() + 1
    }

    fn target(&self, row: usize, column: usize) -> Option<ProjectedCursor> {
        let mut line_start = 0_usize;
        let line = self.text.split('\n').nth(row)?;
        for preceding in self.text.split('\n').take(row) {
            line_start = line_start.saturating_add(preceding.len() + 1);
        }
        let display_offset = line_start + byte_at_display_column(line, column);
        for piece in &self.pieces {
            if display_offset >= piece.display.start && display_offset < piece.display.end {
                return Some(ProjectedCursor {
                    source_offset: piece.fold_id.map_or_else(
                        || piece.source.start + display_offset - piece.display.start,
                        |_| piece.source.start,
                    ),
                    fold_id: piece.fold_id,
                });
            }
        }
        Some(ProjectedCursor {
            source_offset: self.source_len,
            fold_id: None,
        })
    }
}

#[derive(Clone, Copy, Debug)]
struct ProjectedCursor {
    source_offset: usize,
    fold_id: Option<u64>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct EditOutcome {
    pub changed: bool,
    pub ignored_controls: usize,
    pub rejected_limit: bool,
}

impl EditOutcome {
    fn changed() -> Self {
        Self {
            changed: true,
            ..Self::default()
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EditorAction {
    Submit,
    Edit(EditOutcome),
    Ignored,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct PromptEditor {
    text: String,
    cursor: usize,
    preferred_column: Option<usize>,
    revision: u64,
    folds: Vec<PasteFold>,
    next_fold_id: u64,
}

impl PromptEditor {
    pub fn text(&self) -> &str {
        &self.text
    }

    /// Return the compact display form while keeping [`Self::text`] authoritative.
    pub fn compact_text(&self) -> String {
        self.projection().text.into_owned()
    }

    pub(crate) fn has_folds(&self) -> bool {
        !self.folds.is_empty()
    }

    pub(crate) fn projection(&self) -> PromptProjection<'_> {
        if self.folds.is_empty() {
            let pieces = (!self.text.is_empty())
                .then_some(ProjectionPiece {
                    display: 0..self.text.len(),
                    source: 0..self.text.len(),
                    fold_id: None,
                })
                .into_iter()
                .collect();
            return PromptProjection {
                text: Cow::Borrowed(&self.text),
                cursor_row: self.cursor_row_raw(),
                cursor_column: self.cursor_column(),
                source_len: self.text.len(),
                pieces,
            };
        }
        let mut text = String::new();
        let mut pieces = Vec::with_capacity(self.folds.len().saturating_mul(2).saturating_add(1));
        let mut source_start = 0_usize;
        for fold in &self.folds {
            append_projection_source(
                &mut text,
                &mut pieces,
                &self.text,
                source_start..fold.range.start,
            );
            let marker = fold.marker();
            let display_start = text.len();
            text.push_str(&marker);
            pieces.push(ProjectionPiece {
                display: display_start..text.len(),
                source: fold.range.clone(),
                fold_id: Some(fold.id),
            });
            source_start = fold.range.end;
        }
        append_projection_source(
            &mut text,
            &mut pieces,
            &self.text,
            source_start..self.text.len(),
        );
        let cursor_display_offset = projected_display_offset(&pieces, self.cursor, text.len());
        let cursor_prefix = &text[..cursor_display_offset];
        let cursor_row = cursor_prefix.bytes().filter(|byte| *byte == b'\n').count();
        let cursor_line_start = cursor_prefix.rfind('\n').map_or(0, |index| index + 1);
        PromptProjection {
            cursor_column: display_width(&text[cursor_line_start..cursor_display_offset]),
            cursor_row,
            text: Cow::Owned(text),
            source_len: self.text.len(),
            pieces,
        }
    }

    pub fn clear(&mut self) {
        self.revision = self.revision.wrapping_add(1);
        self.text.clear();
        self.cursor = 0;
        self.preferred_column = None;
        self.folds.clear();
    }

    #[cfg(test)]
    pub fn cursor_row(&self) -> usize {
        self.cursor_row_raw()
    }

    fn cursor_row_raw(&self) -> usize {
        self.text[..self.cursor]
            .bytes()
            .filter(|byte| *byte == b'\n')
            .count()
    }

    pub fn cursor_column(&self) -> usize {
        let (start, _) = self.current_line_bounds();
        display_width(&self.text[start..self.cursor])
    }

    pub(crate) fn cursor_offset(&self) -> usize {
        self.cursor
    }

    pub(crate) fn revision(&self) -> u64 {
        self.revision
    }

    pub(crate) fn place_projected_cursor(&mut self, row: usize, column: usize) -> EditOutcome {
        let Some(target) = self.projection().target(row, column) else {
            return EditOutcome::default();
        };
        let expanded = target
            .fold_id
            .is_some_and(|fold_id| self.expand_fold(fold_id));
        let cursor_changed = self.cursor != target.source_offset || self.preferred_column.is_some();
        if cursor_changed {
            self.cursor = target.source_offset;
            self.preferred_column = None;
            self.revision = self.revision.wrapping_add(1);
        }
        if expanded || cursor_changed {
            EditOutcome::changed()
        } else {
            EditOutcome::default()
        }
    }

    /// Replace an exact UTF-8 range atomically, preserving surrounding text and limits.
    pub(crate) fn replace_range(
        &mut self,
        range: std::ops::Range<usize>,
        replacement: &str,
    ) -> EditOutcome {
        if range.start > range.end
            || range.end > self.text.len()
            || !self.text.is_char_boundary(range.start)
            || !self.text.is_char_boundary(range.end)
            || self.text.len() - range.len() + replacement.len() > MAX_PROMPT_BYTES
            || self.line_count()
                - self.text[range.clone()]
                    .bytes()
                    .filter(|b| *b == b'\n')
                    .count()
                + replacement.bytes().filter(|b| *b == b'\n').count()
                > MAX_PROMPT_LINES
        {
            return EditOutcome {
                rejected_limit: true,
                ..EditOutcome::default()
            };
        }
        self.adjust_folds_for_replacement(&range, replacement.len());
        self.text.replace_range(range.clone(), replacement);
        self.revision = self.revision.wrapping_add(1);
        self.cursor = range.start + replacement.len();
        self.preferred_column = None;
        EditOutcome::changed()
    }

    pub fn line_count(&self) -> usize {
        self.text.bytes().filter(|byte| *byte == b'\n').count() + 1
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> EditorAction {
        let cursor = self.cursor;
        let action = self.handle_editor_key(key);
        if self.cursor != cursor {
            self.revision = self.revision.wrapping_add(1);
        }
        action
    }

    fn handle_editor_key(&mut self, key: KeyEvent) -> EditorAction {
        let control = key.modifiers.contains(KeyModifiers::CONTROL);
        let alternate = key.modifiers.contains(KeyModifiers::ALT);
        let shift = key.modifiers.contains(KeyModifiers::SHIFT);
        match key.code {
            KeyCode::Enter if shift || alternate => EditorAction::Edit(self.insert_text("\n")),
            KeyCode::Enter => EditorAction::Submit,
            KeyCode::Char('j') if control => EditorAction::Edit(self.insert_text("\n")),
            KeyCode::Char('a') if control => {
                self.move_home();
                EditorAction::Edit(EditOutcome::changed())
            }
            KeyCode::Char('e') if control => {
                self.move_end();
                EditorAction::Edit(EditOutcome::changed())
            }
            KeyCode::Char(character) if !control => {
                EditorAction::Edit(self.insert_text(&character.to_string()))
            }
            KeyCode::Tab => EditorAction::Edit(self.insert_text("\t")),
            KeyCode::Backspace => EditorAction::Edit(self.backspace()),
            KeyCode::Delete => EditorAction::Edit(self.delete()),
            KeyCode::Left => {
                self.move_left();
                EditorAction::Edit(EditOutcome::changed())
            }
            KeyCode::Right => {
                self.move_right();
                EditorAction::Edit(EditOutcome::changed())
            }
            KeyCode::Up => {
                self.move_vertical(-1);
                EditorAction::Edit(EditOutcome::changed())
            }
            KeyCode::Down => {
                self.move_vertical(1);
                EditorAction::Edit(EditOutcome::changed())
            }
            KeyCode::Home => {
                self.move_home();
                EditorAction::Edit(EditOutcome::changed())
            }
            KeyCode::End => {
                self.move_end();
                EditorAction::Edit(EditOutcome::changed())
            }
            _ => EditorAction::Ignored,
        }
    }

    pub fn insert_paste(&mut self, pasted: &str) -> EditOutcome {
        let (safe, ignored_controls) = safe_prompt_text(pasted);
        let start = self.cursor;
        let characters = safe.chars().count();
        let lines = safe.bytes().filter(|byte| *byte == b'\n').count() + 1;
        let bytes = safe.len();
        let outcome = self.insert_sanitized(&safe, ignored_controls);
        if outcome.changed
            && characters > COMPACT_PASTE_CHAR_THRESHOLD
            && self.folds.len() < MAX_PASTE_FOLDS
        {
            self.push_fold(start..start + bytes, characters, lines, bytes);
        }
        outcome
    }

    /// Replace the whole draft atomically, keeping the original on limit rejection.
    pub(crate) fn restore_prompt(&mut self, prompt: &str) -> EditOutcome {
        let mut replacement = Self {
            next_fold_id: self.next_fold_id,
            ..Self::default()
        };
        let mut outcome = replacement.insert_paste(prompt);
        if !outcome.rejected_limit {
            replacement.revision = self.revision.wrapping_add(1);
            *self = replacement;
            outcome.changed = true;
        }
        outcome
    }

    /// Whether a restored queued draft can precede the current draft without overflow.
    pub fn can_prepend_restored(&self, restored: &str) -> bool {
        let (safe, _) = safe_prompt_text(restored);
        self.restored_text_fits(&safe)
    }

    /// Put a restored queued draft before the current draft without displacing it on overflow.
    pub fn prepend_restored(&mut self, restored: &str) -> EditOutcome {
        let (safe, ignored_controls) = safe_prompt_text(restored);
        if safe.is_empty() {
            return EditOutcome {
                ignored_controls,
                ..EditOutcome::default()
            };
        }
        if !self.restored_text_fits(&safe) {
            return EditOutcome {
                ignored_controls,
                rejected_limit: true,
                ..EditOutcome::default()
            };
        }
        let separator = usize::from(!self.text.is_empty());
        let prefix_len = safe.len().saturating_add(separator);
        for fold in &mut self.folds {
            fold.range.start += prefix_len;
            fold.range.end += prefix_len;
        }
        self.text.insert_str(0, &safe);
        self.revision = self.revision.wrapping_add(1);
        if separator != 0 {
            self.text.insert(safe.len(), '\n');
        }
        self.cursor = self.cursor.saturating_add(prefix_len);
        self.preferred_column = None;
        let characters = safe.chars().count();
        if characters > COMPACT_PASTE_CHAR_THRESHOLD && self.folds.len() < MAX_PASTE_FOLDS {
            let lines = safe.bytes().filter(|byte| *byte == b'\n').count() + 1;
            self.push_fold(0..safe.len(), characters, lines, safe.len());
        }
        EditOutcome {
            changed: true,
            ignored_controls,
            rejected_limit: false,
        }
    }

    fn restored_text_fits(&self, safe: &str) -> bool {
        let separator = usize::from(!self.text.is_empty());
        self.text
            .len()
            .saturating_add(safe.len())
            .saturating_add(separator)
            <= MAX_PROMPT_BYTES
            && self
                .line_count()
                .saturating_add(safe.bytes().filter(|byte| *byte == b'\n').count())
                .saturating_add(separator)
                <= MAX_PROMPT_LINES
    }

    fn insert_text(&mut self, inserted: &str) -> EditOutcome {
        let (safe, ignored_controls) = safe_prompt_text(inserted);
        self.insert_sanitized(&safe, ignored_controls)
    }

    fn insert_sanitized(&mut self, safe: &str, ignored_controls: usize) -> EditOutcome {
        if safe.is_empty() {
            return EditOutcome {
                ignored_controls,
                ..EditOutcome::default()
            };
        }
        let next_bytes = self.text.len().saturating_add(safe.len());
        let next_lines = self
            .line_count()
            .saturating_add(safe.bytes().filter(|byte| *byte == b'\n').count());
        if next_bytes > MAX_PROMPT_BYTES || next_lines > MAX_PROMPT_LINES {
            return EditOutcome {
                ignored_controls,
                rejected_limit: true,
                ..EditOutcome::default()
            };
        }
        self.adjust_folds_for_replacement(&(self.cursor..self.cursor), safe.len());
        self.text.insert_str(self.cursor, safe);
        self.revision = self.revision.wrapping_add(1);
        self.cursor += safe.len();
        self.preferred_column = None;
        EditOutcome {
            changed: true,
            ignored_controls,
            rejected_limit: false,
        }
    }

    fn backspace(&mut self) -> EditOutcome {
        let Some(previous) = previous_grapheme_boundary(&self.text, self.cursor) else {
            return EditOutcome::default();
        };
        if self.expand_fold_intersecting(previous..self.cursor) {
            return EditOutcome::changed();
        }
        self.adjust_folds_for_replacement(&(previous..self.cursor), 0);
        self.text.drain(previous..self.cursor);
        self.revision = self.revision.wrapping_add(1);
        self.cursor = previous;
        self.preferred_column = None;
        EditOutcome::changed()
    }

    fn delete(&mut self) -> EditOutcome {
        let Some(next) = next_grapheme_boundary(&self.text, self.cursor) else {
            return EditOutcome::default();
        };
        if self.expand_fold_intersecting(self.cursor..next) {
            return EditOutcome::changed();
        }
        self.adjust_folds_for_replacement(&(self.cursor..next), 0);
        self.text.drain(self.cursor..next);
        self.revision = self.revision.wrapping_add(1);
        self.preferred_column = None;
        EditOutcome::changed()
    }

    fn move_left(&mut self) {
        if let Some(previous) = previous_grapheme_boundary(&self.text, self.cursor) {
            self.expand_fold_containing(previous);
            self.cursor = previous;
        }
        self.preferred_column = None;
    }

    fn move_right(&mut self) {
        if let Some(next) = next_grapheme_boundary(&self.text, self.cursor) {
            self.expand_fold_containing(next);
            self.cursor = next;
        }
        self.preferred_column = None;
    }

    fn move_home(&mut self) {
        self.cursor = self.current_line_bounds().0;
        self.expand_fold_containing(self.cursor);
        self.preferred_column = None;
    }

    fn move_end(&mut self) {
        self.cursor = self.current_line_bounds().1;
        self.expand_fold_containing(self.cursor);
        self.preferred_column = None;
    }

    fn move_vertical(&mut self, direction: isize) {
        let (line_start, line_end) = self.current_line_bounds();
        let preferred = self
            .preferred_column
            .unwrap_or_else(|| display_width(&self.text[line_start..self.cursor]));
        let target = if direction < 0 {
            if line_start == 0 {
                return;
            }
            let end = line_start - 1;
            let start = self.text[..end].rfind('\n').map_or(0, |index| index + 1);
            Some((start, end))
        } else if line_end == self.text.len() {
            None
        } else {
            let start = line_end + 1;
            let end = self.text[start..]
                .find('\n')
                .map_or(self.text.len(), |index| start + index);
            Some((start, end))
        };
        let Some((target_start, target_end)) = target else {
            return;
        };
        self.cursor =
            byte_at_display_column(&self.text[target_start..target_end], preferred) + target_start;
        self.expand_fold_containing(self.cursor);
        self.preferred_column = Some(preferred);
    }

    fn push_fold(&mut self, range: Range<usize>, characters: usize, lines: usize, bytes: usize) {
        self.next_fold_id = self.next_fold_id.wrapping_add(1).max(1);
        let index = self
            .folds
            .partition_point(|fold| fold.range.start < range.start);
        self.folds.insert(
            index,
            PasteFold {
                id: self.next_fold_id,
                range,
                characters,
                lines,
                bytes,
            },
        );
        debug_assert!(
            self.folds
                .windows(2)
                .all(|pair| pair[0].range.end <= pair[1].range.start),
            "paste folds must remain non-overlapping and source ordered"
        );
    }

    fn expand_fold(&mut self, id: u64) -> bool {
        let Some(index) = self.folds.iter().position(|fold| fold.id == id) else {
            return false;
        };
        self.folds.remove(index);
        self.revision = self.revision.wrapping_add(1);
        true
    }

    fn expand_fold_containing(&mut self, offset: usize) -> bool {
        let Some(id) = self
            .folds
            .iter()
            .find(|fold| offset > fold.range.start && offset < fold.range.end)
            .map(|fold| fold.id)
        else {
            return false;
        };
        self.expand_fold(id)
    }

    fn expand_fold_intersecting(&mut self, range: Range<usize>) -> bool {
        let Some(id) = self
            .folds
            .iter()
            .find(|fold| range.start < fold.range.end && range.end > fold.range.start)
            .map(|fold| fold.id)
        else {
            return false;
        };
        self.expand_fold(id)
    }

    fn adjust_folds_for_replacement(&mut self, range: &Range<usize>, replacement_len: usize) {
        let removed_len = range.len();
        self.folds.retain_mut(|fold| {
            if fold.range.end <= range.start {
                return true;
            }
            if fold.range.start >= range.end {
                if replacement_len >= removed_len {
                    let shift = replacement_len - removed_len;
                    fold.range.start += shift;
                    fold.range.end += shift;
                } else {
                    let shift = removed_len - replacement_len;
                    fold.range.start -= shift;
                    fold.range.end -= shift;
                }
                return true;
            }
            false
        });
    }

    fn current_line_bounds(&self) -> (usize, usize) {
        let start = self.text[..self.cursor]
            .rfind('\n')
            .map_or(0, |index| index + 1);
        let end = self.text[self.cursor..]
            .find('\n')
            .map_or(self.text.len(), |index| self.cursor + index);
        (start, end)
    }
}

fn append_projection_source(
    text: &mut String,
    pieces: &mut Vec<ProjectionPiece>,
    source: &str,
    range: Range<usize>,
) {
    if range.is_empty() {
        return;
    }
    let display_start = text.len();
    text.push_str(&source[range.clone()]);
    pieces.push(ProjectionPiece {
        display: display_start..text.len(),
        source: range,
        fold_id: None,
    });
}

fn projected_display_offset(
    pieces: &[ProjectionPiece],
    source_offset: usize,
    display_len: usize,
) -> usize {
    for piece in pieces {
        if source_offset < piece.source.start {
            return piece.display.start;
        }
        if source_offset <= piece.source.end {
            if piece.fold_id.is_some() {
                return if source_offset == piece.source.end {
                    piece.display.end
                } else {
                    piece.display.start
                };
            }
            return piece.display.start + source_offset - piece.source.start;
        }
    }
    display_len
}

fn safe_prompt_text(inserted: &str) -> (String, usize) {
    let normalized = inserted.replace("\r\n", "\n").replace('\r', "\n");
    let mut safe = String::with_capacity(normalized.len());
    let mut ignored_controls = 0;
    for character in normalized.chars() {
        if is_safe_prompt_character(character) {
            safe.push(character);
        } else {
            ignored_controls += 1;
        }
    }
    (safe, ignored_controls)
}

fn is_safe_prompt_character(character: char) -> bool {
    character == '\n'
        || character == '\t'
        || (!character.is_control() && !crate::is_bidi_control(character))
}

fn previous_grapheme_boundary(text: &str, cursor: usize) -> Option<usize> {
    text[..cursor]
        .grapheme_indices(true)
        .next_back()
        .map(|(index, _)| index)
}

fn next_grapheme_boundary(text: &str, cursor: usize) -> Option<usize> {
    text[cursor..]
        .graphemes(true)
        .next()
        .map(|grapheme| cursor + grapheme.len())
}

fn display_width(text: &str) -> usize {
    display_width_at_column(text, 0)
}

fn display_width_at_column(text: &str, start_column: usize) -> usize {
    let mut column = start_column;
    for grapheme in text.graphemes(true) {
        if grapheme == "\t" {
            column += TAB_WIDTH - (column % TAB_WIDTH);
        } else {
            column += grapheme.width();
        }
    }
    column - start_column
}

fn byte_at_display_column(text: &str, target: usize) -> usize {
    let mut width = 0;
    for (index, grapheme) in text.grapheme_indices(true) {
        let next = width + display_width_at_column(grapheme, width);
        if next > target {
            return index;
        }
        width = next;
    }
    text.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    #[test]
    fn whole_prompt_restore_replaces_exact_text_or_keeps_the_entire_editor() {
        let mut editor = PromptEditor::default();
        editor.insert_paste("original\ndraft");
        editor.handle_key(key(KeyCode::Left));
        let before = editor.clone();
        for oversized in [
            "x".repeat(MAX_PROMPT_BYTES + 1),
            "\n".repeat(MAX_PROMPT_LINES),
        ] {
            assert!(editor.restore_prompt(&oversized).rejected_limit);
            assert_eq!(editor, before);
        }
        let exact = "  e\u{301}\n👩‍💻\tlast  ";
        assert!(editor.restore_prompt(exact).changed);
        assert_eq!(editor.text(), exact);
        assert_eq!(editor.cursor_offset(), exact.len());
    }

    #[test]
    fn command_completion_overflow_and_invalid_boundaries_leave_draft_untouched() {
        let mut editor = PromptEditor::default();
        editor.insert_paste(&format!("/mo {}", "x".repeat(MAX_PROMPT_BYTES - 4)));
        let before = editor.clone();
        assert!(editor.replace_range(0..3, "/model").rejected_limit);
        assert_eq!(editor, before);
        editor.clear();
        editor.insert_paste("/mo 候選");
        let before = editor.clone();
        assert!(editor.replace_range(0..5, "/model").rejected_limit);
        assert_eq!(editor, before);
    }

    #[test]
    fn edits_multiline_unicode_by_grapheme() {
        let mut editor = PromptEditor::default();
        editor.insert_paste("a\ne\u{301}🙂");
        editor.handle_key(key(KeyCode::Backspace));
        editor.handle_key(key(KeyCode::Backspace));
        assert_eq!(editor.text(), "a\n");
        editor.handle_key(key(KeyCode::Backspace));
        assert_eq!(editor.text(), "a");
    }

    #[test]
    fn emoji_clusters_use_terminal_width_and_delete_atomically() {
        let mut editor = PromptEditor::default();
        editor.insert_paste("👨‍👩‍👧‍👦");
        assert_eq!(editor.cursor_column(), 2);
        editor.handle_key(key(KeyCode::Backspace));
        assert_eq!(editor.text(), "");
    }

    #[test]
    fn vertical_movement_preserves_display_column() {
        let mut editor = PromptEditor::default();
        editor.insert_paste("ab🙂d\nx\nabcdef");
        editor.handle_key(key(KeyCode::Up));
        assert_eq!((editor.cursor_row(), editor.cursor_column()), (1, 1));
        editor.handle_key(key(KeyCode::Up));
        assert_eq!((editor.cursor_row(), editor.cursor_column()), (0, 5));
    }

    #[test]
    fn vertical_movement_uses_current_column_for_tab_stops() {
        let mut editor = PromptEditor::default();
        editor.insert_paste("xxxx\na\tb");
        editor.cursor = "xxxx".len();
        editor.handle_key(key(KeyCode::Down));
        assert_eq!((editor.cursor_row(), editor.cursor_column()), (1, 4));
        assert_eq!(editor.cursor, "xxxx\na\t".len());
    }

    #[test]
    fn newline_bindings_do_not_submit() {
        let mut editor = PromptEditor::default();
        assert!(matches!(
            editor.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT)),
            EditorAction::Edit(EditOutcome { changed: true, .. })
        ));
        assert!(matches!(
            editor.handle_key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL)),
            EditorAction::Edit(EditOutcome { changed: true, .. })
        ));
        assert_eq!(editor.text(), "\n\n");
        assert_eq!(editor.handle_key(key(KeyCode::Enter)), EditorAction::Submit);
    }

    #[test]
    fn paste_normalizes_newlines_preserves_tabs_and_filters_controls() {
        let mut editor = PromptEditor::default();
        let outcome = editor.insert_paste("a\r\nb\rc\t\u{1b}d\u{202e}e");
        assert_eq!(editor.text(), "a\nb\nc\tde");
        assert_eq!(outcome.ignored_controls, 2);
    }

    #[test]
    fn oversized_edit_is_rejected_atomically() {
        let mut editor = PromptEditor::default();
        editor.insert_paste("kept");
        let outcome = editor.insert_paste(&"x".repeat(MAX_PROMPT_BYTES));
        assert!(outcome.rejected_limit);
        assert_eq!(editor.text(), "kept");
    }

    #[test]
    fn restored_draft_prepends_without_displacing_the_newer_draft() {
        let mut editor = PromptEditor::default();
        editor.insert_paste("newer");
        let outcome = editor.prepend_restored("older\u{1b}\r\nline");
        assert!(outcome.changed);
        assert_eq!(outcome.ignored_controls, 1);
        assert_eq!(editor.text(), "older\nline\nnewer");

        let preserved = editor.text().to_owned();
        let outcome = editor.prepend_restored(&"x".repeat(MAX_PROMPT_BYTES));
        assert!(outcome.rejected_limit);
        assert_eq!(editor.text(), preserved);
    }

    #[test]
    fn printable_q_is_regular_input() {
        let mut editor = PromptEditor::default();
        editor.handle_key(key(KeyCode::Char('q')));
        assert_eq!(editor.text(), "q");
    }

    #[test]
    fn large_paste_is_display_only_and_counts_unicode_after_sanitizing() {
        let mut editor = PromptEditor::default();
        editor.insert_paste("before ");
        let pasted = format!("{}\r\nend\u{1b}", "界".repeat(COMPACT_PASTE_CHAR_THRESHOLD));
        let outcome = editor.insert_paste(&pasted);

        let safe = format!("{}\nend", "界".repeat(COMPACT_PASTE_CHAR_THRESHOLD));
        assert!(outcome.changed);
        assert_eq!(outcome.ignored_controls, 1);
        assert_eq!(editor.text(), format!("before {safe}"));
        assert_eq!(
            editor.compact_text(),
            format!(
                "before [Pasted content #1: {} chars, 2 lines, {} bytes]",
                COMPACT_PASTE_CHAR_THRESHOLD + 4,
                safe.len()
            )
        );
    }

    #[test]
    fn threshold_is_strict_and_fold_count_is_bounded() {
        let mut editor = PromptEditor::default();
        let threshold = "x".repeat(COMPACT_PASTE_CHAR_THRESHOLD);
        editor.insert_paste(&threshold);
        assert_eq!(editor.compact_text(), threshold);

        editor.clear();
        let large = "y".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1);
        for _ in 0..=MAX_PASTE_FOLDS {
            assert!(editor.insert_paste(&large).changed);
        }
        let compact = editor.compact_text();
        assert_eq!(
            compact.matches("[Pasted content #").count(),
            MAX_PASTE_FOLDS
        );
        assert!(compact.ends_with(&large));
        assert_eq!(editor.text().len(), large.len() * (MAX_PASTE_FOLDS + 1));
    }

    #[test]
    fn destructive_keys_expand_a_fold_before_editing_raw_text() {
        let large = "🙂".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1);
        let mut editor = PromptEditor::default();
        editor.insert_paste(&large);
        let raw = editor.text().to_owned();

        assert!(matches!(
            editor.handle_key(key(KeyCode::Backspace)),
            EditorAction::Edit(EditOutcome { changed: true, .. })
        ));
        assert_eq!(editor.text(), raw);
        assert_eq!(editor.compact_text(), raw);
        editor.handle_key(key(KeyCode::Backspace));
        assert_eq!(editor.text().chars().count(), COMPACT_PASTE_CHAR_THRESHOLD);

        editor.restore_prompt(&large);
        editor.handle_key(key(KeyCode::Home));
        editor.handle_key(key(KeyCode::Delete));
        assert_eq!(editor.text(), large);
        assert_eq!(editor.compact_text(), large);
        editor.handle_key(key(KeyCode::Delete));
        assert_eq!(editor.text().chars().count(), COMPACT_PASTE_CHAR_THRESHOLD);
    }

    #[test]
    fn movement_and_projected_click_expand_fold_without_changing_content() {
        let large = "z".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1);
        let mut editor = PromptEditor::default();
        editor.insert_paste(&large);
        editor.handle_key(key(KeyCode::Left));
        assert_eq!(editor.text(), large);
        assert_eq!(editor.compact_text(), large);

        editor.restore_prompt(&format!("prefix {large} suffix"));
        // Whole-draft history restoration is one fold. Clicking its marker expands
        // it and maps to the first raw byte without creating placeholder content.
        let projection = editor.projection();
        let before = editor.text().to_owned();
        drop(projection);
        assert!(editor.place_projected_cursor(0, 5).changed);
        assert_eq!(editor.text(), before);
        assert_eq!(editor.cursor_offset(), 0);
        assert_eq!(editor.compact_text(), before);
    }

    #[test]
    fn edits_shift_or_drop_fold_ranges_and_clear_never_reuses_ids() {
        let large = "p".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1);
        let mut editor = PromptEditor::default();
        editor.insert_paste(&large);
        assert!(editor.replace_range(0..0, "lead ").changed);
        assert!(
            editor
                .compact_text()
                .starts_with("lead [Pasted content #1:")
        );
        assert!(editor.replace_range(4..7, "overlap").changed);
        assert_eq!(editor.compact_text(), editor.text());

        editor.clear();
        editor.insert_paste(&large);
        assert!(editor.compact_text().starts_with("[Pasted content #2:"));
    }

    #[test]
    fn restore_and_queue_prepend_preserve_raw_text_and_fold_identity() {
        let newer = "n".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1);
        let older = format!("{}\nold", "o".repeat(COMPACT_PASTE_CHAR_THRESHOLD));
        let mut editor = PromptEditor::default();
        editor.restore_prompt(&newer);
        assert!(editor.prepend_restored(&older).changed);

        assert_eq!(editor.text(), format!("{older}\n{newer}"));
        let compact = editor.compact_text();
        assert!(compact.starts_with("[Pasted content #2:"));
        assert!(compact.ends_with("[Pasted content #1: 2001 chars, 1 lines, 2001 bytes]"));
    }

    #[test]
    fn inserting_large_pastes_before_and_between_folds_keeps_source_order() {
        let first = "a".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1);
        let before = "b".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1);
        let middle = "m".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1);
        let mut editor = PromptEditor::default();
        editor.insert_paste(&first);
        editor.handle_key(key(KeyCode::Home));
        editor.insert_paste(&before);

        assert_eq!(editor.text(), format!("{before}{first}"));
        let compact = editor.compact_text();
        let before_marker = compact.find("[Pasted content #2:").unwrap();
        let first_marker = compact.find("[Pasted content #1:").unwrap();
        assert!(before_marker < first_marker);

        editor.cursor = before.len();
        editor.handle_key(key(KeyCode::Char('-')));
        editor.insert_paste(&middle);
        assert_eq!(editor.text(), format!("{before}-{middle}{first}"));
        let compact = editor.compact_text();
        let before_marker = compact.find("[Pasted content #2:").unwrap();
        let middle_marker = compact.find("[Pasted content #3:").unwrap();
        let first_marker = compact.find("[Pasted content #1:").unwrap();
        assert!(before_marker < middle_marker && middle_marker < first_marker);
    }

    #[test]
    fn projected_clicks_preserve_source_boundaries_around_wide_graphemes() {
        let large = "x".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1);
        let mut editor = PromptEditor::default();
        editor.insert_paste("🙂");
        editor.insert_paste(&large);
        editor.handle_key(key(KeyCode::Char('界')));
        let marker_width = editor.folds[0].marker().width();

        assert!(editor.place_projected_cursor(0, 1).changed);
        assert_eq!(
            editor.cursor_offset(),
            0,
            "wide prefix maps to its leading byte"
        );
        assert!(editor.has_folds());

        assert!(
            editor
                .place_projected_cursor(0, 2 + marker_width + 1)
                .changed
        );
        assert_eq!(
            editor.cursor_offset(),
            "🙂".len() + large.len(),
            "wide suffix maps to its leading byte"
        );
        assert!(
            editor.has_folds(),
            "clicking beside the marker does not expand it"
        );
    }

    #[test]
    fn line_boundary_navigation_expands_only_when_destination_enters_a_fold() {
        let multiline = format!("{}\ntail", "x".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1));
        for home in [
            key(KeyCode::Home),
            KeyEvent::new(KeyCode::Char('a'), KeyModifiers::CONTROL),
        ] {
            let mut editor = PromptEditor::default();
            editor.insert_paste(&multiline);
            editor.handle_key(home);
            assert_eq!(editor.cursor_offset(), COMPACT_PASTE_CHAR_THRESHOLD + 2);
            assert_eq!(editor.text(), multiline);
            assert!(!editor.has_folds());
        }

        for end in [
            key(KeyCode::End),
            KeyEvent::new(KeyCode::Char('e'), KeyModifiers::CONTROL),
        ] {
            let mut editor = PromptEditor::default();
            editor.insert_paste(&multiline);
            editor.cursor = 0;
            editor.handle_key(end);
            assert_eq!(editor.cursor_offset(), COMPACT_PASTE_CHAR_THRESHOLD + 1);
            assert_eq!(editor.text(), multiline);
            assert!(!editor.has_folds());
        }

        let mut boundary = PromptEditor::default();
        boundary.insert_paste(&"z".repeat(COMPACT_PASTE_CHAR_THRESHOLD + 1));
        boundary.handle_key(key(KeyCode::Home));
        assert_eq!(boundary.cursor_offset(), 0);
        assert!(
            boundary.has_folds(),
            "landing exactly at the fold start stays collapsed"
        );
    }
}
