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
    if let Some(prompt) = state.last_submitted_prompt.as_deref() {
        lines.push(Line::styled(
            "you",
            Style::default()
                .fg(Color::Green)
                .add_modifier(Modifier::BOLD),
        ));
        push_content_lines(&mut lines, prompt, content_width);
        lines.push(Line::default());
        lines.push(Line::styled(
            "wisp",
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ));
        match state.retained_text.as_deref() {
            Some(content) if !content.is_empty() => {
                push_content_lines(&mut lines, content, content_width);
            }
            _ if state.view_status == ViewStatus::Running => {
                lines.push(Line::styled(
                    "working…",
                    Style::default().fg(Color::DarkGray),
                ));
            }
            _ => lines.push(Line::default()),
        }
    } else {
        lines.push(Line::styled(
            "Type a prompt below to start.",
            Style::default().fg(Color::DarkGray),
        ));
    }
    let inner_height = area.height.saturating_sub(2);
    let line_count = lines.len();
    let block = Block::default()
        .title(" conversation ")
        .borders(Borders::ALL);
    let paragraph = Paragraph::new(Text::from(lines)).block(block);
    let scroll = line_count.saturating_sub(usize::from(inner_height));
    let paragraph = paragraph.scroll((u16::try_from(scroll).unwrap_or(u16::MAX), 0));
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

fn push_content_lines(lines: &mut Vec<Line<'static>>, content: &str, width: usize) {
    let safe = sanitize_for_terminal(content);
    for hard_line in safe.split('\n') {
        let mut rendered = String::new();
        let mut rendered_width = 0_usize;
        for grapheme in hard_line.graphemes(true) {
            let grapheme_width = grapheme.width();
            if !rendered.is_empty() && rendered_width.saturating_add(grapheme_width) > width {
                lines.push(Line::from(std::mem::take(&mut rendered)));
                rendered_width = 0;
            }
            rendered.push_str(grapheme);
            rendered_width = rendered_width.saturating_add(grapheme_width);
        }
        lines.push(Line::from(rendered));
    }
}

fn sanitize_for_terminal(content: &str) -> String {
    let mut safe = String::with_capacity(content.len());
    let mut column = 0_usize;
    for grapheme in content.graphemes(true) {
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
                for character in grapheme.chars() {
                    if character.is_control() {
                        safe.push('�');
                        column += 1;
                    } else {
                        safe.push(character);
                    }
                }
                if !grapheme.chars().any(char::is_control) {
                    column += grapheme.width();
                }
            }
        }
    }
    safe
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
        state.last_submitted_prompt = Some("safe\u{1b}[2Jtail".into());
        let rendered = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(!rendered.contains('\u{1b}'));
        assert!(rendered.contains("safe�[2Jtail"));
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
}
