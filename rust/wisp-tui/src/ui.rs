use crate::prompt_editor::PromptEditor;
use crate::reducer::{UiState, ViewStatus};
use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

const MIN_TERMINAL_WIDTH: u16 = 30;
const MIN_TERMINAL_HEIGHT: u16 = 8;
const MAX_COMPOSER_HEIGHT: u16 = 8;
const TRANSCRIPT_TAIL_BYTES_PER_CELL: usize = 8;
const MIN_TRANSCRIPT_TAIL_SCAN_BYTES: usize = 4 * 1024;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConnectionInfo {
    pub backend_version: String,
    pub protocol_version: u32,
    pub event_schema_version: u32,
}

pub fn render(
    frame: &mut Frame<'_>,
    state: &UiState,
    editor: &PromptEditor,
    connection: &ConnectionInfo,
    notice: Option<&str>,
) {
    let area = frame.area();
    if area.width < MIN_TERMINAL_WIDTH || area.height < MIN_TERMINAL_HEIGHT {
        frame.render_widget(
            Paragraph::new("Wisp: terminal too small (minimum 30x8)")
                .alignment(Alignment::Center)
                .wrap(Wrap { trim: true }),
            area,
        );
        return;
    }

    let composer_height = if editable(state) {
        u16::try_from(editor.line_count().saturating_add(2))
            .unwrap_or(MAX_COMPOSER_HEIGHT)
            .clamp(3, MAX_COMPOSER_HEIGHT)
    } else {
        3
    };
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(2),
            Constraint::Length(composer_height),
            Constraint::Length(1),
        ])
        .split(area);

    render_header(frame, chunks[0], state, connection);
    render_transcript(frame, chunks[1], state);
    render_composer(frame, chunks[2], state, editor);
    render_footer(frame, chunks[3], notice);
}

fn render_header(frame: &mut Frame<'_>, area: Rect, state: &UiState, connection: &ConnectionInfo) {
    let status_style = match state.view_status {
        ViewStatus::Idle => Style::default().fg(Color::Green),
        ViewStatus::Running => Style::default().fg(Color::Cyan),
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust => {
            Style::default().fg(Color::Yellow)
        }
        ViewStatus::Error => Style::default().fg(Color::Red),
    };
    let mut details = format!(
        "backend {}  •  rpc v{} / events v{}",
        connection.backend_version, connection.protocol_version, connection.event_schema_version
    );
    if let Some(provider) = state.provider.as_deref() {
        details.push_str("  •  ");
        details.push_str(provider);
        if let Some(model) = state.model.as_deref() {
            details.push('/');
            details.push_str(model);
        }
    }
    let title = Line::from(vec![
        Span::styled(
            " WISP ",
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(" • "),
        Span::styled(
            state.view_status.as_str(),
            status_style.add_modifier(Modifier::BOLD),
        ),
    ]);
    frame.render_widget(
        Paragraph::new(sanitize_for_terminal(&details))
            .alignment(Alignment::Center)
            .block(Block::default().title(title).borders(Borders::ALL)),
        area,
    );
}

fn render_transcript(frame: &mut Frame<'_>, area: Rect, state: &UiState) {
    let mut lines = Vec::new();
    let content_width = usize::from(area.width.saturating_sub(2)).max(1);
    let visible_lines = usize::from(area.height.saturating_sub(2)).max(1);
    if let Some(prompt) = state.last_submitted_prompt.as_deref() {
        push_transcript_line(
            &mut lines,
            Line::styled(
                "you",
                Style::default()
                    .fg(Color::Green)
                    .add_modifier(Modifier::BOLD),
            ),
            visible_lines,
        );
        push_content_lines(&mut lines, prompt, content_width, visible_lines);
        push_transcript_line(&mut lines, Line::default(), visible_lines);
        push_transcript_line(
            &mut lines,
            Line::styled(
                "wisp",
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            ),
            visible_lines,
        );
        match state.retained_text.as_deref() {
            Some(content) if !content.is_empty() => {
                push_content_lines(&mut lines, content, content_width, visible_lines);
            }
            _ if state.view_status == ViewStatus::Running => {
                push_transcript_line(
                    &mut lines,
                    Line::styled("working…", Style::default().fg(Color::DarkGray)),
                    visible_lines,
                );
            }
            _ => push_transcript_line(&mut lines, Line::default(), visible_lines),
        }
    } else {
        push_transcript_line(
            &mut lines,
            Line::styled(
                "Type a prompt below to start.",
                Style::default().fg(Color::DarkGray),
            ),
            visible_lines,
        );
    }
    let block = Block::default()
        .title(" conversation ")
        .borders(Borders::ALL);
    let paragraph = Paragraph::new(Text::from(lines)).block(block);
    frame.render_widget(paragraph, area);
}

fn render_composer(frame: &mut Frame<'_>, area: Rect, state: &UiState, editor: &PromptEditor) {
    let title = match state.view_status {
        ViewStatus::Idle => " prompt ",
        ViewStatus::Running => " running ",
        ViewStatus::WaitingForApproval => " approval required ",
        ViewStatus::WaitingForTrust => " trust required ",
        ViewStatus::Error => " prompt failed ",
    };
    let border_style = match state.view_status {
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust => {
            Style::default().fg(Color::Yellow)
        }
        ViewStatus::Error => Style::default().fg(Color::Red),
        _ => Style::default().fg(Color::DarkGray),
    };
    let block = Block::default()
        .title(title)
        .borders(Borders::ALL)
        .border_style(border_style);
    let inner = block.inner(area);
    if editable(state) {
        let row = editor.cursor_row();
        let column = editor.cursor_column();
        let vertical_scroll = row.saturating_sub(usize::from(inner.height.saturating_sub(1)));
        let horizontal_scroll = column.saturating_sub(usize::from(inner.width.saturating_sub(1)));
        let paragraph = Paragraph::new(editor.display_text()).block(block).scroll((
            u16::try_from(vertical_scroll).unwrap_or(u16::MAX),
            u16::try_from(horizontal_scroll).unwrap_or(u16::MAX),
        ));
        frame.render_widget(paragraph, area);
        let cursor_x = inner.x.saturating_add(
            u16::try_from(column.saturating_sub(horizontal_scroll)).unwrap_or(u16::MAX),
        );
        let cursor_y = inner
            .y
            .saturating_add(u16::try_from(row.saturating_sub(vertical_scroll)).unwrap_or(u16::MAX));
        if cursor_x < inner.right() && cursor_y < inner.bottom() {
            frame.set_cursor_position((cursor_x, cursor_y));
        }
        return;
    }

    let message = match state.view_status {
        ViewStatus::Running => "Prompt in progress; active-run input arrives in #466.",
        ViewStatus::WaitingForApproval => {
            "A tool approval is waiting; approval controls arrive in the next PR."
        }
        ViewStatus::WaitingForTrust => {
            "Project trust is waiting; trust controls arrive in the next PR."
        }
        ViewStatus::Error => "The prompt failed. Ctrl-C exits the current Rust TUI slice.",
        ViewStatus::Idle => "",
    };
    frame.render_widget(
        Paragraph::new(message)
            .block(block)
            .alignment(Alignment::Center)
            .wrap(Wrap { trim: true }),
        area,
    );
}

fn render_footer(frame: &mut Frame<'_>, area: Rect, notice: Option<&str>) {
    let (content, style) = match notice {
        Some(notice) => (
            sanitize_for_terminal(notice),
            Style::default().fg(Color::Yellow),
        ),
        None => (
            "Enter send  •  Shift+Enter/Ctrl+J newline  •  Ctrl-C quit".into(),
            Style::default().fg(Color::DarkGray),
        ),
    };
    frame.render_widget(
        Paragraph::new(content)
            .alignment(Alignment::Center)
            .style(style),
        area,
    );
}

fn editable(state: &UiState) -> bool {
    state.input_ready && state.current_command.is_none() && state.view_status == ViewStatus::Idle
}

fn push_content_lines(
    lines: &mut Vec<Line<'static>>,
    content: &str,
    width: usize,
    max_lines: usize,
) {
    let safe = sanitize_for_terminal(transcript_tail_slice(content, width, max_lines));
    for hard_line in safe.split('\n') {
        let mut rendered = String::new();
        let mut rendered_width = 0_usize;
        for grapheme in hard_line.graphemes(true) {
            let grapheme_width = grapheme.width();
            if !rendered.is_empty() && rendered_width.saturating_add(grapheme_width) > width {
                push_transcript_line(lines, Line::from(std::mem::take(&mut rendered)), max_lines);
                rendered_width = 0;
            }
            rendered.push_str(grapheme);
            rendered_width = rendered_width.saturating_add(grapheme_width);
        }
        push_transcript_line(lines, Line::from(rendered), max_lines);
    }
}

fn sanitize_for_terminal(content: &str) -> String {
    let normalized = content.replace("\r\n", "\n").replace('\r', "\n");
    let mut safe = String::with_capacity(normalized.len());
    let mut column = 0_usize;
    for grapheme in normalized.graphemes(true) {
        match grapheme {
            "\n" => {
                safe.push('\n');
                column = 0;
            }
            "\t" => {
                let spaces = 4 - (column % 4);
                safe.extend(std::iter::repeat_n(' ', spaces));
                column += spaces;
            }
            _ => {
                if grapheme.chars().any(terminal_control_character) {
                    let mut safe_grapheme = String::with_capacity(grapheme.len());
                    for character in grapheme.chars() {
                        if terminal_control_character(character) {
                            safe_grapheme.push('�');
                        } else {
                            safe_grapheme.push(character);
                        }
                    }
                    column += safe_grapheme.as_str().width();
                    safe.push_str(&safe_grapheme);
                } else {
                    safe.push_str(grapheme);
                    column += grapheme.width();
                }
            }
        }
    }
    safe
}

fn terminal_control_character(character: char) -> bool {
    character.is_control() || crate::is_bidi_control(character)
}

fn push_transcript_line(lines: &mut Vec<Line<'static>>, line: Line<'static>, max_lines: usize) {
    lines.push(line);
    if lines.len() > max_lines {
        let extra = lines.len() - max_lines;
        lines.drain(0..extra);
    }
}

fn transcript_tail_slice(content: &str, width: usize, max_lines: usize) -> &str {
    if content.is_empty() {
        return content;
    }
    let visible_cells = width.max(1).saturating_mul(max_lines.max(1));
    let scan_bytes = visible_cells
        .saturating_mul(TRANSCRIPT_TAIL_BYTES_PER_CELL)
        .max(MIN_TRANSCRIPT_TAIL_SCAN_BYTES);
    if content.len() <= scan_bytes {
        return content;
    }
    let mut start = content.len() - scan_bytes;
    while start < content.len() && !content.is_char_boundary(start) {
        start += 1;
    }
    let mut hard_lines = 0_usize;
    for (offset, character) in content[start..].char_indices().rev() {
        if character == '\n' {
            hard_lines += 1;
            if hard_lines >= max_lines.max(1) {
                return &content[start + offset + 1..];
            }
        }
    }
    &content[start..]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reducer::{ActiveCommand, ActiveCommandType, PendingApproval};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use serde_json::json;

    fn connection() -> ConnectionInfo {
        ConnectionInfo {
            backend_version: "0.9.0".into(),
            protocol_version: 2,
            event_schema_version: 34,
        }
    }

    fn render_to_string(width: u16, height: u16, state: &UiState, editor: &PromptEditor) -> String {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| render(frame, state, editor, &connection(), None))
            .unwrap();
        terminal.backend().to_string()
    }

    #[test]
    fn idle_screen_contains_live_composer_and_contract() {
        let state = UiState::new("fake".into(), Some("model-x".into()), None);
        let mut editor = PromptEditor::default();
        editor.insert_paste("hello");
        let rendered = render_to_string(80, 18, &state, &editor);
        assert!(rendered.contains("rpc v2 / events v34"));
        assert!(rendered.contains("hello"));
        assert!(rendered.contains("Enter send"));
    }

    #[test]
    fn running_screen_shows_user_and_streaming_assistant_text() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.last_submitted_prompt = Some("hello".into());
        state.retained_text = Some("partial answer".into());
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let rendered = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(rendered.contains("hello"));
        assert!(rendered.contains("partial answer"));
        assert!(rendered.contains("active-run input arrives in #466"));
    }

    #[test]
    fn approval_and_trust_are_truthful_blocking_states() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "shell".into(),
            arguments: json!({}),
            safety: "ask".into(),
        });
        let approval = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(approval.contains("approval is waiting"));

        state.view_status = ViewStatus::WaitingForTrust;
        state.pending_trust_request_id = Some("trust-1".into());
        let trust = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(trust.contains("Project trust is waiting"));
    }

    #[test]
    fn terminal_controls_are_rendered_inertly() {
        let mut state = UiState::unconfigured();
        state.last_submitted_prompt = Some("safe\u{1b}[2Jtail\u{202e}spoof".into());
        state.retained_text = Some("answer\u{2066}tail".into());
        let rendered = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(!rendered.contains('\u{1b}'));
        assert!(!rendered.contains('\u{202e}'));
        assert!(!rendered.contains('\u{2066}'));
        assert!(rendered.contains("safe�[2Jtail�spoof"));
        assert!(rendered.contains("answer�tail"));
    }

    #[test]
    fn terminal_sanitizer_preserves_crlf_line_breaks() {
        assert_eq!(
            sanitize_for_terminal("alpha\r\nbeta\rgamma"),
            "alpha\nbeta\ngamma"
        );
    }

    #[test]
    fn tiny_terminal_uses_safe_fallback() {
        let state = UiState::unconfigured();
        let rendered = render_to_string(20, 5, &state, &PromptEditor::default());
        assert!(rendered.contains("terminal too"));
    }

    #[test]
    fn minimum_supported_terminal_renders_without_layout_underflow() {
        let state = UiState::unconfigured();
        let rendered = render_to_string(30, 8, &state, &PromptEditor::default());
        assert!(rendered.contains("WISP"));
        assert!(!rendered.contains("terminal too"));
    }

    #[test]
    fn transcript_auto_follows_wrapped_output_tail() {
        let mut state = UiState::unconfigured();
        state.last_submitted_prompt = Some("hello".into());
        state.retained_text = Some(format!("{}TAIL", "wrapped output ".repeat(80)));
        let rendered = render_to_string(40, 14, &state, &PromptEditor::default());
        assert!(rendered.contains("TAIL"));
    }

    #[test]
    fn transcript_tail_slice_bounds_large_unbroken_content() {
        let content = format!("HEAD{}TAIL", "x".repeat(1024 * 1024));
        let tail = transcript_tail_slice(&content, 80, 5);
        assert!(!tail.contains("HEAD"));
        assert!(tail.ends_with("TAIL"));
        let scan_bytes =
            (80 * 5 * TRANSCRIPT_TAIL_BYTES_PER_CELL).max(MIN_TRANSCRIPT_TAIL_SCAN_BYTES);
        assert!(tail.len() <= scan_bytes + "TAIL".len());
    }
}
