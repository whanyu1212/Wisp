use crate::prompt_editor::PromptEditor;
use crate::reducer::{UiState, ViewStatus};
use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

const MIN_TERMINAL_WIDTH: u16 = 30;
const MIN_TERMINAL_HEIGHT: u16 = 8;
const MAX_COMPOSER_HEIGHT: u16 = 8;
const COMPOSER_TAB_WIDTH: usize = 4;
const TRANSCRIPT_TAIL_BYTES_PER_CELL: usize = 16;
const TRANSCRIPT_TAIL_MIN_BYTES: usize = 4 * 1024;
const TRANSCRIPT_TAIL_MAX_BYTES: usize = 64 * 1024;
const DECISION_PREVIEW_GRAPHEMES: usize = 160;
const DECISION_PREVIEW_JSON_BYTES: usize = 1024;

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
    } else if matches!(
        state.view_status,
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
    ) {
        5
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
        let cursor_visible_row = row.saturating_sub(vertical_scroll);
        let display_text = composer_visible_text(
            editor,
            vertical_scroll,
            horizontal_scroll,
            usize::from(inner.width),
            usize::from(inner.height),
            cursor_visible_row,
        );
        let cursor_horizontal_scroll = display_text.cursor_horizontal_scroll;
        let paragraph = Paragraph::new(display_text.text).block(block);
        frame.render_widget(paragraph, area);
        let cursor_x = inner.x.saturating_add(
            u16::try_from(column.saturating_sub(cursor_horizontal_scroll)).unwrap_or(u16::MAX),
        );
        let cursor_y = inner
            .y
            .saturating_add(u16::try_from(row.saturating_sub(vertical_scroll)).unwrap_or(u16::MAX));
        if cursor_x < inner.right() && cursor_y < inner.bottom() {
            frame.set_cursor_position((cursor_x, cursor_y));
        }
        return;
    }

    if state.view_status == ViewStatus::WaitingForApproval {
        frame.render_widget(
            Paragraph::new(Text::from(approval_composer_lines(
                state,
                usize::from(inner.width),
            )))
            .block(block),
            area,
        );
        return;
    }

    let message = match state.view_status {
        ViewStatus::Running => {
            "Prompt in progress. Esc/Ctrl-C cancels; steering arrives in #466.".into()
        }
        ViewStatus::WaitingForTrust => trust_composer_message(state),
        ViewStatus::Error => "The prompt failed. Ctrl-C exits.".into(),
        ViewStatus::Idle | ViewStatus::WaitingForApproval => String::new(),
    };
    frame.render_widget(
        Paragraph::new(message)
            .block(block)
            .alignment(Alignment::Center)
            .wrap(Wrap { trim: true }),
        area,
    );
}

struct ComposerVisibleText {
    text: String,
    cursor_horizontal_scroll: usize,
}

fn composer_visible_text(
    editor: &PromptEditor,
    vertical_scroll: usize,
    horizontal_scroll: usize,
    width: usize,
    height: usize,
    cursor_visible_row: usize,
) -> ComposerVisibleText {
    let source_text = editor.text();
    let mut visible = String::new();
    let visible_width = width.max(1);
    let visible_height = height.max(1);
    let mut cursor_horizontal_scroll = horizontal_scroll;
    for (index, line) in source_text
        .split('\n')
        .skip(vertical_scroll)
        .take(visible_height)
        .enumerate()
    {
        if index > 0 {
            visible.push('\n');
        }
        let window = source_display_column_window(line, horizontal_scroll, visible_width);
        if index == cursor_visible_row {
            cursor_horizontal_scroll = window.effective_start;
        }
        visible.push_str(&window.text);
    }
    ComposerVisibleText {
        text: visible,
        cursor_horizontal_scroll,
    }
}

struct SourceDisplayColumnWindow {
    text: String,
    effective_start: usize,
}

fn source_display_column_window(
    line: &str,
    start: usize,
    width: usize,
) -> SourceDisplayColumnWindow {
    if width == 0 {
        return SourceDisplayColumnWindow {
            text: String::new(),
            effective_start: start,
        };
    }
    let mut column = 0_usize;
    let mut visible = String::new();
    let mut effective_start = None;
    let end = start.saturating_add(width);
    for grapheme in line.graphemes(true) {
        let grapheme_width = source_grapheme_display_width(grapheme, column);
        let next_column = column.saturating_add(grapheme_width);
        if column >= end {
            break;
        }
        if next_column > start {
            let grapheme_start = push_source_grapheme_window(
                &mut visible,
                grapheme,
                column,
                next_column,
                start,
                end,
            );
            effective_start = effective_start.or(grapheme_start);
        }
        column = next_column;
    }
    SourceDisplayColumnWindow {
        text: visible,
        effective_start: effective_start.unwrap_or(column),
    }
}

fn source_grapheme_display_width(grapheme: &str, column: usize) -> usize {
    if grapheme == "\t" {
        return COMPOSER_TAB_WIDTH - (column % COMPOSER_TAB_WIDTH);
    }
    grapheme.width()
}

fn push_source_grapheme_window(
    output: &mut String,
    grapheme: &str,
    column: usize,
    next_column: usize,
    start: usize,
    end: usize,
) -> Option<usize> {
    if grapheme == "\t" {
        let visible_start = column.max(start);
        let visible_end = next_column.min(end);
        if visible_start < visible_end {
            output.extend(std::iter::repeat_n(' ', visible_end - visible_start));
            return Some(visible_start);
        }
        return None;
    }
    if column < start || column >= end {
        return None;
    }
    output.push_str(grapheme);
    Some(column)
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

fn approval_composer_lines(state: &UiState, width: usize) -> Vec<Line<'static>> {
    let Some(pending) = state.pending_approval.as_ref() else {
        return vec![
            Line::from(decision_row("[y once/t tool/a all/N]", width)),
            Line::default(),
            Line::from(decision_row("args: unavailable", width)),
        ];
    };
    vec![
        Line::from(decision_row("[y once/t tool/a all/N]", width)),
        Line::from(decision_row(
            &format!(
                "tool: {} ({})",
                bounded_decision_preview(&pending.name),
                bounded_decision_preview(&pending.safety)
            ),
            width,
        )),
        Line::from(decision_row(
            &format!("args: {}", bounded_json_preview(&pending.arguments)),
            width,
        )),
    ]
}

fn decision_row(content: &str, width: usize) -> String {
    let safe = bounded_decision_preview(content);
    if safe.width() <= width {
        return safe;
    }
    if width <= 1 {
        return "…".chars().take(width).collect();
    }
    let mut row = source_display_column_window(&safe, 0, width - 1).text;
    row.push('…');
    row
}

fn trust_composer_message(state: &UiState) -> String {
    match state.pending_trust_project_path.as_deref() {
        Some(project_path) => format!(
            "trust project {}? [y/N]",
            bounded_decision_preview(project_path)
        ),
        None => "trust this project? [y/N]".into(),
    }
}

fn bounded_decision_preview(content: &str) -> String {
    let mut preview = String::new();
    let mut graphemes = content.graphemes(true);
    for grapheme in graphemes.by_ref().take(DECISION_PREVIEW_GRAPHEMES) {
        if matches!(grapheme, "\n" | "\r" | "\r\n" | "\t") {
            preview.push(' ');
        } else {
            for character in grapheme.chars() {
                if terminal_control_character(character) {
                    preview.push('�');
                } else {
                    preview.push(character);
                }
            }
        }
    }
    if graphemes.next().is_some() {
        preview.push('…');
    }
    preview
}

fn bounded_json_preview(value: &serde_json::Value) -> String {
    let mut writer = DecisionPreviewWriter::default();
    if serde_json::to_writer(&mut writer, value).is_err() && !writer.truncated {
        return "<invalid arguments>".into();
    }
    let mut preview = bounded_decision_preview(&String::from_utf8_lossy(&writer.bytes));
    if writer.truncated && !preview.ends_with('…') {
        preview.push('…');
    }
    preview
}

#[derive(Default)]
struct DecisionPreviewWriter {
    bytes: Vec<u8>,
    truncated: bool,
}

impl std::io::Write for DecisionPreviewWriter {
    fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
        let remaining = DECISION_PREVIEW_JSON_BYTES.saturating_sub(self.bytes.len());
        if remaining == 0 {
            self.truncated = true;
            return Err(std::io::Error::new(
                std::io::ErrorKind::WriteZero,
                "decision preview limit reached",
            ));
        }
        if buffer.len() > remaining {
            self.bytes.extend_from_slice(&buffer[..remaining]);
            self.truncated = true;
            return Err(std::io::Error::new(
                std::io::ErrorKind::WriteZero,
                "decision preview limit reached",
            ));
        }
        self.bytes.extend_from_slice(buffer);
        Ok(buffer.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
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
    let width = width.max(1);
    let max_lines = max_lines.max(1);
    let byte_start =
        transcript_tail_byte_start(content, transcript_tail_byte_budget(width, max_lines));
    let bounded_content = &content[byte_start..];
    let candidate_start =
        byte_start + transcript_tail_candidate_start(bounded_content, width, max_lines);
    let candidate = &content[candidate_start..];
    let mut line_starts = vec![candidate_start];
    let mut column = 0_usize;
    let mut line_empty = true;

    for (relative_offset, grapheme) in candidate.grapheme_indices(true) {
        let offset = candidate_start + relative_offset;
        if is_line_break_grapheme(grapheme) {
            line_starts.push(offset + grapheme.len());
            column = 0;
            line_empty = true;
            continue;
        }

        let width_at_column = transcript_tail_grapheme_width(grapheme, column);
        if !line_empty && column.saturating_add(width_at_column) > width {
            line_starts.push(offset);
            column = 0;
        }
        column = column.saturating_add(transcript_tail_grapheme_width(grapheme, column));
        line_empty = false;
    }

    if line_starts.len() > max_lines {
        return &content[line_starts[line_starts.len() - max_lines]..];
    }

    candidate
}

fn transcript_tail_candidate_start(content: &str, width: usize, max_lines: usize) -> usize {
    let visible_cells = width.saturating_mul(max_lines);
    let mut display_cells = 0_usize;
    let mut hard_lines = 0_usize;

    for (offset, grapheme) in content.grapheme_indices(true).rev() {
        if is_line_break_grapheme(grapheme) {
            hard_lines += 1;
            if hard_lines >= max_lines {
                return offset + grapheme.len();
            }
            continue;
        }

        display_cells =
            display_cells.saturating_add(transcript_tail_grapheme_min_width(grapheme).max(1));
        if display_cells >= visible_cells {
            return offset;
        }
    }

    0
}

fn transcript_tail_byte_budget(width: usize, max_lines: usize) -> usize {
    width
        .saturating_mul(max_lines)
        .saturating_mul(TRANSCRIPT_TAIL_BYTES_PER_CELL)
        .clamp(TRANSCRIPT_TAIL_MIN_BYTES, TRANSCRIPT_TAIL_MAX_BYTES)
}

fn transcript_tail_byte_start(content: &str, budget: usize) -> usize {
    if content.len() <= budget {
        return 0;
    }
    let mut start = content.len() - budget;
    while start < content.len() && !content.is_char_boundary(start) {
        start += 1;
    }
    start
}

fn is_line_break_grapheme(grapheme: &str) -> bool {
    matches!(grapheme, "\n" | "\r\n" | "\r")
}

fn transcript_tail_grapheme_min_width(grapheme: &str) -> usize {
    if grapheme == "\t" {
        return 1;
    }
    transcript_tail_grapheme_width(grapheme, 0)
}

fn transcript_tail_grapheme_width(grapheme: &str, column: usize) -> usize {
    if grapheme == "\t" {
        return 4 - (column % 4);
    }
    if grapheme.chars().any(terminal_control_character) {
        return grapheme
            .chars()
            .map(|character| {
                if terminal_control_character(character) {
                    '�'.width().unwrap_or(0)
                } else {
                    character.width().unwrap_or(0)
                }
            })
            .sum();
    }
    grapheme.width()
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
        assert!(rendered.contains("Esc/Ctrl-C cancels"));
    }

    #[test]
    fn approval_and_trust_are_truthful_blocking_states() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "shell".into(),
            arguments: json!({"command": "rm -rf /tmp/example"}),
            safety: "ask".into(),
        });
        let approval = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(approval.contains("tool: shell (ask)"));
        assert!(approval.contains("args:"));
        assert!(approval.contains("rm -rf /tmp/example"));
        assert!(approval.contains("[y once/t tool/a all/N]"));

        state.pending_approval = Some(PendingApproval {
            call_id: "call-2".into(),
            name: "shell\u{1b}[2J\u{202e}spoof\nnext".into(),
            arguments: json!({}),
            safety: "ask\u{2066}safe".into(),
        });
        let adversarial = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(!adversarial.contains('\u{1b}'));
        assert!(!adversarial.contains('\u{202e}'));
        assert!(!adversarial.contains('\u{2066}'));
        assert!(adversarial.contains("shell�[2J�spoof next"));
        assert!(adversarial.contains("ask�safe"));

        state.pending_approval = Some(PendingApproval {
            call_id: "call-3".into(),
            name: "shell".into(),
            arguments: json!({"command": "x".repeat(DECISION_PREVIEW_GRAPHEMES + 20)}),
            safety: "ask".into(),
        });
        let bounded = approval_composer_lines(&state, usize::MAX)
            .into_iter()
            .map(|line| line.to_string())
            .collect::<Vec<_>>()
            .join("\n");
        assert!(bounded.contains('…'));
        assert!(bounded.len() < DECISION_PREVIEW_GRAPHEMES + 100);

        state.pending_approval = Some(PendingApproval {
            call_id: "call-4".into(),
            name: "very-long-tool-name-".repeat(20),
            arguments: json!({"command": "rm -rf /tmp/example"}),
            safety: "command".into(),
        });
        let narrow = render_to_string(30, 14, &state, &PromptEditor::default());
        assert!(narrow.contains("[y once/t tool/a all/N]"));
        assert!(narrow.contains("args:"));
        assert!(narrow.contains("rm -rf"));

        let mut writer = DecisionPreviewWriter::default();
        let error =
            std::io::Write::write(&mut writer, &vec![b'x'; DECISION_PREVIEW_JSON_BYTES + 1])
                .unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::WriteZero);
        assert_eq!(writer.bytes.len(), DECISION_PREVIEW_JSON_BYTES);
        assert!(writer.truncated);

        state.view_status = ViewStatus::WaitingForTrust;
        state.pending_trust_request_id = Some("trust-1".into());
        state.pending_trust_project_path =
            Some("/workspace/project\u{1b}[2J\u{202e}spoof\nnext".into());
        let trust = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(trust.contains("trust project /workspace/project�[2J�spoof next? [y/N]"));
        assert!(!trust.contains('\u{1b}'));
        assert!(!trust.contains('\u{202e}'));
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
    fn composer_scrolls_to_prompt_tail_past_u16_columns() {
        let state = UiState::new("fake".into(), None, None);
        let mut editor = PromptEditor::default();
        let prompt = format!("{}TAIL", "x".repeat(70_000));
        editor.insert_paste(&prompt);
        assert_eq!(
            source_display_column_window(&prompt, 70_000, 4).text,
            "TAIL"
        );
        let rendered = render_to_string(40, 14, &state, &editor);
        assert!(rendered.contains("TAIL"));
    }

    #[test]
    fn composer_window_aligns_start_to_wide_grapheme_boundary() {
        let line = format!("{}TAIL", "🙂".repeat(35_000));
        let width = 38;
        let requested_start = line.width().saturating_sub(width - 1);
        let window = source_display_column_window(&line, requested_start, width);
        assert!(window.effective_start >= requested_start);
        assert!(line.width().saturating_sub(window.effective_start) < width);
        assert!(window.text.ends_with("TAIL"));
    }

    #[test]
    fn composer_window_expands_only_visible_tabs() {
        let line = format!("{}A\tB", "x".repeat(70_000));
        let window = source_display_column_window(&line, 70_000, 8);
        assert_eq!(window.text, "A   B");
    }

    #[test]
    fn composer_window_slices_inside_visible_tabs() {
        let window = source_display_column_window("A\tB", 2, 3);
        assert_eq!(window.effective_start, 2);
        assert_eq!(window.text, "  B");
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
        assert!(tail.len() <= 80 * 5);
    }

    #[test]
    fn transcript_tail_slice_uses_grapheme_display_width() {
        let family = "👨‍👩‍👧‍👦";
        let content = format!("HEAD{}TAIL", family.repeat(240));
        let tail = transcript_tail_slice(&content, 80, 5);
        assert!(!tail.contains("HEAD"));
        assert!(tail.starts_with(family));
        assert!(tail.ends_with("TAIL"));
        assert!(tail.len() > 80 * 5 * 8);
    }

    #[test]
    fn transcript_tail_slice_uses_current_column_for_tabs() {
        let content = format!("HEAD{}TAIL", "abc\t".repeat(200));
        let tail = transcript_tail_slice(&content, 80, 5);
        assert!(!tail.contains("HEAD"));
        assert!(tail.ends_with("TAIL"));
        assert!(tail.matches("abc\t").count() >= 75);
    }

    #[test]
    fn transcript_tail_slice_bounds_enormous_grapheme_by_bytes() {
        let content = format!("HEADa{}TAIL", "\u{301}".repeat(200_000));
        let tail = transcript_tail_slice(&content, 80, 5);
        assert!(!tail.contains("HEAD"));
        assert!(tail.ends_with("TAIL"));
        assert!(tail.len() <= transcript_tail_byte_budget(80, 5));
    }
}
