use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

pub const MAX_PROMPT_BYTES: usize = 1024 * 1024;
pub const MAX_PROMPT_LINES: usize = 10_000;
const TAB_WIDTH: usize = 4;

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
}

impl PromptEditor {
    pub fn text(&self) -> &str {
        &self.text
    }

    pub fn clear(&mut self) {
        self.text.clear();
        self.cursor = 0;
        self.preferred_column = None;
    }

    pub fn cursor_row(&self) -> usize {
        self.text[..self.cursor]
            .bytes()
            .filter(|byte| *byte == b'\n')
            .count()
    }

    pub fn cursor_column(&self) -> usize {
        let (start, _) = self.current_line_bounds();
        display_width(&self.text[start..self.cursor])
    }

    pub fn line_count(&self) -> usize {
        self.text.bytes().filter(|byte| *byte == b'\n').count() + 1
    }

    pub fn display_text(&self) -> String {
        expand_tabs(&self.text)
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> EditorAction {
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
        self.insert_text(pasted)
    }

    fn insert_text(&mut self, inserted: &str) -> EditOutcome {
        let normalized = inserted.replace("\r\n", "\n").replace('\r', "\n");
        let mut safe = String::with_capacity(normalized.len());
        let mut ignored_controls = 0;
        for character in normalized.chars() {
            if character == '\n' || character == '\t' || !character.is_control() {
                safe.push(character);
            } else {
                ignored_controls += 1;
            }
        }
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
        self.text.insert_str(self.cursor, &safe);
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
        self.text.drain(previous..self.cursor);
        self.cursor = previous;
        self.preferred_column = None;
        EditOutcome::changed()
    }

    fn delete(&mut self) -> EditOutcome {
        let Some(next) = next_grapheme_boundary(&self.text, self.cursor) else {
            return EditOutcome::default();
        };
        self.text.drain(self.cursor..next);
        self.preferred_column = None;
        EditOutcome::changed()
    }

    fn move_left(&mut self) {
        if let Some(previous) = previous_grapheme_boundary(&self.text, self.cursor) {
            self.cursor = previous;
        }
        self.preferred_column = None;
    }

    fn move_right(&mut self) {
        if let Some(next) = next_grapheme_boundary(&self.text, self.cursor) {
            self.cursor = next;
        }
        self.preferred_column = None;
    }

    fn move_home(&mut self) {
        self.cursor = self.current_line_bounds().0;
        self.preferred_column = None;
    }

    fn move_end(&mut self) {
        self.cursor = self.current_line_bounds().1;
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
        self.preferred_column = Some(preferred);
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

fn expand_tabs(text: &str) -> String {
    let mut expanded = String::with_capacity(text.len());
    let mut column = 0;
    for grapheme in text.graphemes(true) {
        match grapheme {
            "\n" => {
                expanded.push('\n');
                column = 0;
            }
            "\t" => {
                let spaces = TAB_WIDTH - (column % TAB_WIDTH);
                expanded.extend(std::iter::repeat_n(' ', spaces));
                column += spaces;
            }
            _ => {
                expanded.push_str(grapheme);
                column += grapheme.width();
            }
        }
    }
    expanded
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
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
        let outcome = editor.insert_paste("a\r\nb\rc\t\u{1b}d");
        assert_eq!(editor.text(), "a\nb\nc\td");
        assert_eq!(outcome.ignored_controls, 1);
        assert_eq!(editor.display_text(), "a\nb\nc   d");
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
    fn printable_q_is_regular_input() {
        let mut editor = PromptEditor::default();
        editor.handle_key(key(KeyCode::Char('q')));
        assert_eq!(editor.text(), "q");
    }
}
