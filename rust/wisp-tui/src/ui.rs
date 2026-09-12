use crate::detail_view::{DetailView, DetailViewRow};
use crate::keybindings::{Action as KeyAction, Bindings};
use crate::markdown::{BlockStyle, InlineStyle, TranscriptSpanStyle};
use crate::mouse;
use crate::prompt_editor::{PromptEditor, PromptProjection};
use crate::reducer::{UiState, ViewStatus};
use crate::syntax::SyntaxClass;
use crate::theme::Palette;
use crate::tool_detail::{DetailAvailability, DetailRowKind, ToolDetailPresentation};
use crate::transcript::{TranscriptEntryId, TranscriptRole};
use crate::transcript_view::{
    RowAnchor, RowPosition, TranscriptRow, TranscriptRowCache, TranscriptRowKind,
    TranscriptRowTone, TranscriptViewport,
};
use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
#[cfg(test)]
use ratatui::style::Color;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::commands::QueueKind;

const MIN_TERMINAL_WIDTH: u16 = 30;
const MIN_TERMINAL_HEIGHT: u16 = 8;
const MAX_COMPOSER_HEIGHT: u16 = 8;
const COMPOSER_TAB_WIDTH: usize = 4;
const DECISION_PREVIEW_GRAPHEMES: usize = 160;
const DECISION_PREVIEW_JSON_BYTES: usize = 1024;
pub(crate) const EMPTY_TRANSCRIPT_HINT: &str = "Type a prompt or / for commands.";
const STICKY_USER_ROWS: usize = 4;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConnectionInfo {
    pub backend_version: String,
    pub protocol_version: u32,
    pub event_schema_version: u32,
}

pub fn decision_context_visible(area: Rect) -> bool {
    area.width >= MIN_TERMINAL_WIDTH && area.height >= MIN_TERMINAL_HEIGHT
}

fn header_height(area: Rect) -> u16 {
    // Keep one editable row plus its borders, the transcript borders and footer.
    area.height.saturating_sub(6).min(3)
}

fn composer_height(area: Rect, state: &UiState, editor: &PromptEditor) -> u16 {
    if editable(state) {
        let queue_rows = if state.active_prompt_editable() {
            state
                .queued_steering()
                .saturating_add(state.queued_follow_ups())
                .min(3)
        } else {
            0
        };
        u16::try_from(
            editor
                .projection()
                .line_count()
                .saturating_add(queue_rows)
                .saturating_add(2),
        )
        .unwrap_or(MAX_COMPOSER_HEIGHT)
        .clamp(
            3,
            area.height
                .saturating_sub(header_height(area) + 3)
                .clamp(3, MAX_COMPOSER_HEIGHT),
        )
    } else if matches!(
        state.view_status,
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
    ) {
        5
    } else {
        3
    }
}

/// Anchor suggestions above the composer without changing transcript geometry.
pub(crate) fn file_picker_area(area: Rect, state: &UiState, editor: &PromptEditor) -> Option<Rect> {
    if !decision_context_visible(area) {
        return None;
    }
    // At short sizes a tall draft leaves no free strip. Cover the upper rows rather
    // than hide all choices or change the transcript's layout to make room.
    let bottom = area
        .bottom()
        .saturating_sub(composer_height(area, state, editor) + 1)
        .max(area.y + 4);
    let height = (bottom - area.y).min(12);
    Some(Rect::new(
        area.x,
        bottom - height,
        area.width.min(100),
        height,
    ))
}

/// Center a bounded popup within supported terminal sizes.
pub fn overlay_area(area: Rect) -> Option<Rect> {
    if !decision_context_visible(area) {
        return None;
    }
    let width = (area.width.saturating_mul(4) / 5).clamp(MIN_TERMINAL_WIDTH, 100);
    let height = (area.height.saturating_mul(4) / 5).clamp(MIN_TERMINAL_HEIGHT, 28);
    Some(Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    ))
}

pub fn clear_overlay(frame: &mut Frame<'_>, area: Rect, palette: Palette) {
    if area.x > frame.area().x {
        for y in area.y..area.bottom() {
            let cell = &mut frame.buffer_mut()[(area.x - 1, y)];
            // A wide glyph crossing the left edge would make the terminal skip the border.
            // Its leading cell cannot remain visible without its covered trailing cell.
            if cell.symbol().width() > 1 {
                cell.reset();
                cell.set_style(palette.base());
            }
        }
    }
    frame.render_widget(Clear, area);
    frame.render_widget(Block::default().style(palette.base()), area);
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
        frame,
        state,
        viewport,
        row_cache,
        editor,
        connection,
        notice,
        None,
        true,
        None,
        Palette::default(),
        &Bindings::default(),
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
    composer_focused: bool,
    completion: Option<&crate::commands::CompletionView<'_>>,
    palette: Palette,
    bindings: &Bindings,
) -> mouse::Conversation {
    let area = frame.area();
    frame.render_widget(Block::default().style(palette.base()), area);
    if !decision_context_visible(area) {
        frame.render_widget(
            Paragraph::new("Wisp: terminal too small (minimum 30x8)")
                .alignment(Alignment::Center)
                .wrap(Wrap { trim: true }),
            area,
        );
        return mouse::Conversation::default();
    }

    let decision_pending = matches!(
        state.view_status,
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
    );
    if decision_pending && area.height < 11 {
        let chunks = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(3), Constraint::Min(5)])
            .split(area);
        if let Some(notice) = notice {
            render_compact_notice(frame, chunks[0], notice, palette);
        } else {
            render_header(frame, chunks[0], state, connection, palette);
        }
        render_composer(frame, chunks[1], state, editor, composer_focused, palette);
        return mouse::Conversation::default();
    }

    let composer_height = composer_height(area, state, editor);
    let completion_height = completion.map_or(0, |view| {
        (view.items.len().min(5) as u16).min(area.height.saturating_sub(composer_height + 4))
    });
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(header_height(area)),
            Constraint::Min(if completion_height > 0 { 0 } else { 2 }),
            Constraint::Length(completion_height),
            Constraint::Length(composer_height),
            Constraint::Length(1),
        ])
        .split(area);

    render_header(frame, chunks[0], state, connection, palette);
    render_transcript(
        frame,
        chunks[1],
        state,
        viewport,
        row_cache,
        browse_selected,
        palette,
    );
    let completion_rows = completion
        .filter(|_| completion_height > 0)
        .map(|view| crate::commands::render_completion(frame, chunks[2], view, palette))
        .unwrap_or_default();
    let editor = render_composer(frame, chunks[3], state, editor, composer_focused, palette);
    render_footer(frame, chunks[4], state, notice, palette, bindings);
    mouse::Conversation {
        transcript: chunks[1],
        editor,
        completion: completion_rows,
        completion_visible: completion_height > 0,
    }
}

fn status_label(state: &UiState) -> &'static str {
    if state.configuration_active() {
        "configuring"
    } else if state.interaction_status == crate::reducer::InteractionStatus::Compacting {
        "compacting"
    } else {
        match state.view_status {
            ViewStatus::Idle => "idle",
            ViewStatus::Running => "working",
            ViewStatus::WaitingForApproval => "approval",
            ViewStatus::WaitingForTrust => "trust",
            ViewStatus::Error => "error",
        }
    }
}

fn header_identity(state: &UiState) -> Option<String> {
    let provider = state.provider.as_deref()?;
    let mut identity = provider.to_string();
    if let Some(model) = state
        .model_catalog
        .as_ref()
        .and_then(|catalog| catalog.selection.effective_model.as_deref())
        .or(state.model.as_deref())
    {
        identity.push('/');
        identity.push_str(model);
    }
    if let Some(effort) = state.effort.as_deref() {
        identity.push_str(" · ");
        identity.push_str(effort);
    }
    if state.model_selection_stale {
        identity.push_str(" (last confirmed; selection unavailable)");
    }
    Some(identity)
}

fn header_details(state: &UiState) -> String {
    if let Some(reason) = state.context.compaction {
        return format!("Compacting ({})…", reason.as_str());
    }
    if let Some(notice) = &state.context.compaction_notice {
        return notice.clone();
    }
    let mut parts = Vec::new();
    if let Some(identity) = header_identity(state) {
        parts.push(identity);
    }
    if let Some(session) = state.selected_session.as_ref() {
        parts.push(
            session
                .session_name
                .as_deref()
                .filter(|name| !name.trim().is_empty())
                .unwrap_or(&session.session_path)
                .to_string(),
        );
    }
    parts.join("  ·  ")
}

fn render_header(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    _connection: &ConnectionInfo,
    palette: Palette,
) {
    let status_style = match state.view_status {
        ViewStatus::Idle => Style::default().fg(palette.success),
        ViewStatus::Running => Style::default().fg(palette.primary),
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust => {
            Style::default().fg(palette.warning)
        }
        ViewStatus::Error => Style::default().fg(palette.error),
    };
    let mut title = vec![
        Span::styled(
            " WISP ",
            Style::default()
                .fg(palette.primary)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(format!(
            "{} • ",
            if state.mode_confirmed {
                state.mode.as_str()
            } else {
                "?"
            }
        )),
        Span::styled(
            status_label(state),
            status_style.add_modifier(Modifier::BOLD),
        ),
    ];
    if state.active_prompt_editable() {
        let steering = state.queued_steering();
        let follow_up = state.queued_follow_ups();
        if steering > 0 || follow_up > 0 {
            title.push(Span::raw(format!(" • s:{steering}/l:{follow_up}")));
        }
    }
    let title = Line::from(title);
    let details = header_details(state);
    let context = crate::context_view::indicator(state, usize::from(area.width.saturating_sub(4)));
    frame.render_widget(
        Paragraph::new(sanitize_for_terminal(&details))
            .alignment(Alignment::Center)
            .block(
                Block::default()
                    .title(title)
                    .border_style(palette.border())
                    .title_bottom(Line::raw(format!(" {context} ")).right_aligned())
                    .borders(Borders::ALL),
            ),
        area,
    );
}

fn sticky_user_rows(
    state: &UiState,
    viewport: &TranscriptViewport,
    row_cache: &mut TranscriptRowCache,
    width: usize,
) -> Vec<TranscriptRow> {
    if !viewport.follows_tail() {
        return Vec::new();
    }
    let Some(user) = state
        .transcript
        .entries()
        .iter()
        .rev()
        .find(|entry| entry.role == TranscriptRole::User)
    else {
        return Vec::new();
    };
    let mut rows = Vec::new();
    let mut anchor = RowAnchor {
        entry_id: user.id,
        position: RowPosition::Header,
    };
    while rows.len() < STICKY_USER_ROWS {
        let Some(cached) = row_cache.row_at(&state.transcript, anchor, width) else {
            break;
        };
        if cached.row.kind == TranscriptRowKind::Spacer || cached.row.anchor.entry_id != user.id {
            break;
        }
        rows.push(cached.row);
        let Some(next) = cached.next else {
            break;
        };
        if next.entry_id != user.id {
            break;
        }
        anchor = next;
    }
    rows
}

fn render_transcript(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    viewport: &mut TranscriptViewport,
    row_cache: &mut TranscriptRowCache,
    browse_selected: Option<TranscriptEntryId>,
    palette: Palette,
) {
    let content_width = usize::from(area.width.saturating_sub(2)).max(1);
    let visible_lines = usize::from(area.height.saturating_sub(2)).max(1);
    viewport.set_geometry(&state.transcript, row_cache, content_width, visible_lines);
    let sticky = sticky_user_rows(state, viewport, row_cache, content_width);
    let mut rows = viewport.visible_rows(&state.transcript, row_cache);
    if !sticky.is_empty()
        && !rows
            .iter()
            .any(|row| row.anchor.entry_id == sticky[0].anchor.entry_id)
    {
        let drop = sticky.len().min(rows.len());
        rows.drain(..drop);
        let mut combined = sticky;
        combined.append(&mut rows);
        rows = combined;
    }
    let selected_row = browse_selected.and_then(|selected_entry| {
        rows.iter()
            .find(|row| {
                row.anchor.entry_id == selected_entry
                    && matches!(
                        row.kind,
                        TranscriptRowKind::CardAction
                            | TranscriptRowKind::CardGroup
                            | TranscriptRowKind::Thought
                    )
                    && !matches!(
                        row.anchor.position,
                        crate::transcript_view::RowPosition::ThoughtContent(_)
                    )
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
        empty_transcript_lines(state, palette, visible_lines)
    } else {
        rows.into_iter()
            .map(|row| {
                let selected = selected_row == Some(row.anchor);
                let mut style = match row.tone {
                    TranscriptRowTone::Default => Style::default(),
                    TranscriptRowTone::User => Style::default()
                        .fg(palette.success)
                        .add_modifier(Modifier::BOLD),
                    TranscriptRowTone::Assistant if row.kind == TranscriptRowKind::Header => {
                        Style::default()
                            .fg(palette.primary)
                            .add_modifier(Modifier::BOLD)
                    }
                    TranscriptRowTone::Assistant => Style::default().fg(palette.foreground),
                    TranscriptRowTone::Muted => Style::default().fg(palette.muted),
                    TranscriptRowTone::Pending => Style::default()
                        .fg(palette.primary)
                        .add_modifier(Modifier::BOLD),
                    TranscriptRowTone::Success => Style::default()
                        .fg(palette.success)
                        .add_modifier(Modifier::BOLD),
                    TranscriptRowTone::Warning => Style::default()
                        .fg(palette.warning)
                        .add_modifier(Modifier::BOLD),
                    TranscriptRowTone::Error => Style::default()
                        .fg(palette.error)
                        .add_modifier(Modifier::BOLD),
                };
                if selected {
                    style = style.patch(palette.selection());
                }
                if row.spans.len() == 1 {
                    let span = row.spans.into_iter().next().expect("one span exists");
                    Line::styled(span.text, markdown_span_style(style, span.style, palette))
                } else {
                    let spans = if row.spans.is_empty() {
                        vec![Span::styled(String::new(), style)]
                    } else {
                        row.spans
                            .into_iter()
                            .map(|span| {
                                Span::styled(
                                    span.text,
                                    markdown_span_style(style, span.style, palette),
                                )
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
    let paragraph = Paragraph::new(Text::from(lines)).block(
        Block::default()
            .title(title)
            .borders(Borders::ALL)
            .border_style(palette.border()),
    );
    frame.render_widget(paragraph, area);
}

fn selected_detail<'a>(
    state: &'a UiState,
    view: &DetailView,
) -> Option<&'a ToolDetailPresentation> {
    let selected = view.selected_entry()?;
    if let Some(detail) = state
        .history
        .active_exact_detail
        .as_ref()
        .filter(|detail| detail.target == selected)
    {
        return Some(&detail.presentation);
    }
    let entry = state.transcript.entry(selected)?;
    let card = entry.tool_card()?;
    let DetailAvailability::LiveRetained(detail) = &card.structured_detail else {
        return None;
    };
    Some(detail)
}

pub fn render_detail_overlay(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    view: &mut DetailView,
    palette: Palette,
) {
    if let Some(presentation) = selected_detail(state, view) {
        render_detail(frame, area, view, presentation, palette);
    }
}

fn render_detail(
    frame: &mut Frame<'_>,
    area: Rect,
    view: &mut DetailView,
    presentation: &ToolDetailPresentation,
    palette: Palette,
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
                .border_style(palette.border())
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
            Style::default().fg(palette.muted),
        )]
    } else {
        rows.into_iter()
            .map(|row| detail_line(row, palette))
            .collect()
    };
    let title = if presentation.truncated {
        " detail • retained content incomplete "
    } else {
        " detail "
    };
    frame.render_widget(
        Paragraph::new(Text::from(lines)).block(
            Block::default()
                .title(title)
                .borders(Borders::ALL)
                .border_style(palette.border()),
        ),
        chunks[1],
    );
    frame.render_widget(
        Paragraph::new("↑/↓ scroll · PgUp/PgDn · Home/End · Esc close")
            .style(Style::default().fg(palette.muted)),
        chunks[2],
    );
}

fn detail_line(row: DetailViewRow, palette: Palette) -> Line<'static> {
    let style = match row.kind {
        DetailRowKind::Addition => Style::default()
            .fg(palette.addition)
            .bg(palette.addition_background),
        DetailRowKind::Deletion => Style::default()
            .fg(palette.deletion)
            .bg(palette.deletion_background),
        DetailRowKind::Hunk | DetailRowKind::Header => Style::default()
            .fg(palette.primary)
            .add_modifier(Modifier::BOLD),
        DetailRowKind::GrepMatch => Style::default().fg(palette.accent),
        DetailRowKind::Omission | DetailRowKind::Note => Style::default().fg(palette.warning),
        DetailRowKind::Context | DetailRowKind::ReadLine | DetailRowKind::FindPath => {
            Style::default()
        }
    };
    Line::styled(row.text, style)
}

fn markdown_span_style(base: Style, semantic: TranscriptSpanStyle, palette: Palette) -> Style {
    let mut style = match semantic.block {
        BlockStyle::Normal => base,
        BlockStyle::Heading(level) => {
            let color = if level <= 2 {
                palette.primary
            } else {
                palette.secondary
            };
            base.fg(color).add_modifier(Modifier::BOLD)
        }
        BlockStyle::Code => base.fg(palette.foreground).bg(palette.surface),
        BlockStyle::RawHtml => base.fg(palette.muted),
    };
    style = match semantic.inline {
        InlineStyle::Normal => style,
        InlineStyle::Code => style.fg(palette.warning).bg(palette.panel),
        InlineStyle::Link => style.fg(palette.primary).add_modifier(Modifier::UNDERLINED),
        InlineStyle::QuoteMarker => style.fg(palette.muted),
        InlineStyle::ListMarker => style.fg(palette.primary),
    };
    style = match semantic.syntax {
        SyntaxClass::Plain => style,
        SyntaxClass::Comment => style.fg(palette.muted).add_modifier(Modifier::ITALIC),
        SyntaxClass::Keyword => style.fg(palette.accent).add_modifier(Modifier::BOLD),
        SyntaxClass::String => style.fg(palette.success),
        SyntaxClass::Number | SyntaxClass::Constant => style.fg(palette.warning),
        SyntaxClass::Type => style.fg(palette.secondary),
        SyntaxClass::Function => style.fg(palette.primary),
        SyntaxClass::Variable => style.fg(palette.foreground),
        SyntaxClass::Operator => style.fg(palette.warning),
        SyntaxClass::Punctuation => style.fg(palette.foreground),
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

fn render_composer(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    editor: &PromptEditor,
    focused: bool,
    palette: Palette,
) -> Option<mouse::Editor> {
    let queued_total = state
        .queued_steering()
        .saturating_add(state.queued_follow_ups());
    let title = if state.active_prompt_editable() {
        let omitted = queued_total.saturating_sub(3);
        format!(
            " queue steer:{} later:{}{} ",
            state.queued_steering(),
            state.queued_follow_ups(),
            (omitted > 0)
                .then(|| format!(" +{omitted}"))
                .unwrap_or_default()
        )
    } else if state.session_operation.is_some() {
        " session ".into()
    } else {
        match state.view_status {
            ViewStatus::Idle => " prompt ",
            ViewStatus::Running => " working ",
            ViewStatus::WaitingForApproval => " approval required ",
            ViewStatus::WaitingForTrust => " trust required ",
            ViewStatus::Error => " prompt failed ",
        }
        .into()
    };
    let border_style = match state.view_status {
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust => {
            Style::default().fg(palette.warning)
        }
        ViewStatus::Error => Style::default().fg(palette.error),
        _ => palette.border(),
    };
    let block = Block::default()
        .title(title)
        .borders(Borders::ALL)
        .border_style(border_style);
    let inner = block.inner(area);
    if editable(state) {
        frame.render_widget(block, area);
        let preview_rows = if state.active_prompt_editable() {
            queued_total
                .min(3)
                .min(usize::from(inner.height.saturating_sub(1)))
        } else {
            0
        };
        if preview_rows > 0 {
            let preview_area = Rect {
                x: inner.x,
                y: inner.y,
                width: inner.width,
                height: u16::try_from(preview_rows).unwrap_or(inner.height),
            };
            frame.render_widget(
                Paragraph::new(Text::from(queue_preview_lines(
                    state,
                    preview_rows,
                    usize::from(inner.width),
                ))),
                preview_area,
            );
        }
        let editor_area = Rect {
            x: inner.x,
            y: inner
                .y
                .saturating_add(u16::try_from(preview_rows).unwrap_or(inner.height)),
            width: inner.width,
            height: inner
                .height
                .saturating_sub(u16::try_from(preview_rows).unwrap_or(inner.height)),
        };
        let projection = editor.projection();
        let row = projection.cursor_row();
        let column = projection.cursor_column();
        let vertical_scroll = row.saturating_sub(usize::from(editor_area.height.saturating_sub(1)));
        let horizontal_scroll =
            column.saturating_sub(usize::from(editor_area.width.saturating_sub(1)));
        let cursor_visible_row = row.saturating_sub(vertical_scroll);
        let display_text = composer_visible_text(
            &projection,
            vertical_scroll,
            horizontal_scroll,
            usize::from(editor_area.width),
            usize::from(editor_area.height),
            cursor_visible_row,
        );
        let cursor_horizontal_scroll = display_text.cursor_horizontal_scroll;
        frame.render_widget(Paragraph::new(display_text.text), editor_area);
        let cursor_x = editor_area.x.saturating_add(
            u16::try_from(column.saturating_sub(cursor_horizontal_scroll)).unwrap_or(u16::MAX),
        );
        let cursor_y = editor_area
            .y
            .saturating_add(u16::try_from(row.saturating_sub(vertical_scroll)).unwrap_or(u16::MAX));
        if focused && cursor_x < editor_area.right() && cursor_y < editor_area.bottom() {
            frame.set_cursor_position((cursor_x, cursor_y));
        }
        return Some(mouse::Editor {
            area: editor_area,
            revision: editor.revision(),
            first_line: vertical_scroll,
            column_starts: display_text.column_starts,
        });
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
        return None;
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
    None
}

fn queue_preview_lines(state: &UiState, max_rows: usize, width: usize) -> Vec<Line<'static>> {
    state
        .queue_items()
        .take(max_rows)
        .map(|(kind, _, content)| {
            let label = match kind {
                QueueKind::Steering => "steer",
                QueueKind::FollowUp => "later",
            };
            Line::from(bounded_queue_preview(
                &format!("{label}: {}", bounded_decision_preview(content)),
                width,
            ))
        })
        .collect()
}

fn bounded_queue_preview(content: &str, width: usize) -> String {
    if content.width() <= width {
        return content.into();
    }
    if width <= 1 {
        return "…".chars().take(width).collect();
    }
    let mut preview = String::new();
    let mut used = 0_usize;
    for grapheme in content.graphemes(true) {
        let grapheme_width = grapheme.width();
        if used.saturating_add(grapheme_width) > width - 1 {
            break;
        }
        preview.push_str(grapheme);
        used = used.saturating_add(grapheme_width);
    }
    preview.push('…');
    preview
}

struct ComposerVisibleText {
    text: String,
    cursor_horizontal_scroll: usize,
    column_starts: Vec<usize>,
}

fn composer_visible_text(
    projection: &PromptProjection<'_>,
    vertical_scroll: usize,
    horizontal_scroll: usize,
    width: usize,
    height: usize,
    cursor_visible_row: usize,
) -> ComposerVisibleText {
    let source_text = projection.text();
    let mut visible = String::new();
    let visible_width = width.max(1);
    let visible_height = height.max(1);
    let mut cursor_horizontal_scroll = horizontal_scroll;
    let mut column_starts = Vec::new();
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
        column_starts.push(window.effective_start);
        visible.push_str(&window.text);
    }
    ComposerVisibleText {
        text: visible,
        cursor_horizontal_scroll,
        column_starts,
    }
}

pub(crate) struct SourceDisplayColumnWindow {
    pub text: String,
    pub effective_start: usize,
}

pub(crate) fn source_display_column_window(
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

fn render_compact_notice(frame: &mut Frame<'_>, area: Rect, notice: &str, palette: Palette) {
    frame.render_widget(
        Paragraph::new(sanitize_for_terminal(notice))
            .alignment(Alignment::Center)
            .style(Style::default().fg(palette.warning))
            .wrap(Wrap { trim: true }),
        area,
    );
}

fn join_hints(parts: impl IntoIterator<Item = String>, width: usize) -> String {
    let mut out = String::new();
    for part in parts {
        if part.is_empty() {
            continue;
        }
        let candidate = if out.is_empty() {
            part
        } else {
            format!("{out} · {part}")
        };
        if !out.is_empty() && candidate.width() > width {
            break;
        }
        out = candidate;
    }
    out
}

fn primary_label(bindings: &Bindings, action: KeyAction) -> String {
    let label = bindings.label(action);
    label
        .split(" / ")
        .next()
        .filter(|part| !part.is_empty())
        .unwrap_or("Unbound")
        .to_string()
}

fn empty_transcript_lines(state: &UiState, palette: Palette, height: usize) -> Vec<Line<'static>> {
    let muted = Style::default().fg(palette.muted);
    if state.context.loading() {
        return vec![Line::styled(
            "Refreshing context… You can keep editing your draft.",
            muted,
        )];
    }
    if !editable(state) {
        return Vec::new();
    }
    let mut lines = vec![Line::styled(EMPTY_TRANSCRIPT_HINT, muted)];
    if height < 3 {
        return lines;
    }
    lines.push(Line::default());
    if state.provider.is_none() {
        lines.push(Line::styled(
            "Use /connect to add a provider, then type a prompt.",
            muted,
        ));
    } else if let Some(identity) = header_identity(state) {
        lines.push(Line::styled(sanitize_for_terminal(&identity), muted));
        if height >= 5 {
            lines.push(Line::styled(
                "/resume previous sessions. @ to mention a file.",
                muted,
            ));
        }
    }
    lines
}

fn footer_hints(state: &UiState, bindings: &Bindings, width: usize) -> String {
    let parts = if matches!(state.view_status, ViewStatus::WaitingForApproval) {
        vec![
            "y once".into(),
            "t tool".into(),
            "a all".into(),
            "n deny".into(),
            "Ctrl+G help".into(),
        ]
    } else if matches!(state.view_status, ViewStatus::WaitingForTrust) {
        vec!["y trust".into(), "n deny".into(), "Ctrl+G help".into()]
    } else if state.active_prompt_editable() {
        vec![
            format!("{} steer", primary_label(bindings, KeyAction::Submit)),
            format!(
                "{} later",
                primary_label(bindings, KeyAction::AlternateSubmit)
            ),
            "Esc/Ctrl-C cancels".into(),
            format!(
                "{} restore",
                primary_label(bindings, KeyAction::RestoreQueue)
            ),
            "Ctrl+G help".into(),
        ]
    } else if state.cancel_requested {
        vec!["Ctrl+G help".into()]
    } else if matches!(state.view_status, ViewStatus::Running)
        || state.interaction_status == crate::reducer::InteractionStatus::Compacting
    {
        vec!["Esc/Ctrl-C cancels".into(), "Ctrl+G help".into()]
    } else {
        vec![
            format!("{} send", primary_label(bindings, KeyAction::Submit)),
            "/ commands".into(),
            "@ files".into(),
            "Ctrl+G help".into(),
            format!("{} history", primary_label(bindings, KeyAction::History)),
            format!("{} newline", primary_label(bindings, KeyAction::Newline)),
        ]
    };
    join_hints(parts, width)
}

fn render_footer(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    notice: Option<&str>,
    palette: Palette,
    bindings: &Bindings,
) {
    let width = usize::from(area.width);
    let (content, style) = match notice {
        Some(notice) => (
            sanitize_for_terminal(notice),
            Style::default().fg(palette.warning),
        ),
        None => (
            footer_hints(state, bindings, width),
            Style::default().fg(palette.muted),
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
    state.editor_editable()
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
    use crate::reducer::{
        ActiveCommand, ActiveCommandType, BackendEvent, CommandIdSource, CommandKind,
        InteractionStatus, PendingApproval, reduce,
    };
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use serde_json::json;

    #[derive(Default)]
    struct TestIds(u64);

    impl CommandIdSource for TestIds {
        fn next_id(&mut self, kind: CommandKind) -> String {
            self.0 += 1;
            format!("{}-{}", kind.prefix(), self.0)
        }
    }

    fn connection() -> ConnectionInfo {
        ConnectionInfo {
            backend_version: "0.9.0".into(),
            protocol_version: 3,
            event_schema_version: 35,
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
        render_to_string_with_bindings(width, height, state, editor, notice, &Bindings::default())
    }

    fn render_to_string_with_bindings(
        width: u16,
        height: u16,
        state: &UiState,
        editor: &PromptEditor,
        notice: Option<&str>,
        bindings: &Bindings,
    ) -> String {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut row_cache = TranscriptRowCache::default();
        terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    state,
                    &mut viewport,
                    &mut row_cache,
                    editor,
                    &connection(),
                    notice,
                    None,
                    true,
                    None,
                    Palette::default(),
                    bindings,
                );
            })
            .unwrap();
        terminal.backend().to_string()
    }

    #[test]
    fn fetched_exact_detail_is_selected_for_rendering() {
        let mut state = UiState::new("fake".into(), None, None);
        let target = state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "read-1".into(),
                name: "read".into(),
                arguments: json!({"path": "README.md"}),
                detail_source: crate::tool_detail::ToolDetailSource::None,
            });
        let presentation = ToolDetailPresentation {
            kind: crate::tool_detail::DetailPresentationKind::Read,
            title: "README.md".into(),
            summary: "1 line".into(),
            additions: 0,
            deletions: 0,
            rows: Vec::new(),
            truncated: false,
        };
        state.history.active_exact_detail = Some(crate::reducer::ActiveExactDetail {
            target,
            presentation: presentation.clone(),
        });
        let mut view = DetailView::default();
        view.open(target, &presentation);

        assert_eq!(selected_detail(&state, &view), Some(&presentation));
    }

    #[test]
    fn idle_screen_contains_live_composer_and_contract() {
        let state = UiState::new("fake".into(), Some("model-x".into()), None);
        let mut editor = PromptEditor::default();
        editor.insert_paste("hello");
        let rendered = render_to_string(80, 18, &state, &editor);
        assert!(rendered.contains("fake/model-x"));
        assert!(rendered.contains("hello"));
        assert!(rendered.contains("Enter send"));
        assert!(rendered.contains("/ commands"));
        assert!(rendered.contains("Ctrl+G help"));
        assert!(!rendered.contains("rpc v"));
        assert!(!rendered.contains("events v"));
        assert!(!rendered.contains("backend "));
        assert!(!rendered.contains("PgUp"));
        assert!(!rendered.contains("theme"));
    }

    #[test]
    fn empty_idle_transcript_invites_prompt_and_commands() {
        let state = UiState::new("fake".into(), Some("model-x".into()), None);
        let rendered = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(rendered.contains(EMPTY_TRANSCRIPT_HINT));
        assert!(rendered.contains("/resume previous sessions"));
        assert!(rendered.contains("fake/model-x"));
        let compact = render_to_string(30, 8, &state, &PromptEditor::default());
        assert!(compact.contains("WISP"));
        assert!(compact.contains("Enter send") || compact.contains("/ commands"));
        assert!(!compact.contains("/resume previous sessions"));
    }

    #[test]
    fn follow_tail_pins_the_compact_user_turn_above_a_long_assistant_echo() {
        let mut state = UiState::new("fake".into(), None, None);
        let raw = format!("{}\n🙂END", "界".repeat(2001));
        state.transcript.append_prompt_with_display(
            raw.clone(),
            Some("[Pasted content #1: 2006 chars, 2 lines, 6011 bytes]".into()),
        );
        state
            .transcript
            .complete_message(1, format!("fake response to: {raw}"));
        let rendered = render_to_string(80, 24, &state, &PromptEditor::default());
        assert!(rendered.contains("Pasted content #1"));
        assert!(rendered.contains("2006"));
        assert!(rendered.contains("6011"));
    }

    #[test]
    fn footer_uses_resolved_binding_labels() {
        let state = UiState::new("fake".into(), Some("model-x".into()), None);
        let bindings = Bindings::from_json(
            r#"{"prompt.submit":["ctrl+enter"],"history.open":["f4"],"theme.toggle":[]}"#,
        )
        .unwrap();
        let rendered = render_to_string_with_bindings(
            220,
            18,
            &state,
            &PromptEditor::default(),
            None,
            &bindings,
        );
        assert!(rendered.contains("Ctrl+Enter send"));
        assert!(rendered.contains("F4 history"));
        assert!(rendered.contains("Ctrl+G help"));
        assert!(!rendered.contains("Unbound theme"));
        assert!(!rendered.contains("Ctrl+T theme"));
    }

    #[test]
    fn footer_matches_approval_and_running_workflows() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        let approval = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(approval.contains("y once"));
        assert!(approval.contains("n deny"));
        assert!(!approval.contains("Enter send"));

        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let running = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(running.contains("Enter steer"));
        assert!(running.contains("Alt+Enter later"));
        assert!(running.contains("working"));
        assert!(!running.contains("running"));

        state.current_command = None;
        state.interaction_status = InteractionStatus::Compacting;
        let compacting = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(compacting.contains("Esc/Ctrl-C cancels"));
        assert!(compacting.contains("compacting"));
        assert!(!compacting.contains("Enter send"));
    }

    #[test]
    fn busy_empty_screen_does_not_invite_early_prompt_input() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        let rendered = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(!rendered.contains(EMPTY_TRANSCRIPT_HINT));
        assert!(!rendered.contains("/connect"));
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
        assert!(cancelling.contains("Ctrl+G help"));
        assert!(!cancelling.contains("Esc/Ctrl-C cancels"));
        assert!(!cancelling.contains("Enter send"));
    }

    #[test]
    fn active_queue_editor_renders_bounded_sanitized_previews_and_counts() {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut ids = TestIds::default();
        reduce(
            &mut state,
            crate::reducer::UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec![
                    "first\u{1b}[2J\u{202e}spoof".into(),
                    format!("{}TAIL", "x".repeat(500)),
                ],
                follow_up: vec!["later one".into(), "later omitted".into()],
            }),
            &mut ids,
        )
        .unwrap();
        let mut editor = PromptEditor::default();
        editor.insert_paste("new steering");

        let rendered = render_to_string(80, 18, &state, &editor);
        assert!(rendered.contains("new steering"));
        assert!(rendered.contains("queue steer:2 later:2 +1"));
        assert!(rendered.contains("steer: first�[2J�spoof"));
        assert!(rendered.contains("later: later one"));
        assert!(!rendered.contains("TAIL"));
        assert!(!rendered.contains("steering arrives"));
        assert!(rendered.contains("Enter steer"));
        assert!(rendered.contains("Alt+Enter later"));
        assert!(!rendered.contains('\u{1b}'));
        assert!(!rendered.contains('\u{202e}'));

        let tiny = render_to_string(30, 8, &state, &editor);
        assert!(!tiny.contains("terminal too"));
        assert!(tiny.contains("s:2/l:2") || tiny.contains("steer:2"));
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
        editor.replace_range(0..0, &prompt);
        assert_eq!(
            source_display_column_window(&prompt, 70_000, 4).text,
            "TAIL"
        );
        let rendered = render_to_string(40, 14, &state, &editor);
        assert!(rendered.contains("TAIL"));
    }

    #[test]
    fn composer_renders_large_paste_marker_from_display_projection() {
        let state = UiState::new("fake".into(), None, None);
        let mut editor = PromptEditor::default();
        let raw = format!("{}\nTAIL", "界".repeat(2_001));
        editor.insert_paste(&raw);

        let rendered = render_to_string(80, 14, &state, &editor);
        assert!(rendered.contains("Pasted content #1"));
        assert!(!rendered.contains("TAIL"));
        assert_eq!(editor.text(), raw);
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
        assert_eq!(pending_fg, Palette::default().primary);
        assert!(pending_modifiers.contains(Modifier::BOLD));

        state
            .transcript
            .observe_approval_resolved("call-1", true, None);
        state
            .transcript
            .observe_tool_result(tool_result("call-1", "contents"));
        let complete = draw(&state);
        assert!(complete.backend().to_string().contains("Read  README.md"));
        assert!(!complete.backend().to_string().contains("contents"));
        let (success_fg, _, success_modifiers) =
            style_at_text(complete.backend(), "Read  README.md").unwrap();
        assert_eq!(success_fg, Palette::default().success);
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
        assert!(collapsed.contains("file.txt"));
        assert!(!collapsed.contains("+ new value"));
        assert!(collapsed.contains("Ctrl+G help"));
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
                    true,
                    None,
                    Palette::default(),
                    &Bindings::default(),
                );
            })
            .unwrap();
        let visible = browse_viewport.visible_rows(&state.transcript, &mut browse_cache);
        let selected = visible
            .iter()
            .find(|row| row.anchor.entry_id == card_id && row.kind == TranscriptRowKind::CardAction)
            .expect("collapsed card action is visible");
        let visible_text = selected.plain_text();
        let (_, selected_background, _) =
            style_at_text(browse_terminal.backend(), &visible_text).unwrap();
        assert_eq!(selected_background, Palette::default().primary);

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
                    false,
                    None,
                    Palette::default(),
                    &Bindings::default(),
                );
                let area = overlay_area(frame.area()).unwrap();
                clear_overlay(frame, area, Palette::default());
                render_detail_overlay(frame, area, &state, &mut detail_view, Palette::default());
            })
            .unwrap();
        let rendered = terminal.backend().to_string();
        assert!(rendered.contains("live retained detail"));
        assert!(rendered.contains("- old�[2J"));
        assert!(rendered.contains("+ new value"));
        let (added_fg, _, _) = style_at_text(terminal.backend(), "+ new value").unwrap();
        let (deleted_fg, _, _) = style_at_text(terminal.backend(), "- old�[2J").unwrap();
        assert_eq!(added_fg, Palette::default().addition);
        assert_eq!(deleted_fg, Palette::default().deletion);
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
        assert_eq!(keyword_fg, Palette::default().accent);
        assert_eq!(keyword_bg, Palette::default().surface);
        assert!(keyword_modifiers.contains(Modifier::BOLD));
        let (comment_fg, comment_bg, comment_modifiers) =
            style_at_text(terminal.backend(), "// note").unwrap();
        assert_eq!(comment_fg, Palette::default().muted);
        assert_eq!(comment_bg, Palette::default().surface);
        assert!(comment_modifiers.contains(Modifier::ITALIC));
        let (string_fg, string_bg, _) = style_at_text(terminal.backend(), "\"text\"").unwrap();
        assert_eq!(string_fg, Palette::default().success);
        assert_eq!(string_bg, Palette::default().surface);
    }

    #[test]
    fn monochrome_diff_rows_keep_readable_pairs_and_noncolor_signs() {
        for theme in crate::theme::themes() {
            for (kind, text) in [
                (DetailRowKind::Addition, "+ added"),
                (DetailRowKind::Deletion, "- deleted"),
            ] {
                let line = detail_line(
                    DetailViewRow {
                        anchor: crate::detail_view::DetailAnchor {
                            row_key: 0,
                            byte_offset: 0,
                        },
                        kind,
                        text: text.into(),
                    },
                    theme.palette(true),
                );
                assert_eq!(line.spans[0].content, text);
                let style = line.style.patch(line.spans[0].style);
                assert!(
                    crate::theme::contrast_ratio(style.fg.unwrap(), style.bg.unwrap()) >= 4.5,
                    "{}",
                    theme.slug
                );
            }
        }
    }

    #[test]
    fn semantic_markdown_styles_map_to_terminal_styles() {
        let palette = Palette::default();
        let heading = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                block: BlockStyle::Heading(1),
                ..TranscriptSpanStyle::default()
            },
            palette,
        );
        assert_eq!(heading.fg, Some(palette.primary));
        assert!(heading.add_modifier.contains(Modifier::BOLD));

        let code = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                inline: InlineStyle::Code,
                ..TranscriptSpanStyle::default()
            },
            palette,
        );
        assert_eq!(code.fg, Some(palette.warning));
        assert!(code.bg.is_some());

        let link = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                inline: InlineStyle::Link,
                ..TranscriptSpanStyle::default()
            },
            palette,
        );
        assert!(link.add_modifier.contains(Modifier::UNDERLINED));

        let uniform_code = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                block: BlockStyle::Code,
                ..TranscriptSpanStyle::default()
            },
            palette,
        );
        assert_eq!(uniform_code.fg, Some(palette.foreground));
        assert_eq!(uniform_code.bg, Some(palette.surface));

        let keyword = markdown_span_style(
            Style::default().bg(palette.surface),
            TranscriptSpanStyle {
                block: BlockStyle::Code,
                syntax: SyntaxClass::Keyword,
                ..TranscriptSpanStyle::default()
            },
            palette,
        );
        assert_eq!(keyword.fg, Some(palette.accent));
        assert_eq!(keyword.bg, Some(palette.surface));
        assert!(keyword.add_modifier.contains(Modifier::BOLD));

        let comment = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                block: BlockStyle::Code,
                syntax: SyntaxClass::Comment,
                ..TranscriptSpanStyle::default()
            },
            palette,
        );
        assert_eq!(comment.fg, Some(palette.muted));
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
