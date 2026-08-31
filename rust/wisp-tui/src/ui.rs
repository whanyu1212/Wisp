use crate::detail_view::{DetailView, DetailViewRow};
use crate::markdown::{BlockStyle, InlineStyle, TranscriptSpanStyle};
use crate::prompt_editor::PromptEditor;
use crate::reducer::{UiState, ViewStatus};
use crate::syntax::SyntaxClass;
use crate::tool_detail::{DetailAvailability, DetailRowKind, ToolDetailPresentation};
use crate::transcript::TranscriptEntryId;
use crate::transcript_view::{
    TranscriptRowCache, TranscriptRowKind, TranscriptRowTone, TranscriptViewport,
};
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
const COMPOSER_TAB_WIDTH: usize = 4;
const DECISION_PREVIEW_GRAPHEMES: usize = 160;
const DECISION_PREVIEW_JSON_BYTES: usize = 1024;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConnectionInfo {
    pub backend_version: String,
    pub protocol_version: u32,
    pub event_schema_version: u32,
}

pub fn decision_context_visible(area: Rect) -> bool {
    area.width >= MIN_TERMINAL_WIDTH && area.height >= MIN_TERMINAL_HEIGHT
}

#[cfg(test)]
pub fn render(
    frame: &mut Frame<'_>,
    state: &UiState,
    viewport: &mut TranscriptViewport,
    row_cache: &mut TranscriptRowCache,
    editor: &PromptEditor,
    connection: &ConnectionInfo,
    notice: Option<&str>,
) {
    render_interactive(
        frame, state, viewport, row_cache, editor, connection, notice, None, None,
    );
}

#[allow(clippy::too_many_arguments)]
pub fn render_interactive(
    frame: &mut Frame<'_>,
    state: &UiState,
    viewport: &mut TranscriptViewport,
    row_cache: &mut TranscriptRowCache,
    editor: &PromptEditor,
    connection: &ConnectionInfo,
    notice: Option<&str>,
    browse_selected: Option<TranscriptEntryId>,
    detail_view: Option<&mut DetailView>,
) {
    let area = frame.area();
    if !decision_context_visible(area) {
        frame.render_widget(
            Paragraph::new("Wisp: terminal too small (minimum 30x8)")
                .alignment(Alignment::Center)
                .wrap(Wrap { trim: true }),
            area,
        );
        return;
    }

    let decision_pending = matches!(
        state.view_status,
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
    );
    if !decision_pending {
        if let Some(view) = detail_view {
            if let Some(presentation) = selected_detail(state, view) {
                render_detail(frame, area, view, presentation);
                return;
            }
        }
    }
    if decision_pending && area.height < 11 {
        let chunks = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(3), Constraint::Min(5)])
            .split(area);
        if let Some(notice) = notice {
            render_compact_notice(frame, chunks[0], notice);
        } else {
            render_header(frame, chunks[0], state, connection);
        }
        render_composer(frame, chunks[1], state, editor);
        return;
    }

    let composer_height = if editable(state) {
        u16::try_from(editor.line_count().saturating_add(2))
            .unwrap_or(MAX_COMPOSER_HEIGHT)
            .clamp(3, MAX_COMPOSER_HEIGHT)
    } else if decision_pending {
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
    render_transcript(
        frame,
        chunks[1],
        state,
        viewport,
        row_cache,
        browse_selected,
    );
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
    if let Some(session) = state.selected_session.as_ref() {
        details.push_str("  •  ");
        details.push_str(
            session
                .session_name
                .as_deref()
                .filter(|name| !name.trim().is_empty())
                .unwrap_or(&session.session_path),
        );
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

fn render_transcript(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    viewport: &mut TranscriptViewport,
    row_cache: &mut TranscriptRowCache,
    browse_selected: Option<TranscriptEntryId>,
) {
    let content_width = usize::from(area.width.saturating_sub(2)).max(1);
    let visible_lines = usize::from(area.height.saturating_sub(2)).max(1);
    viewport.set_geometry(&state.transcript, row_cache, content_width, visible_lines);
    let rows = viewport.visible_rows(&state.transcript, row_cache);
    let selected_row = browse_selected.and_then(|selected_entry| {
        rows.iter()
            .find(|row| {
                row.anchor.entry_id == selected_entry && row.kind == TranscriptRowKind::CardAction
            })
            .or_else(|| {
                rows.iter().find(|row| {
                    row.anchor.entry_id == selected_entry
                        && matches!(
                            row.kind,
                            TranscriptRowKind::CardDetail | TranscriptRowKind::CardOmission
                        )
                })
            })
            .map(|row| row.anchor)
    });
    let lines = if rows.is_empty() {
        let message = if editable(state) {
            "Type a prompt below to start."
        } else {
            ""
        };
        vec![Line::styled(message, Style::default().fg(Color::DarkGray))]
    } else {
        rows.into_iter()
            .map(|row| {
                let selected = selected_row == Some(row.anchor);
                let mut style = match row.tone {
                    TranscriptRowTone::Default => Style::default(),
                    TranscriptRowTone::User => Style::default()
                        .fg(Color::Green)
                        .add_modifier(Modifier::BOLD),
                    TranscriptRowTone::Assistant if row.kind == TranscriptRowKind::Header => {
                        Style::default()
                            .fg(Color::Cyan)
                            .add_modifier(Modifier::BOLD)
                    }
                    TranscriptRowTone::Assistant => Style::default().fg(Color::White),
                    TranscriptRowTone::Muted => Style::default().fg(Color::DarkGray),
                    TranscriptRowTone::Pending => Style::default()
                        .fg(Color::Cyan)
                        .add_modifier(Modifier::BOLD),
                    TranscriptRowTone::Success => Style::default()
                        .fg(Color::Green)
                        .add_modifier(Modifier::BOLD),
                    TranscriptRowTone::Warning => Style::default()
                        .fg(Color::Yellow)
                        .add_modifier(Modifier::BOLD),
                    TranscriptRowTone::Error => {
                        Style::default().fg(Color::Red).add_modifier(Modifier::BOLD)
                    }
                };
                if selected {
                    style = style.bg(Color::Blue).fg(Color::White);
                }
                if row.spans.len() == 1 {
                    let span = row.spans.into_iter().next().expect("one span exists");
                    Line::styled(span.text, markdown_span_style(style, span.style))
                } else {
                    let spans = if row.spans.is_empty() {
                        vec![Span::styled(String::new(), style)]
                    } else {
                        row.spans
                            .into_iter()
                            .map(|span| {
                                Span::styled(span.text, markdown_span_style(style, span.style))
                            })
                            .collect()
                    };
                    Line::from(spans)
                }
            })
            .collect()
    };
    let title = if state.history.tail_evicted && viewport.follows_tail() {
        " conversation • more history ↓ "
    } else if viewport.has_unseen_output() {
        " conversation • new ↓ "
    } else if viewport.follows_tail() {
        " conversation "
    } else {
        " conversation • scrolled "
    };
    let paragraph = Paragraph::new(Text::from(lines))
        .block(Block::default().title(title).borders(Borders::ALL));
    frame.render_widget(paragraph, area);
}

fn selected_detail<'a>(
    state: &'a UiState,
    view: &DetailView,
) -> Option<&'a ToolDetailPresentation> {
    let entry = state.transcript.entry(view.selected_entry()?)?;
    let card = entry.tool_card()?;
    let DetailAvailability::LiveRetained(detail) = &card.structured_detail else {
        return None;
    };
    Some(detail)
}

fn render_detail(
    frame: &mut Frame<'_>,
    area: Rect,
    view: &mut DetailView,
    presentation: &ToolDetailPresentation,
) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(2),
            Constraint::Length(1),
        ])
        .split(area);
    let heading = if presentation.summary.is_empty() {
        presentation.title.clone()
    } else {
        format!("{}  {}", presentation.title, presentation.summary)
    };
    frame.render_widget(
        Paragraph::new(sanitize_for_terminal(&heading)).block(
            Block::default()
                .title(" live retained detail ")
                .borders(Borders::ALL),
        ),
        chunks[0],
    );

    let width = usize::from(chunks[1].width.saturating_sub(2)).max(1);
    let height = usize::from(chunks[1].height.saturating_sub(2)).max(1);
    view.set_geometry(presentation, width, height);
    let rows = view.visible_rows(presentation);
    let lines = if rows.is_empty() {
        vec![Line::styled(
            "(no retained detail rows)",
            Style::default().fg(Color::DarkGray),
        )]
    } else {
        rows.into_iter().map(detail_line).collect()
    };
    let title = if presentation.truncated {
        " detail • retained content incomplete "
    } else {
        " detail "
    };
    frame.render_widget(
        Paragraph::new(Text::from(lines))
            .block(Block::default().title(title).borders(Borders::ALL)),
        chunks[1],
    );
    frame.render_widget(
        Paragraph::new("↑/↓ scroll · PgUp/PgDn · Home/End · Esc close")
            .style(Style::default().fg(Color::DarkGray)),
        chunks[2],
    );
}

fn detail_line(row: DetailViewRow) -> Line<'static> {
    let style = match row.kind {
        DetailRowKind::Addition => Style::default().fg(Color::Green),
        DetailRowKind::Deletion => Style::default().fg(Color::Red),
        DetailRowKind::Hunk | DetailRowKind::Header => Style::default()
            .fg(Color::Cyan)
            .add_modifier(Modifier::BOLD),
        DetailRowKind::GrepMatch => Style::default().fg(Color::LightMagenta),
        DetailRowKind::Omission | DetailRowKind::Note => Style::default().fg(Color::Yellow),
        DetailRowKind::Context | DetailRowKind::ReadLine | DetailRowKind::FindPath => {
            Style::default()
        }
    };
    Line::styled(row.text, style)
}

fn markdown_span_style(base: Style, semantic: TranscriptSpanStyle) -> Style {
    let mut style = match semantic.block {
        BlockStyle::Normal => base,
        BlockStyle::Heading(level) => {
            let color = if level <= 2 {
                Color::Cyan
            } else {
                Color::LightCyan
            };
            base.fg(color).add_modifier(Modifier::BOLD)
        }
        BlockStyle::Code => base.fg(Color::LightGreen).bg(Color::Rgb(30, 30, 30)),
        BlockStyle::RawHtml => base.fg(Color::DarkGray),
    };
    style = match semantic.inline {
        InlineStyle::Normal => style,
        InlineStyle::Code => style.fg(Color::Yellow).bg(Color::Rgb(45, 45, 45)),
        InlineStyle::Link => style
            .fg(Color::LightCyan)
            .add_modifier(Modifier::UNDERLINED),
        InlineStyle::QuoteMarker => style.fg(Color::DarkGray),
        InlineStyle::ListMarker => style.fg(Color::Cyan),
    };
    style = match semantic.syntax {
        SyntaxClass::Plain => style,
        SyntaxClass::Comment => style.fg(Color::DarkGray).add_modifier(Modifier::ITALIC),
        SyntaxClass::Keyword => style.fg(Color::LightMagenta).add_modifier(Modifier::BOLD),
        SyntaxClass::String => style.fg(Color::LightGreen),
        SyntaxClass::Number | SyntaxClass::Constant => style.fg(Color::LightMagenta),
        SyntaxClass::Type => style.fg(Color::LightCyan),
        SyntaxClass::Function => style.fg(Color::LightBlue),
        SyntaxClass::Variable => style.fg(Color::White),
        SyntaxClass::Operator => style.fg(Color::Yellow),
        SyntaxClass::Punctuation => style.fg(Color::Gray),
    };
    if semantic.strong {
        style = style.add_modifier(Modifier::BOLD);
    }
    if semantic.emphasis {
        style = style.add_modifier(Modifier::ITALIC);
    }
    if semantic.struck {
        style = style.add_modifier(Modifier::CROSSED_OUT);
    }
    style
}

fn render_composer(frame: &mut Frame<'_>, area: Rect, state: &UiState, editor: &PromptEditor) {
    let title = if state.session_operation.is_some() {
        " session "
    } else {
        match state.view_status {
            ViewStatus::Idle => " prompt ",
            ViewStatus::Running => " running ",
            ViewStatus::WaitingForApproval => " approval required ",
            ViewStatus::WaitingForTrust => " trust required ",
            ViewStatus::Error => " prompt failed ",
        }
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

    if matches!(
        state.view_status,
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
    ) {
        let lines = match state.view_status {
            ViewStatus::WaitingForApproval => {
                approval_composer_lines(state, usize::from(inner.width))
            }
            ViewStatus::WaitingForTrust => trust_composer_lines(state, usize::from(inner.width)),
            _ => unreachable!("decision rows require a decision view"),
        };
        frame.render_widget(Paragraph::new(Text::from(lines)).block(block), area);
        return;
    }

    let message = if let Some(operation) = state.session_operation.as_ref() {
        operation.label().into()
    } else {
        match state.view_status {
            ViewStatus::Running if state.cancel_requested => "Cancelling current prompt…".into(),
            ViewStatus::Running => {
                "Prompt in progress. Esc/Ctrl-C cancels; steering arrives in #466.".into()
            }
            ViewStatus::Error => "The prompt failed. Ctrl-C exits.".into(),
            ViewStatus::Idle | ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust => {
                String::new()
            }
        }
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

fn render_compact_notice(frame: &mut Frame<'_>, area: Rect, notice: &str) {
    frame.render_widget(
        Paragraph::new(sanitize_for_terminal(notice))
            .alignment(Alignment::Center)
            .style(Style::default().fg(Color::Yellow))
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
            "Enter send • Ctrl+J newline • PgUp/PgDn scroll • Ctrl-End tail • F6 details • Ctrl-C quit".into(),
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
    state.input_ready
        && state.session_operation.is_none()
        && state.current_command.is_none()
        && state.view_status == ViewStatus::Idle
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

fn trust_composer_lines(state: &UiState, width: usize) -> Vec<Line<'static>> {
    let path = state
        .pending_trust_project_path
        .as_deref()
        .map(bounded_decision_tail_preview)
        .unwrap_or_else(|| "unknown project".into());
    vec![
        Line::from(decision_row("[y trust/N deny]", width)),
        Line::from(decision_row("trust project:", width)),
        Line::from(decision_tail_row(&path, width)),
    ]
}

fn decision_tail_row(content: &str, width: usize) -> String {
    if content.width() <= width {
        return content.to_owned();
    }
    if width <= 1 {
        return "…".chars().take(width).collect();
    }
    let start = content.width().saturating_sub(width - 1);
    let tail = source_display_column_window(content, start, width - 1).text;
    format!("…{tail}")
}

fn bounded_decision_tail_preview(content: &str) -> String {
    let mut graphemes = content.graphemes(true).rev();
    let mut retained: Vec<_> = graphemes
        .by_ref()
        .take(DECISION_PREVIEW_GRAPHEMES)
        .collect();
    let truncated = graphemes.next().is_some();
    retained.reverse();
    let mut preview = bounded_decision_preview(&retained.concat());
    if truncated {
        preview.insert(0, '…');
    }
    preview
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

pub(crate) fn sanitize_for_terminal(content: &str) -> String {
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

    fn tool_result(call_id: &str, output: &str) -> crate::tool_cards::ToolResultInput {
        crate::tool_cards::ToolResultInput {
            call_id: call_id.into(),
            name: "read".into(),
            output: output.into(),
            output_tail: None,
            output_source_bytes: output.len() as u64,
            output_source_lines: crate::tool_cards::logical_line_count(output),
            output_projection_cut_mid_line: false,
            is_error: false,
            failure_code: None,
            retryable: false,
            recovery_hint: None,
            exit_code: None,
            output_has_exit_status: false,
            before_text: None,
            created: false,
            summary: None,
            truncated: false,
            process_id: None,
            process_state: None,
            process_error: None,
            stdout: None,
            stdout_source_bytes: 0,
            stderr: None,
            stderr_source_bytes: 0,
            stdout_truncated: false,
            stderr_truncated: false,
            stdout_dropped_bytes: 0,
            stderr_dropped_bytes: 0,
        }
    }

    fn render_to_string(width: u16, height: u16, state: &UiState, editor: &PromptEditor) -> String {
        render_to_string_with_notice(width, height, state, editor, None)
    }

    fn style_at_text(backend: &TestBackend, text: &str) -> Option<(Color, Color, Modifier)> {
        let expected = text
            .chars()
            .map(|character| character.to_string())
            .collect::<Vec<_>>();
        let buffer = backend.buffer();
        for y in buffer.area.top()..buffer.area.bottom() {
            for x in buffer.area.left()..buffer.area.right() {
                if expected.iter().enumerate().all(|(offset, symbol)| {
                    let offset = u16::try_from(offset).unwrap_or(u16::MAX);
                    x.checked_add(offset)
                        .filter(|candidate| *candidate < buffer.area.right())
                        .is_some_and(|candidate| buffer[(candidate, y)].symbol() == symbol)
                }) {
                    let cell = &buffer[(x, y)];
                    return Some((cell.fg, cell.bg, cell.modifier));
                }
            }
        }
        None
    }

    fn render_to_string_with_notice(
        width: u16,
        height: u16,
        state: &UiState,
        editor: &PromptEditor,
        notice: Option<&str>,
    ) -> String {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut row_cache = TranscriptRowCache::default();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    state,
                    &mut viewport,
                    &mut row_cache,
                    editor,
                    &connection(),
                    notice,
                );
            })
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
    fn busy_empty_screen_does_not_invite_early_prompt_input() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        let rendered = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(!rendered.contains("Type a prompt below to start."));
    }

    #[test]
    fn running_screen_shows_user_and_streaming_assistant_text() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.transcript.append_exchange("hello".into());
        state.transcript.append_message_delta(1, "partial answer");
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let rendered = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(rendered.contains("hello"));
        assert!(rendered.contains("partial answer"));
        assert!(rendered.contains("Esc/Ctrl-C cancels"));

        state.cancel_requested = true;
        let cancelling = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(cancelling.contains("Cancelling current prompt"));
        assert!(!cancelling.contains("Esc/Ctrl-C cancels"));
    }

    #[test]
    fn approval_and_trust_are_truthful_blocking_states() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "shell".into(),
            arguments: json!({"command": "rm -rf /tmp/example"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
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
            detail_source: crate::tool_detail::ToolDetailSource::None,
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
            detail_source: crate::tool_detail::ToolDetailSource::None,
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
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "command".into(),
        });
        let narrow = render_to_string(30, 14, &state, &PromptEditor::default());
        assert!(narrow.contains("[y once/t tool/a all/N]"));
        assert!(narrow.contains("args:"));
        assert!(narrow.contains("rm -rf"));
        let minimum_approval = render_to_string(30, 8, &state, &PromptEditor::default());
        assert!(minimum_approval.contains("[y once/t tool/a all/N]"));
        assert!(minimum_approval.contains("args:"));
        assert!(minimum_approval.contains("rm -rf"));
        for height in 8..=10 {
            let compact_notice = render_to_string_with_notice(
                30,
                height,
                &state,
                &PromptEditor::default(),
                Some(
                    "Esc/Ctrl-C again exits. Skipped approval response: frame exceeds the negotiated limit.",
                ),
            );
            assert!(compact_notice.contains("Esc/Ctrl-C"));
            assert!(compact_notice.contains("again exits"));
            assert!(compact_notice.contains("[y once/t tool/a all/N]"));
            assert!(compact_notice.contains("tool:"));
            assert!(compact_notice.contains("args:"));
        }

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
        assert!(trust.contains("[y trust/N deny]"));
        assert!(trust.contains("trust project:"));
        assert!(trust.contains("/workspace/project�[2J�spoof next"));
        assert!(!trust.contains('\u{1b}'));
        assert!(!trust.contains('\u{202e}'));

        state.pending_trust_project_path = Some(format!(
            "/very/long/shared/prefix/{}/distinct-project",
            "nested/".repeat(30)
        ));
        let narrow_trust = render_to_string(30, 14, &state, &PromptEditor::default());
        assert!(narrow_trust.contains("[y trust/N deny]"));
        assert!(narrow_trust.contains("distinct-project"));
        assert!(!narrow_trust.contains("/very/long/shared/prefix"));
        let minimum_trust = render_to_string(30, 8, &state, &PromptEditor::default());
        assert!(minimum_trust.contains("[y trust/N deny]"));
        assert!(minimum_trust.contains("distinct-project"));
        assert!(!minimum_trust.contains("/very/long/shared/prefix"));
        for height in 8..=10 {
            let compact_notice = render_to_string_with_notice(
                30,
                height,
                &state,
                &PromptEditor::default(),
                Some(
                    "Esc/Ctrl-C again exits. Skipped trust response: frame exceeds the negotiated limit.",
                ),
            );
            assert!(compact_notice.contains("Esc/Ctrl-C"));
            assert!(compact_notice.contains("again exits"));
            assert!(compact_notice.contains("[y trust/N deny]"));
            assert!(compact_notice.contains("trust project:"));
            assert!(compact_notice.contains("distinct-project"));
        }
    }

    #[test]
    fn terminal_controls_are_rendered_inertly() {
        let mut state = UiState::unconfigured();
        state
            .transcript
            .append_exchange("safe\u{1b}[2Jtail\u{202e}spoof".into());
        state
            .transcript
            .complete_message(1, "answer\u{2066}tail".into());
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
    fn transcript_title_reports_unseen_output_while_anchored() {
        let mut state = UiState::unconfigured();
        state.transcript.append_exchange("prompt".into());
        state.transcript.start_message(1);
        state
            .transcript
            .append_message_delta(1, &("history\n".repeat(50)));
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut row_cache = TranscriptRowCache::default();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    &state,
                    &mut viewport,
                    &mut row_cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                );
            })
            .unwrap();
        viewport.reduce(
            crate::transcript_view::TranscriptViewAction::PageUp,
            &state.transcript,
            &mut row_cache,
        );
        state.transcript.append_message_delta(1, "new output");
        viewport.reduce(
            crate::transcript_view::TranscriptViewAction::OutputChanged,
            &state.transcript,
            &mut row_cache,
        );

        terminal
            .draw(|frame| {
                render(
                    frame,
                    &state,
                    &mut viewport,
                    &mut row_cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                );
            })
            .unwrap();

        assert!(terminal.backend().to_string().contains("new ↓"));
    }

    #[test]
    fn transcript_title_reports_an_evicted_newer_tail() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("retained".into());
        state.history.tail_evicted = true;
        let backend = TestBackend::new(80, 18);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut row_cache = TranscriptRowCache::default();

        terminal
            .draw(|frame| {
                render(
                    frame,
                    &state,
                    &mut viewport,
                    &mut row_cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                );
            })
            .unwrap();

        assert!(terminal.backend().to_string().contains("more history ↓"));
    }

    #[test]
    fn tool_card_status_tones_reach_terminal_cells() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("read".into());
        state
            .transcript
            .observe_approval_requested(crate::tool_cards::ToolCallInput {
                call_id: "call-1".into(),
                name: "read".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"path": "README.md"}),
            });
        let draw = |state: &UiState| {
            let backend = TestBackend::new(80, 18);
            let mut terminal = Terminal::new(backend).unwrap();
            let mut viewport = TranscriptViewport::default();
            let mut row_cache = TranscriptRowCache::default();
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        state,
                        &mut viewport,
                        &mut row_cache,
                        &PromptEditor::default(),
                        &connection(),
                        None,
                    );
                })
                .unwrap();
            terminal
        };
        let pending = draw(&state);
        assert!(
            pending
                .backend()
                .to_string()
                .contains("Awaiting approval to read")
        );
        let (pending_fg, _, pending_modifiers) =
            style_at_text(pending.backend(), "Awaiting approval to read").unwrap();
        assert_eq!(pending_fg, Color::Cyan);
        assert!(pending_modifiers.contains(Modifier::BOLD));

        state
            .transcript
            .observe_approval_resolved("call-1", true, None);
        state
            .transcript
            .observe_tool_result(tool_result("call-1", "contents"));
        let complete = draw(&state);
        assert!(complete.backend().to_string().contains("Read  README.md"));
        assert!(complete.backend().to_string().contains("contents"));
        let (success_fg, _, success_modifiers) =
            style_at_text(complete.backend(), "Read  README.md").unwrap();
        assert_eq!(success_fg, Color::Green);
        assert!(success_modifiers.contains(Modifier::BOLD));
    }

    #[test]
    fn structured_diff_preview_and_detail_reach_terminal_cells_safely() {
        let mut state = UiState::unconfigured();
        let arguments = json!({
            "path": "file.txt",
            "edits": [{"oldText": "old\u{1b}[2J\n", "newText": "new value\n"}]
        });
        let card_id = state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "edit-detail".into(),
                name: "edit".into(),
                detail_source: crate::tool_detail::project_tool_detail_source(
                    "edit",
                    arguments.as_object().unwrap(),
                ),
                arguments: crate::tool_cards::bounded_tool_arguments("edit", &arguments),
            });
        let mut completed = tool_result("edit-detail", "Applied 1 edit");
        completed.name = "edit".into();
        state.transcript.observe_tool_result(completed);

        let collapsed = render_to_string(80, 20, &state, &PromptEditor::default());
        assert!(collapsed.contains("M file.txt"));
        assert!(collapsed.contains("+ new value"));
        assert!(collapsed.contains("F6 browse"));
        assert!(!collapsed.contains('\u{1b}'));

        let backend = TestBackend::new(60, 10);
        let mut browse_terminal = Terminal::new(backend).unwrap();
        let mut browse_viewport = TranscriptViewport::default();
        let mut browse_cache = TranscriptRowCache::default();
        browse_terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    &state,
                    &mut browse_viewport,
                    &mut browse_cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                    Some(card_id),
                    None,
                );
            })
            .unwrap();
        let visible = browse_viewport.visible_rows(&state.transcript, &mut browse_cache);
        assert_eq!(visible.len(), 1);
        assert_eq!(visible[0].anchor.entry_id, card_id);
        assert!(matches!(
            visible[0].kind,
            TranscriptRowKind::CardDetail | TranscriptRowKind::CardOmission
        ));
        let visible_text = visible[0].plain_text();
        let (_, selected_background, _) =
            style_at_text(browse_terminal.backend(), &visible_text).unwrap();
        assert_eq!(selected_background, Color::Blue);

        let card = state
            .transcript
            .entry(card_id)
            .unwrap()
            .tool_card()
            .unwrap();
        let DetailAvailability::LiveRetained(presentation) = &card.structured_detail else {
            panic!("structured detail expected");
        };
        let mut detail_view = DetailView::default();
        detail_view.open(card_id, presentation);
        let backend = TestBackend::new(80, 20);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut row_cache = TranscriptRowCache::default();
        terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    &state,
                    &mut viewport,
                    &mut row_cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                    Some(card_id),
                    Some(&mut detail_view),
                );
            })
            .unwrap();
        let rendered = terminal.backend().to_string();
        assert!(rendered.contains("live retained detail"));
        assert!(rendered.contains("- old�[2J"));
        assert!(rendered.contains("+ new value"));
        let (added_fg, _, _) = style_at_text(terminal.backend(), "+ new value").unwrap();
        let (deleted_fg, _, _) = style_at_text(terminal.backend(), "- old�[2J").unwrap();
        assert_eq!(added_fg, Color::Green);
        assert_eq!(deleted_fg, Color::Red);
    }

    #[test]
    fn assistant_markdown_is_formatted_while_user_content_stays_literal() {
        let mut state = UiState::unconfigured();
        state
            .transcript
            .append_exchange("# literal **user** `code`".into());
        state.transcript.complete_message(
            1,
            "# Plan\n\nUse **bold** and `code`.\n\n```rust\nlet x = 1;\n```".into(),
        );

        let rendered = render_to_string(80, 24, &state, &PromptEditor::default());

        assert!(rendered.contains("# literal **user** `code`"));
        assert!(rendered.contains("Plan"));
        assert!(rendered.contains("Use bold and code."));
        assert!(rendered.contains("let x = 1;"));
        assert!(!rendered.contains("**bold**"));
        assert!(!rendered.contains("```rust"));
    }

    #[test]
    fn closed_fence_syntax_styles_reach_terminal_cells() {
        let mut state = UiState::unconfigured();
        state.transcript.append_exchange("show code".into());
        state.transcript.complete_message(
            1,
            "```rust\nfn demo() {\n    // note\n    let value = \"text\";\n}\n```".into(),
        );
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut row_cache = TranscriptRowCache::default();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    &state,
                    &mut viewport,
                    &mut row_cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                );
            })
            .unwrap();

        let (keyword_fg, keyword_bg, keyword_modifiers) =
            style_at_text(terminal.backend(), "fn").unwrap();
        assert_eq!(keyword_fg, Color::LightMagenta);
        assert_eq!(keyword_bg, Color::Rgb(30, 30, 30));
        assert!(keyword_modifiers.contains(Modifier::BOLD));
        let (comment_fg, comment_bg, comment_modifiers) =
            style_at_text(terminal.backend(), "// note").unwrap();
        assert_eq!(comment_fg, Color::DarkGray);
        assert_eq!(comment_bg, Color::Rgb(30, 30, 30));
        assert!(comment_modifiers.contains(Modifier::ITALIC));
        let (string_fg, string_bg, _) = style_at_text(terminal.backend(), "\"text\"").unwrap();
        assert_eq!(string_fg, Color::LightGreen);
        assert_eq!(string_bg, Color::Rgb(30, 30, 30));
    }

    #[test]
    fn semantic_markdown_styles_map_to_terminal_styles() {
        let heading = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                block: BlockStyle::Heading(1),
                ..TranscriptSpanStyle::default()
            },
        );
        assert_eq!(heading.fg, Some(Color::Cyan));
        assert!(heading.add_modifier.contains(Modifier::BOLD));

        let code = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                inline: InlineStyle::Code,
                ..TranscriptSpanStyle::default()
            },
        );
        assert_eq!(code.fg, Some(Color::Yellow));
        assert!(code.bg.is_some());

        let link = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                inline: InlineStyle::Link,
                ..TranscriptSpanStyle::default()
            },
        );
        assert!(link.add_modifier.contains(Modifier::UNDERLINED));

        let uniform_code = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                block: BlockStyle::Code,
                ..TranscriptSpanStyle::default()
            },
        );
        assert_eq!(uniform_code.fg, Some(Color::LightGreen));
        assert_eq!(uniform_code.bg, Some(Color::Rgb(30, 30, 30)));

        let keyword = markdown_span_style(
            Style::default().bg(Color::Rgb(30, 30, 30)),
            TranscriptSpanStyle {
                block: BlockStyle::Code,
                syntax: SyntaxClass::Keyword,
                ..TranscriptSpanStyle::default()
            },
        );
        assert_eq!(keyword.fg, Some(Color::LightMagenta));
        assert_eq!(keyword.bg, Some(Color::Rgb(30, 30, 30)));
        assert!(keyword.add_modifier.contains(Modifier::BOLD));

        let comment = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                block: BlockStyle::Code,
                syntax: SyntaxClass::Comment,
                ..TranscriptSpanStyle::default()
            },
        );
        assert_eq!(comment.fg, Some(Color::DarkGray));
        assert!(comment.add_modifier.contains(Modifier::ITALIC));
    }

    #[test]
    fn transcript_renders_multiple_retained_turns() {
        let mut state = UiState::unconfigured();
        state.transcript.append_exchange("first prompt".into());
        state.transcript.complete_message(1, "first answer".into());
        state.transcript.append_exchange("second prompt".into());
        state.transcript.complete_message(2, "second answer".into());

        let rendered = render_to_string(80, 24, &state, &PromptEditor::default());

        assert!(rendered.contains("first prompt"));
        assert!(rendered.contains("first answer"));
        assert!(rendered.contains("second prompt"));
        assert!(rendered.contains("second answer"));
    }

    #[test]
    fn transcript_auto_follows_wrapped_output_tail() {
        let mut state = UiState::unconfigured();
        state.transcript.append_exchange("hello".into());
        state
            .transcript
            .complete_message(1, format!("{}TAIL", "wrapped output ".repeat(80)));
        let rendered = render_to_string(40, 14, &state, &PromptEditor::default());
        assert!(rendered.contains("TAIL"));
    }
}
