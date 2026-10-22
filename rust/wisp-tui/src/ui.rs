use crate::detail_view::{DetailView, DetailViewRow};
use crate::keybindings::{Action as KeyAction, Bindings};
use crate::markdown::{BlockStyle, InlineStyle, TranscriptSpanStyle};
use crate::mouse;
use crate::prompt_editor::{PromptEditor, PromptProjection};
use crate::prompt_highlighting::{self, Highlight, Kind as PromptHighlightKind};
use crate::reducer::{UiState, ViewStatus};
use crate::side_panel::{self, SidePanel};
use crate::startup_logo::{self, LogoChoice};
use crate::syntax::SyntaxClass;
use crate::theme::Palette;
use crate::tool_cards::{ProcessDisplayState, ToolStatus};
use crate::tool_detail::{DetailAvailability, DetailRowKind, ToolDetailPresentation};
use crate::transcript::{TranscriptEntry, TranscriptEntryId, TranscriptRole};
use crate::transcript_view::{
    BrowseTarget, RESULT_CONNECTOR, RESULT_INDENT, RowAnchor, RowGutter, RowPosition,
    TranscriptRow, TranscriptRowCache, TranscriptRowKind, TranscriptRowTone, TranscriptViewport,
};
use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Margin, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Padding, Paragraph, Wrap};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

const MIN_TERMINAL_WIDTH: u16 = 30;
const MIN_TERMINAL_HEIGHT: u16 = 8;
/// The composer's ceiling with its frame and blank row. A framed composer without the
/// blank row stops one row lower, and an unframed one two rows lower, so neither
/// spends the rows they leave out on more draft.
const MAX_COMPOSER_HEIGHT: u16 = 10;
/// From this conversation height the framed composer leaves a blank row between the
/// draft and its identity; a shorter screen keeps that row for the transcript.
const COMPOSER_GAP_MIN_HEIGHT: u16 = 24;
const COMPOSER_TAB_WIDTH: usize = 4;
const DECISION_PREVIEW_GRAPHEMES: usize = 160;
const DECISION_PREVIEW_JSON_BYTES: usize = 1024;
pub(crate) const EMPTY_TRANSCRIPT_HINT: &str = "Type a prompt or / for commands.";
const EMPTY_TRANSCRIPT_TAGLINE: &str = "A coding agent that stays in sync";
const EMPTY_TRANSCRIPT_PANEL_WIDTH: usize = 40;
const PINNED_USER_ROWS: usize = 4;
/// Below this surface height the decision panel owns the whole surface, with no
/// transcript or footer, so its choices stay visible down to the 30x8 minimum.
const DECISION_PANEL_SHARED_MIN_HEIGHT: u16 = 11;
/// Rows a docked decision panel leaves for the transcript and footer above and below it.
const DECISION_PANEL_RESERVED_ROWS: u16 = 3;
const DECISION_DETAIL_MAX_ROWS: usize = 3;
pub(crate) const APPROVAL_CHOICES: usize = 4;
pub(crate) const TRUST_CHOICES: usize = 2;
const COMPOSER_PREFIX: &str = "› ";
const CONTENT_PADDING: u16 = 1;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConnectionInfo {
    pub backend_version: String,
}

pub fn decision_context_visible(area: Rect) -> bool {
    area.width >= MIN_TERMINAL_WIDTH && area.height >= MIN_TERMINAL_HEIGHT
}

fn composer_height(area: Rect, state: &UiState, layout: Option<&ComposerLayout>) -> u16 {
    let framed = area.height >= 16;
    let gap = composer_gap(area);
    // The footer, the transcript's two rows, and the status line while running.
    let ceiling = area
        .height
        .saturating_sub(3 + status_line_height(state))
        .clamp(
            2,
            MAX_COMPOSER_HEIGHT - u16::from(!framed) - u16::from(!(framed && gap)),
        );
    if editable(state) {
        u16::try_from(
            layout
                .map_or(1, |layout| layout.rows.len())
                // A framed composer adds its borders and the identity row, and on a
                // tall screen a blank row between the draft and the identity.
                .saturating_add(if framed { 3 + usize::from(gap) } else { 0 }),
        )
        .unwrap_or(MAX_COMPOSER_HEIGHT)
        .clamp(2, ceiling)
    } else {
        2
    }
}

/// Whether a conversation this tall gives the framed composer its blank row.
fn composer_gap(area: Rect) -> bool {
    area.height >= COMPOSER_GAP_MIN_HEIGHT
}

fn conversation_surface(area: Rect) -> Rect {
    area.inner(Margin {
        horizontal: if area.width >= 60 { 2 } else { 1 },
        vertical: 0,
    })
}

fn content_area(area: Rect) -> Rect {
    area.inner(Margin {
        horizontal: CONTENT_PADDING,
        vertical: 0,
    })
}

fn decision_pending(state: &UiState) -> bool {
    matches!(
        state.view_status,
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
    )
}

/// Anchor suggestions above the composer without changing transcript geometry.
pub(crate) fn file_picker_area(
    area: Rect,
    state: &UiState,
    editor: &PromptEditor,
    layout_cache: &mut ComposerLayoutCache,
) -> Option<Rect> {
    if !decision_context_visible(area) {
        return None;
    }
    // At short sizes a tall draft leaves no free strip. Cover the upper rows rather
    // than hide all choices or change the transcript's layout to make room.
    let composer = conversation_surface(area);
    let editor_width = composer_editor_width(composer.width);
    let layout = editable(state).then(|| layout_cache.layout(editor, editor_width));
    let bottom = area
        .bottom()
        .saturating_sub(composer_height(composer, state, layout) + status_line_height(state) + 1)
        .max(area.y + 4);
    let height = (bottom - area.y).min(12);
    Some(Rect::new(
        area.x,
        bottom - height,
        area.width.min(100),
        height,
    ))
}

/// Dock a popup of up to `height` rows directly above the composer, spanning the
/// conversation's width, like the `/` command suggestions.
pub(crate) fn docked_popup_area(
    area: Rect,
    state: &UiState,
    editor: &PromptEditor,
    layout_cache: &mut ComposerLayoutCache,
    height: u16,
) -> Option<Rect> {
    if !decision_context_visible(area) {
        return None;
    }
    let surface = conversation_surface(area);
    let editor_width = composer_editor_width(surface.width);
    let layout = editable(state).then(|| layout_cache.layout(editor, editor_width));
    let bottom = surface
        .bottom()
        .saturating_sub(composer_height(surface, state, layout) + status_line_height(state) + 1)
        .max(surface.y + 4);
    let height = (bottom - surface.y).min(height);
    Some(Rect::new(surface.x, bottom - height, surface.width, height))
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
    frame.render_widget(Block::default().style(palette.overlay()), area);
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
    let mut composer_layout_cache = ComposerLayoutCache::default();
    render_interactive(
        frame,
        state,
        viewport,
        row_cache,
        &mut composer_layout_cache,
        editor,
        connection,
        notice,
        None,
        true,
        None,
        0,
        Palette::default(),
        &Bindings::default(),
        LogoChoice::AdalBraille,
        None,
        None,
        None,
    );
}

#[allow(clippy::too_many_arguments)]
pub fn render_interactive(
    frame: &mut Frame<'_>,
    state: &UiState,
    viewport: &mut TranscriptViewport,
    row_cache: &mut TranscriptRowCache,
    composer_layout_cache: &mut ComposerLayoutCache,
    editor: &PromptEditor,
    connection: &ConnectionInfo,
    notice: Option<&str>,
    browse_selected: Option<BrowseTarget>,
    composer_focused: bool,
    completion: Option<&crate::commands::CompletionView<'_>>,
    activity_frame: u8,
    palette: Palette,
    bindings: &Bindings,
    startup_logo: LogoChoice,
    decision_choice: Option<usize>,
    activity_elapsed: Option<std::time::Duration>,
    side_panel: Option<&SidePanel>,
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

    // A wide terminal docks the side panel at its right; the conversation lays out in
    // the column beside it.
    let column = match side_panel.filter(|panel| panel.docked()) {
        Some(panel) => {
            let (column, panel_area) = panel.split(area);
            side_panel::render(
                frame,
                panel_area,
                state,
                panel,
                bindings,
                activity_frame,
                activity_elapsed,
                palette,
            );
            column
        }
        None => area,
    };
    let area = conversation_surface(column);

    // A pending approval or trust decision docks above the footer in place of the
    // composer (the draft is kept). Decisions are keyboard-only, so no mouse regions.
    if decision_pending(state) {
        if area.height < DECISION_PANEL_SHARED_MIN_HEIGHT {
            render_decision_panel(frame, area, state, notice, decision_choice, palette);
            return mouse::Conversation::default();
        }
        let panel_height = decision_panel_height(state, area);
        let chunks = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Min(2),
                Constraint::Length(panel_height),
                Constraint::Length(1),
            ])
            .split(area);
        let (jump_painted, _) = render_transcript(
            frame,
            chunks[0],
            state,
            viewport,
            row_cache,
            browse_selected,
            connection,
            activity_frame,
            palette,
            bindings,
            startup_logo,
        );
        render_decision_panel(frame, chunks[1], state, notice, decision_choice, palette);
        render_footer(
            frame,
            content_area(chunks[2]),
            state,
            viewport,
            notice,
            palette,
            bindings,
            jump_painted.is_some(),
            false,
            column.width,
        );
        // The jump label is drawn but not clickable: clicks never act while deciding.
        return mouse::Conversation::default();
    }

    let editor_width = composer_editor_width(area.width);
    let composer_layout =
        editable(state).then(|| composer_layout_cache.layout(editor, editor_width));
    let composer_height = composer_height(area, state, composer_layout);
    let status_height = status_line_height(state);
    let completion_height = completion.map_or(0, |view| {
        (view.items.len().min(5) as u16).min(
            area.height
                .saturating_sub(composer_height + status_height + 3),
        )
    });
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(if completion_height > 0 { 0 } else { 2 }),
            Constraint::Length(completion_height),
            Constraint::Length(status_height),
            Constraint::Length(composer_height),
            Constraint::Length(1),
        ])
        .split(area);

    let (jump_to_latest, fold_rows) = render_transcript(
        frame,
        chunks[0],
        state,
        viewport,
        row_cache,
        browse_selected,
        connection,
        activity_frame,
        palette,
        bindings,
        startup_logo,
    );
    let completion_rows = completion
        .filter(|_| completion_height > 0)
        .map(|view| crate::commands::render_completion(frame, chunks[1], view, palette))
        .unwrap_or_default();
    if status_height > 0 {
        render_status_line(
            frame,
            content_area(chunks[2]),
            state,
            activity_frame,
            activity_elapsed,
            palette,
        );
    }
    let padded = area.height >= 16;
    let editor = render_composer(
        frame,
        chunks[3],
        state,
        composer_layout,
        composer_focused,
        padded,
        composer_gap(area),
        palette,
        bindings,
    );
    render_footer(
        frame,
        content_area(chunks[4]),
        state,
        viewport,
        notice,
        palette,
        bindings,
        jump_to_latest.is_some(),
        composer_framed(state, padded, chunks[3]),
        column.width,
    );
    mouse::Conversation {
        transcript: Rect {
            width: column.right().saturating_sub(chunks[0].x),
            ..chunks[0]
        },
        // The jump label sits on the transcript's last row, which holds no transcript text.
        selectable: Rect {
            height: chunks[0]
                .height
                .saturating_sub(u16::from(jump_to_latest.is_some())),
            ..content_area(chunks[0])
        },
        jump_to_latest,
        editor,
        completion: completion_rows,
        completion_visible: completion_height > 0,
        fold_rows,
    }
}

pub(crate) fn status_label(state: &UiState) -> String {
    if state.configuration_active() {
        "configuring".into()
    } else if state.interaction_status == crate::reducer::InteractionStatus::Compacting {
        "compacting".into()
    } else {
        match state.view_status {
            ViewStatus::Idle => "idle".into(),
            ViewStatus::Running => "working".into(),
            ViewStatus::WaitingForApproval => "approval".into(),
            ViewStatus::WaitingForTrust => "trust".into(),
            ViewStatus::Error => "error".into(),
        }
    }
}

/// The colour of the footer's status label, shared by the composer border so the
/// prompt shows at a glance whether Wisp is idle, working, or waiting on you.
pub(crate) fn status_color(state: &UiState, palette: Palette) -> Color {
    let active = matches!(
        state.view_status,
        ViewStatus::Running
            | ViewStatus::WaitingForApproval
            | ViewStatus::WaitingForTrust
            | ViewStatus::Error
    ) || state.interaction_status == crate::reducer::InteractionStatus::Compacting;
    match state.view_status {
        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust => palette.warning,
        ViewStatus::Error => palette.error,
        _ if active => palette.primary,
        _ => palette.muted,
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

/// Folders longer than this show only their last component.
const WORKSPACE_MAX_COLUMNS: usize = 32;

/// Branch names longer than this end in `…`.
const BRANCH_MAX_COLUMNS: usize = 32;

/// Shorten `text` to `columns` display columns, ending in `…` when cut.
pub(crate) fn clip_columns(text: &str, columns: usize) -> String {
    if text.width() <= columns {
        return text.to_owned();
    }
    let mut clipped = String::new();
    for character in text.chars() {
        if clipped.width() + character.to_string().width() + 1 > columns {
            break;
        }
        clipped.push(character);
    }
    clipped.push('…');
    clipped
}

fn display_workspace(cwd: &str) -> String {
    let home = std::env::var("HOME").ok().filter(|home| !home.is_empty());
    workspace_label(cwd, home.as_deref())
}

/// Show a workspace folder compactly: `~` for the home folder, and only the last
/// component of a long path.
fn workspace_label(cwd: &str, home: Option<&str>) -> String {
    let shown = match home {
        Some(home) if cwd == home => "~".to_owned(),
        Some(home) => cwd
            .strip_prefix(home)
            .filter(|rest| rest.starts_with('/'))
            .map_or_else(|| cwd.to_owned(), |rest| format!("~{rest}")),
        None => cwd.to_owned(),
    };
    if shown.width() <= WORKSPACE_MAX_COLUMNS {
        return shown;
    }
    let last = shown
        .rsplit('/')
        .find(|part| !part.is_empty())
        .unwrap_or(&shown);
    format!("…/{last}")
}

/// Whether a run is going, so the status line above the composer replaces the
/// footer's status and the transcript's working row.
pub(crate) fn run_in_status_line(state: &UiState) -> bool {
    !state.configuration_active()
        && (state.view_status == ViewStatus::Running
            || state.interaction_status == crate::reducer::InteractionStatus::Compacting)
}

fn footer_leading_parts(state: &UiState, identity_in_composer: bool) -> Vec<String> {
    let mut parts = vec![status_label(state)];
    if state.active_prompt_editable() {
        let steering = state.queued_steering();
        let follow_up = state.queued_follow_ups();
        let omitted = steering.saturating_add(follow_up).saturating_sub(3);
        parts.push(format!("s:{steering}/l:{follow_up}"));
        if omitted > 0 {
            parts.push(format!("+{omitted}"));
        }
    }
    // Queue and mode stay ahead of identity so a long model name cannot hide
    // counts, plan/build, or ctx on a 30-column row or a notice-budget footer. A
    // framed composer names the mode first on its identity line instead.
    if state.mode_confirmed && !identity_in_composer {
        parts.push(state.mode.as_str().to_string());
    }
    parts
}

/// The status line's closing parts: the identity when the composer cannot show it,
/// and any compaction in progress.
fn footer_trailing_parts(state: &UiState, identity_in_composer: bool) -> Vec<String> {
    let mut parts = Vec::new();
    if !identity_in_composer {
        if let Some(identity) = header_identity(state) {
            parts.push(identity);
        }
    }
    if let Some(reason) = state.context.compaction {
        parts.push(format!("compacting ({})", reason.as_str()));
    }
    parts
}

/// Where commands run, for the footer's left side. Folder and branch names come from
/// the file system and the repository, so they are cleaned of terminal controls.
pub(crate) fn footer_workspace_parts(state: &UiState) -> Vec<String> {
    let mut parts = Vec::new();
    if let Some(cwd) = state.workspace_cwd.as_deref() {
        parts.push(display_workspace(&sanitize_for_terminal(cwd)));
    }
    if let Some(branch) = state.git_branch.as_deref() {
        parts.push(clip_columns(
            &sanitize_for_terminal(branch),
            BRANCH_MAX_COLUMNS,
        ));
    }
    parts
}

fn pinned_user_rows(
    state: &UiState,
    viewport: &TranscriptViewport,
    row_cache: &mut TranscriptRowCache,
    width: usize,
) -> Option<(TranscriptEntryId, Vec<TranscriptRow>)> {
    let user = state
        .transcript
        .entries()
        .iter()
        .rev()
        .find(|entry| entry.role == TranscriptRole::User)?;
    // The live view pins the prompt above its reply. Keep it pinned while reading back through
    // that reply, or the first wheel tick up would drop it; once the top reaches the prompt's
    // own content, the contiguous view shows it in place. While newer history is evicted, the
    // last loaded prompt is not the latest one, so nothing pins.
    let user_index = state.transcript.entry_index(user.id)?;
    let reading_reply = !state.history.tail_evicted
        && viewport.scrolled_top().is_some_and(|top| {
            (top.entry_id == user.id
                && matches!(top.position, RowPosition::Spacer | RowPosition::UserLeadOut))
                || state
                    .transcript
                    .entry_index(top.entry_id)
                    .is_some_and(|index| index > user_index)
        });
    if !viewport.follows_tail() && !reading_reply {
        return None;
    }
    let mut rows = Vec::new();
    let mut anchor = RowAnchor {
        entry_id: user.id,
        position: RowPosition::Header,
    };
    while rows.len() < PINNED_USER_ROWS {
        let cached = row_cache.row_at(&state.transcript, anchor, width)?;
        if cached.row.kind == TranscriptRowKind::Spacer || cached.row.anchor.entry_id != user.id {
            break;
        }
        rows.push(cached.row);
        let Some(next) = cached.next.filter(|next| next.entry_id == user.id) else {
            break;
        };
        anchor = next;
    }
    (!rows.is_empty()).then_some((user.id, rows))
}

fn clipped_tail_assistant_header(
    state: &UiState,
    rows: &[TranscriptRow],
    row_cache: &mut TranscriptRowCache,
    width: usize,
    active_reply: Option<TranscriptEntryId>,
) -> Option<TranscriptRow> {
    let assistant = active_reply
        .and_then(|id| state.transcript.entry(id))
        .or_else(|| state.transcript.entries().last())?;
    if assistant.role != TranscriptRole::Assistant
        || (active_reply.is_none()
            && !rows.iter().any(|row| {
                row.anchor.entry_id == assistant.id
                    && !matches!(
                        row.kind,
                        TranscriptRowKind::Header | TranscriptRowKind::Spacer
                    )
            }))
        || rows
            .iter()
            .any(|row| row.anchor.entry_id == assistant.id && row.kind == TranscriptRowKind::Header)
    {
        return None;
    }
    row_cache
        .row_at(
            &state.transcript,
            RowAnchor {
                entry_id: assistant.id,
                position: RowPosition::Header,
            },
            width,
        )
        .map(|cached| cached.row)
}

#[allow(clippy::too_many_arguments)]
fn render_transcript(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    viewport: &mut TranscriptViewport,
    row_cache: &mut TranscriptRowCache,
    browse_selected: Option<BrowseTarget>,
    connection: &ConnectionInfo,
    activity_frame: u8,
    palette: Palette,
    bindings: &Bindings,
    startup_logo: LogoChoice,
) -> (Option<Rect>, Vec<mouse::FoldRow>) {
    let padding = u16::from(
        !state.transcript.entries().is_empty()
            && area.height >= if decision_pending(state) { 10 } else { 8 },
    );
    let outer = area;
    let mut area = area.inner(Margin {
        horizontal: 0,
        vertical: padding,
    });
    // Keep the outer transcript gutter while adding a highlighted row above "you".
    let user_top_padding = padding > 0
        && state
            .transcript
            .entries()
            .iter()
            .any(|entry| entry.role == TranscriptRole::User);
    if user_top_padding {
        area.y += 1;
        area.height = area.height.saturating_sub(1);
    }
    let conversation = area;
    let active_reply = active_reply(state);
    let status_width = usize::from(content_area(conversation).width);
    // Why the last prompt failed; a run in progress shows in the status line.
    let status_lines = failure_lines(state, status_width, palette);
    let activity_height = if status_lines.is_empty() {
        0
    } else {
        let gap = u16::from(conversation.height >= 8);
        let lines = u16::try_from(status_lines.len()).unwrap_or(u16::MAX);
        conversation.height.saturating_sub(1).min(lines + gap)
    };
    let rows_area = Rect {
        height: conversation.height.saturating_sub(activity_height),
        ..conversation
    };
    let content = content_area(rows_area);
    let content_width = usize::from(content.width).max(1);
    let visible_lines = usize::from(rows_area.height).max(1);
    // Wide terminals set diff cards' old and new side by side (the mock's "wide
    // only"), before sizing so a resize re-anchors on the new layout.
    row_cache.set_side_by_side_width(
        crate::side_panel::docks(frame.area().width).then_some(content_width),
    );
    // Size first: a resize can normalize the scrolled top that decides whether the prompt pins.
    viewport.set_geometry(&state.transcript, row_cache, content_width, visible_lines);
    let mut pinned = pinned_user_rows(state, viewport, row_cache, content_width);
    if let Some((user_id, rows)) = &mut pinned {
        let has_reply = state.transcript.entry_after(*user_id).is_some();
        let reply_rows = if has_reply {
            visible_lines.saturating_sub(1).min(2)
        } else {
            0
        };
        rows.truncate(visible_lines.saturating_sub(reply_rows));
        if visible_lines >= rows.len().saturating_add(reply_rows).saturating_add(1) {
            let mut spacer = rows.last().expect("nonempty user projection").clone();
            spacer.kind = TranscriptRowKind::Spacer;
            spacer.anchor.position = RowPosition::Spacer;
            spacer.spans.clear();
            rows.push(spacer.clone());
            // The plain gap that separates the pinned prompt from its reply, as in place.
            if has_reply && visible_lines >= rows.len().saturating_add(reply_rows).saturating_add(1)
            {
                spacer.role = TranscriptRole::Assistant;
                spacer.anchor.position = RowPosition::UserLeadOut;
                rows.push(spacer);
            }
        }
    }
    let mut rows = viewport.visible_rows(&state.transcript, row_cache);
    if let Some((user_id, mut user_rows)) = pinned {
        let user_index = state.transcript.entry_index(user_id).unwrap_or(usize::MAX);
        rows.retain(|row| {
            state
                .transcript
                .entry_index(row.anchor.entry_id)
                .is_some_and(|index| index > user_index)
        });
        let mut tail_budget = visible_lines.saturating_sub(user_rows.len());
        if rows.len() > tail_budget {
            rows.drain(..rows.len() - tail_budget);
        }
        // A rule only separates two cards; with the card above it cut off, it would hang
        // under the pinned prompt's gap.
        if rows.len() > 1 && rows[0].kind == TranscriptRowKind::Rule {
            rows.remove(0);
        }
        let assistant_header = (tail_budget >= 2)
            .then(|| {
                clipped_tail_assistant_header(state, &rows, row_cache, content_width, active_reply)
            })
            .flatten();
        if let Some(header) = assistant_header {
            tail_budget = tail_budget.saturating_sub(1);
            if rows.len() > tail_budget {
                rows.drain(..rows.len() - tail_budget);
            }
            user_rows.push(header);
        }
        user_rows.append(&mut rows);
        rows = user_rows;
    }
    rows.truncate(visible_lines);
    let painted_rows = rows.len();
    let selected_row = browse_selected.and_then(|selected_entry| {
        rows.iter()
            .find(|row| {
                BrowseTarget::for_row(row) == selected_entry
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
                    BrowseTarget::for_row(row) == selected_entry
                        && matches!(
                            row.kind,
                            TranscriptRowKind::CardDetail | TranscriptRowKind::CardOmission
                        )
                })
            })
            .map(|row| row.anchor)
    });
    let mut fold_rows = Vec::new();
    if rows.is_empty() && status_lines.is_empty() {
        let lines = empty_transcript_lines(
            state,
            connection,
            palette,
            visible_lines,
            content_width,
            bindings,
            startup_logo,
        );
        frame.render_widget(Paragraph::new(Text::from(lines)), content);
    } else {
        let prompts = TurnBounds {
            first: state
                .transcript
                .entries()
                .iter()
                .position(|entry| entry.role == TranscriptRole::User),
            latest: run_prompt_index(state),
        };
        for (offset, row) in rows.iter().enumerate() {
            let area = Rect::new(
                conversation.x,
                conversation.y + offset as u16,
                conversation.width,
                1,
            );
            if matches!(
                row.kind,
                TranscriptRowKind::CardAction
                    | TranscriptRowKind::CardGroup
                    | TranscriptRowKind::Thought
            ) && !matches!(row.anchor.position, RowPosition::ThoughtContent(_))
            {
                fold_rows.push(mouse::FoldRow {
                    y: area.y,
                    entry: row.anchor.entry_id,
                    group: row.kind == TranscriptRowKind::CardGroup,
                });
            }
            if offset == 0
                && user_top_padding
                && row.role == TranscriptRole::User
                && row.anchor.position != RowPosition::UserLeadIn
            {
                frame.render_widget(
                    Block::default().style(palette.user_text()),
                    Rect::new(area.x, area.y - 1, area.width, 1),
                );
                // The prompt's bar runs the full height of its surface, padding included.
                if content_area(area).x > area.x {
                    frame.render_widget(
                        Paragraph::new(Span::styled("▎", palette.user_text().fg(palette.primary))),
                        Rect::new(area.x, area.y - 1, 1, 1),
                    );
                }
            }
            let user_surface = row.role == TranscriptRole::User;
            let pulse = (state.view_status == ViewStatus::Running
                && row_tool_in_progress(state, row))
            .then_some(activity_frame);
            let live_elapsed = pulse
                .filter(|_| row.live_timer)
                .and_then(|_| state.transcript.entry(row.anchor.entry_id))
                .and_then(TranscriptEntry::running_elapsed);
            render_transcript_row(
                frame,
                area,
                row.clone(),
                selected_row,
                user_surface,
                palette,
                pulse,
                live_elapsed,
                turn_rail(state, row, prompts, palette),
            );
        }
    }
    if activity_height > 0 {
        let first = (conversation.y + painted_rows as u16 + u16::from(painted_rows > 0))
            .min(conversation.bottom().saturating_sub(1));
        for (offset, line) in (0_u16..).zip(status_lines) {
            let y = first + offset;
            if y >= conversation.bottom() {
                break;
            }
            frame.render_widget(
                Paragraph::new(line),
                Rect::new(content.x, y, content.width, 1),
            );
        }
    }
    // Scrolled back, the blank row above the composer offers the jump to the latest output.
    let jump_to_latest = (padding > 0 && !viewport.follows_tail())
        .then(|| {
            let row = Rect {
                y: outer.bottom().saturating_sub(1),
                height: 1,
                ..outer
            };
            render_jump_to_latest(frame, content_area(row), viewport, palette, bindings)
        })
        .flatten();
    (jump_to_latest, fold_rows)
}

/// Draw the centered jump-to-latest label in `row`, naming its key when it fits.
///
/// Returns:
///     Option<Rect>: The painted label, which the caller makes clickable; `None` when even the
///     short form does not fit.
fn render_jump_to_latest(
    frame: &mut Frame<'_>,
    row: Rect,
    viewport: &TranscriptViewport,
    palette: Palette,
    bindings: &Bindings,
) -> Option<Rect> {
    let what = if viewport.has_unseen_output() {
        "↓ new output"
    } else {
        "↓ latest"
    };
    let key = primary_label(bindings, KeyAction::Tail);
    let label = [
        (key != "Unbound").then(|| format!(" {what} · {key} ")),
        Some(format!(" {what} ")),
    ]
    .into_iter()
    .flatten()
    .find(|label| label.width() <= usize::from(row.width))?;
    let width = label.width() as u16;
    let area = Rect {
        x: row.x + (row.width - width) / 2,
        width,
        ..row
    };
    frame.render_widget(
        Paragraph::new(label).style(Style::default().fg(palette.primary).bg(palette.panel)),
        area,
    );
    Some(area)
}

fn active_reply(state: &UiState) -> Option<TranscriptEntryId> {
    if state.configuration_active()
        || state.view_status != ViewStatus::Running
        || state.interaction_status == crate::reducer::InteractionStatus::Compacting
    {
        return None;
    }
    state
        .transcript
        .entries()
        .iter()
        .rev()
        .take_while(|entry| entry.role != TranscriptRole::User)
        .find(|entry| entry.role == TranscriptRole::Assistant && !entry.content.is_empty())
        .map(|entry| entry.id)
}

fn tool_in_progress(entry: &TranscriptEntry) -> bool {
    entry
        .tool_card()
        .is_some_and(|card| matches!(card.status, ToolStatus::Requested | ToolStatus::Running))
        || entry.process_card().is_some_and(|card| {
            matches!(
                card.display_state,
                ProcessDisplayState::Running
                    | ProcessDisplayState::Polling
                    | ProcessDisplayState::Cancelling
            )
        })
}

fn row_tool_in_progress(state: &UiState, row: &TranscriptRow) -> bool {
    match row.kind {
        TranscriptRowKind::CardAction => state
            .transcript
            .entry(row.anchor.entry_id)
            .is_some_and(tool_in_progress),
        TranscriptRowKind::CardGroup => {
            // A repeated MCP call's row carries its own marker, which shows `?` while a call
            // awaits approval, as a single card would; the spinner must not replace it.
            let marked = row
                .spans
                .iter()
                .any(|span| span.style.inline == InlineStyle::ToolStatus);
            crate::transcript_view::card_run(&state.transcript, row.anchor.entry_id).is_some_and(
                |(_, members)| {
                    let members = members
                        .iter()
                        .filter_map(|id| state.transcript.entry(*id))
                        .collect::<Vec<_>>();
                    let awaiting = members.iter().any(|entry| {
                        entry
                            .tool_card()
                            .is_some_and(|card| card.status == ToolStatus::AwaitingApproval)
                    });
                    members.iter().copied().any(tool_in_progress) && !(marked && awaiting)
                },
            )
        }
        _ => false,
    }
}

/// One row above the composer while a run is going, so it never takes a row at idle.
fn status_line_height(state: &UiState) -> u16 {
    u16::from(run_in_status_line(state))
}

/// The run's status line: what is running and for how long, the session turn, and
/// the run's tool count and time on the left; queued messages and the context on the
/// right. The running item's text is clipped first, then the turn details drop.
fn render_status_line(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    activity_frame: u8,
    run_elapsed: Option<std::time::Duration>,
    palette: Palette,
) {
    let width = usize::from(area.width);
    let running = Style::default().fg(palette.secondary);
    let muted = Style::default().fg(palette.muted);
    let dim = muted.add_modifier(Modifier::DIM);

    let mut right = Vec::new();
    let queued = state
        .queued_steering()
        .saturating_add(state.queued_follow_ups());
    if queued > 0 {
        right.push(Span::styled(format!("{queued} queued"), running));
        right.push(Span::styled(" · ", dim));
    }
    let context = crate::context_view::indicator(state, 0);
    let pressured =
        crate::context_view::fill(state).is_some_and(|fill| fill >= CONTEXT_WARNING_FILL);
    right.push(Span::styled("ctx ", muted));
    right.push(Span::styled(
        context.strip_prefix("ctx ").unwrap_or(&context).to_owned(),
        if pressured {
            Style::default().fg(palette.warning)
        } else {
            muted
        },
    ));
    let right = Line::from(right);

    let activity = run_activity(state);
    let mut lead = vec![Span::styled(
        format!("{} ", spinner_frame(activity_frame)),
        running,
    )];
    let mut detail = String::new();
    let mut timer = None;
    match activity.item {
        Some(item) => {
            lead.push(Span::styled(
                format!(" {} ", item.label),
                palette.chip(&item.label),
            ));
            lead.push(Span::raw(" "));
            detail = item.detail;
            timer = item.elapsed.map(|elapsed| {
                crate::tool_cards::format_duration(
                    u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX),
                )
            });
        }
        None => lead.push(Span::styled(activity_phase(state), running)),
    }
    let turn_number = activity.turn;
    let tools = activity.tools;
    let mut totals = format!("{tools} tool{}", if tools == 1 { "" } else { "s" });
    if let Some(elapsed) = run_elapsed {
        totals.push_str(" · ");
        totals.push_str(&crate::tool_cards::format_duration(
            u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX),
        ));
    }
    let mut tail = vec![Span::styled(" · ", dim)];
    if turn_number > 0 {
        tail.push(Span::styled(format!("turn {turn_number} "), muted));
    }
    tail.push(Span::styled(totals, dim));

    // Fit the left side beside the right one, keeping at least a little of the
    // running item's text before the turn details are shown.
    const MIN_DETAIL_COLUMNS: usize = 8;
    let right_width = right.width();
    let mut budget = width.saturating_sub(right_width + 2);
    let lead_width = Line::from(lead.clone()).width();
    let show_right = lead_width <= budget;
    if !show_right {
        budget = width;
    }
    let timer_width = timer.as_ref().map_or(0, |timer| timer.width() + 1);
    let tail_width = Line::from(tail.clone()).width();
    let detail_floor = if detail.is_empty() {
        0
    } else {
        detail.width().min(MIN_DETAIL_COLUMNS)
    };
    // The queue and context outrank the turn details, which show only beside them.
    let show_tail = show_right && lead_width + detail_floor + timer_width + tail_width <= budget;
    let detail_room = budget
        .saturating_sub(lead_width + timer_width)
        .saturating_sub(if show_tail { tail_width } else { 0 });
    let mut left = lead;
    if !detail.is_empty() && detail_room > 0 {
        left.push(Span::styled(
            clip_columns(&detail, detail_room),
            Style::default().fg(palette.foreground),
        ));
    }
    if let Some(timer) = timer.filter(|_| lead_width + timer_width <= budget) {
        left.push(Span::raw(" "));
        left.push(Span::styled(timer, running));
    }
    if show_tail {
        left.extend(tail);
    }
    frame.render_widget(Paragraph::new(Line::from(left)), area);
    if show_right {
        let columns = u16::try_from(right_width)
            .unwrap_or(u16::MAX)
            .min(area.width);
        frame.render_widget(
            Paragraph::new(right),
            Rect {
                x: area.right() - columns,
                width: columns,
                ..area
            },
        );
    }
}

/// What the run in progress is doing, as the status line and the side panel show it.
pub(crate) struct RunActivity {
    /// The call or background process that names the run, if one is going.
    pub(crate) item: Option<RunningItem>,
    /// The session turn the run has reached, or 0 before the model answers.
    pub(crate) turn: u64,
    /// Calls and background processes the run has started.
    pub(crate) tools: usize,
}

pub(crate) fn run_activity(state: &UiState) -> RunActivity {
    let turn = current_turn(state);
    // Only calls after the latest reply name the run: once the model answers again, a
    // background process started earlier no longer says what is happening. Cancelling
    // outranks everything, since that is what the user is waiting on.
    let item = turn
        .iter()
        .rev()
        .take_while(|entry| !matches!(entry.kind, crate::transcript::TranscriptEntryKind::Message))
        .find_map(running_item)
        .filter(|_| !state.cancel_requested);
    RunActivity {
        item,
        turn: state.run_progress.map_or(0, |run| run.turn),
        tools: turn
            .iter()
            .filter(|entry| entry.tool_card().is_some() || entry.process_card().is_some())
            .count(),
    }
}

/// The entries of the run in progress: everything after the prompt that started it,
/// including steers and follow-ups it took in, or after the latest prompt otherwise.
pub(crate) fn current_turn(state: &UiState) -> &[TranscriptEntry] {
    let entries = state.transcript.entries();
    let start = run_prompt_index(state)
        .map_or(0, |index| index + 1)
        .min(entries.len());
    &entries[start..]
}

/// Where the prompt that started the run in progress sits, or the latest prompt.
fn run_prompt_index(state: &UiState) -> Option<usize> {
    state
        .run_progress
        .and_then(|run| state.transcript.entry_index(run.prompt))
        .or_else(|| {
            state
                .transcript
                .entries()
                .iter()
                .rposition(|entry| entry.role == TranscriptRole::User)
        })
}

/// A call still in progress, as the status line names it.
pub(crate) struct RunningItem {
    pub(crate) label: String,
    pub(crate) detail: String,
    pub(crate) elapsed: Option<std::time::Duration>,
}

fn running_item(entry: &TranscriptEntry) -> Option<RunningItem> {
    let (label, detail) = if let Some(card) = entry.tool_card() {
        if !matches!(card.status, ToolStatus::Requested | ToolStatus::Running) {
            return None;
        }
        (card.action_label(), card.display_arguments())
    } else {
        let card = entry.process_card()?;
        if !matches!(
            card.display_state,
            ProcessDisplayState::Running | ProcessDisplayState::Polling
        ) {
            return None;
        }
        (
            "Process".to_owned(),
            card.task_label
                .clone()
                .unwrap_or_else(|| "Background command".to_owned()),
        )
    };
    Some(RunningItem {
        label: sanitize_for_terminal(&label),
        detail: sanitize_for_terminal(&detail.split_whitespace().collect::<Vec<_>>().join(" ")),
        elapsed: entry.running_elapsed(),
    })
}

/// Why the last prompt failed, as up to three rows starting with `✗`, while nothing is
/// running. The backend's message already names the fix, such as `/connect` for
/// missing credentials, so it is shown as sent (sanitized and bounded).
fn failure_lines(state: &UiState, width: usize, palette: Palette) -> Vec<Line<'static>> {
    if state.current_command.is_some() || state.view_status != ViewStatus::Idle {
        return Vec::new();
    }
    let Some(error) = state.last_command_error.as_deref() else {
        return Vec::new();
    };
    wrapped_decision_rows(&format!("✗ Prompt failed: {error}"), width, 3)
        .into_iter()
        .map(|row| Line::styled(row, Style::default().fg(palette.error)))
        .collect()
}

/// What the run is doing when no call names it: compacting, cancelling, thinking, or
/// otherwise working.
pub(crate) fn activity_phase(state: &UiState) -> String {
    if state.interaction_status == crate::reducer::InteractionStatus::Compacting {
        return "Compacting context".into();
    }
    if state.cancel_requested {
        return "Cancelling".into();
    }
    let thinking = state
        .transcript
        .entries()
        .iter()
        .rev()
        .take_while(|entry| entry.role != TranscriptRole::User)
        .any(|entry| {
            entry.role == TranscriptRole::Assistant
                && entry.state == crate::transcript::TranscriptEntryState::Streaming
                && !entry.thinking().is_empty()
        });
    // A call or background process still running is named by the status line itself.
    if thinking {
        "Thinking".into()
    } else {
        "Working".into()
    }
}

fn tone_style(tone: TranscriptRowTone, palette: Palette) -> Style {
    match tone {
        TranscriptRowTone::Pending => Style::default().fg(palette.primary),
        TranscriptRowTone::Success => Style::default().fg(palette.success),
        TranscriptRowTone::Warning => Style::default().fg(palette.warning),
        TranscriptRowTone::Error => Style::default().fg(palette.error),
        TranscriptRowTone::DiffAddition => Style::default()
            .fg(palette.addition)
            .bg(palette.addition_background),
        TranscriptRowTone::DiffDeletion => Style::default()
            .fg(palette.deletion)
            .bg(palette.deletion_background),
        TranscriptRowTone::Surface => Style::default().fg(palette.foreground).bg(palette.surface),
        TranscriptRowTone::SurfaceNote => Style::default().fg(palette.muted).bg(palette.surface),
        _ => Style::default().fg(palette.muted),
    }
}

/// Where the transcript's first prompt and the running turn's prompt sit, found once
/// per frame.
#[derive(Clone, Copy)]
struct TurnBounds {
    first: Option<usize>,
    latest: Option<usize>,
}

/// The rail beside a transcript row. A prompt keeps its accent bar; every row of the
/// turn after it, replies included, shares one line that takes the running colour
/// while the turn runs and ends in `╰` at its summary. The gap under a prompt and the
/// gap after a finished turn stay bare.
fn turn_rail(
    state: &UiState,
    row: &TranscriptRow,
    prompts: TurnBounds,
    palette: Palette,
) -> Option<(&'static str, Color)> {
    if row.role == TranscriptRole::User {
        return Some(("▎", palette.primary));
    }
    let index = state.transcript.entry_index(row.anchor.entry_id)?;
    let entry = &state.transcript.entries()[index];
    if entry.role == TranscriptRole::User {
        return None;
    }
    if row.kind == TranscriptRowKind::RunSummary {
        return Some(("╰", palette.rail()));
    }
    // The gap that ends a turn, after its summary or before the next prompt, is bare.
    if matches!(
        row.kind,
        TranscriptRowKind::Spacer | TranscriptRowKind::Rule
    ) && (entry.run_summary().is_some()
        || state
            .transcript
            .entry_after(entry.id)
            .is_some_and(|next| next.role == TranscriptRole::User))
    {
        return None;
    }
    // Rows before the first prompt, such as a resumed session's notes, belong to no turn.
    if prompts.first.is_none_or(|first| index < first) {
        return None;
    }
    let running = state.view_status == ViewStatus::Running
        && prompts.latest.is_some_and(|prompt| index > prompt);
    Some((
        "│",
        if running {
            palette.running_rail()
        } else {
            palette.rail()
        },
    ))
}

#[allow(clippy::too_many_arguments)]
fn render_transcript_row(
    frame: &mut Frame<'_>,
    area: Rect,
    row: TranscriptRow,
    selected_row: Option<RowAnchor>,
    user_surface: bool,
    palette: Palette,
    pulse: Option<u8>,
    live_elapsed: Option<std::time::Duration>,
    rail: Option<(&'static str, Color)>,
) {
    // A selected row looks like any other; its fold hint is the only cue.
    let selected = selected_row == Some(row.anchor);
    let user = user_surface;
    let code = row
        .spans
        .iter()
        .any(|span| span.style.block == BlockStyle::Code);
    let base = if user {
        palette.user_text()
    } else {
        palette.base()
    };
    frame.render_widget(Block::default().style(base), area);
    let mut content = content_area(area);
    // Use the existing inset: decoration must not shift wrapping, copied text, or
    // mouse hit targets.
    if let Some((glyph, color)) = rail.filter(|_| content.x > area.x) {
        frame.render_widget(
            Paragraph::new(Span::styled(glyph, base.fg(color))),
            Rect::new(area.x, area.y, 1, 1),
        );
    }
    let gutter = match row.gutter {
        RowGutter::None => None,
        RowGutter::Connector => Some(RESULT_CONNECTOR),
        RowGutter::Indent => Some(RESULT_INDENT),
    };
    // The gutter sits beside a code or diff row's fill rather than inside it.
    if let Some(gutter) = gutter {
        let columns = u16::try_from(gutter.width())
            .unwrap_or(u16::MAX)
            .min(content.width);
        frame.render_widget(
            Paragraph::new(Span::styled(gutter, base.fg(palette.muted))),
            Rect::new(content.x, content.y, columns, 1),
        );
        content.x += columns;
        content.width -= columns;
    }
    if row.kind == TranscriptRowKind::Rule {
        // Preserve the existing viewport anchors, but separate routine tools with air.
        return;
    }
    if code {
        frame.render_widget(Block::default().style(palette.composer()), content);
    } else if matches!(
        row.tone,
        TranscriptRowTone::DiffAddition
            | TranscriptRowTone::DiffDeletion
            | TranscriptRowTone::Surface
            | TranscriptRowTone::SurfaceNote
    ) {
        frame.render_widget(
            Block::default().style(base.patch(tone_style(row.tone, palette))),
            content,
        );
    }
    let style = match row.kind {
        TranscriptRowKind::Header
            if matches!(row.role, TranscriptRole::User | TranscriptRole::Assistant) =>
        {
            base.patch(palette.speaker_label())
        }
        TranscriptRowKind::CardAction
        | TranscriptRowKind::CardGroup
        | TranscriptRowKind::Evidence
        | TranscriptRowKind::Thought => base.fg(palette.muted),
        _ if user => base,
        _ if matches!(
            row.tone,
            TranscriptRowTone::Pending
                | TranscriptRowTone::Success
                | TranscriptRowTone::Warning
                | TranscriptRowTone::Error
                | TranscriptRowTone::DiffAddition
                | TranscriptRowTone::DiffDeletion
                | TranscriptRowTone::Surface
                | TranscriptRowTone::SurfaceNote
        ) =>
        {
            base.patch(tone_style(row.tone, palette))
        }
        _ if row.tone == TranscriptRowTone::Muted => base.fg(palette.muted),
        _ => base,
    };
    // While the run is active, an in-progress marker animates as a spinner. The
    // shape changes rather than the brightness, so it reads the same in monochrome.
    let spinner = pulse.map(|frame| format!("{}  ", spinner_frame(frame)));
    let label = matches!(
        row.kind,
        TranscriptRowKind::CardAction | TranscriptRowKind::CardGroup | TranscriptRowKind::Evidence
    )
    .then(|| {
        row.spans
            .iter()
            .position(|span| span.style.inline == InlineStyle::ToolName)
    })
    .flatten();
    let mut spans = row
        .spans
        .into_iter()
        .map(|span| {
            let mut semantic = markdown_span_style(style, span.style, palette);
            if span.style.inline == InlineStyle::ToolStatus {
                semantic = semantic.patch(tone_style(row.tone, palette));
            }
            match &spinner {
                Some(spinner) if span.style.inline == InlineStyle::ToolStatus => {
                    Span::styled(spinner.clone(), semantic)
                }
                _ => Span::styled(span.text, semantic),
            }
        })
        .collect::<Vec<_>>();
    if let Some(label) = label {
        let chip = spans[label]
            .style
            .patch(palette.chip(spans[label].content.trim()));
        fill_label_chip(&mut spans, label, chip, palette);
    }
    if let Some(hint) = row.fold_hint.text().filter(|_| selected) {
        spans.push(Span::styled(hint, base.fg(palette.muted)));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)), content);
    // The arrow takes the blank middle of the label gap. A row cut short can put
    // its `…` there instead, which stays.
    if let Some(arrow) = row.fold_arrow.filter(|arrow| arrow.column < content.width) {
        let position = (content.x + arrow.column, content.y);
        if frame.buffer_mut()[position].symbol() == " " {
            frame.buffer_mut()[position]
                .set_symbol(arrow.symbol())
                .set_fg(palette.muted);
        }
    }
    // A finished call's time is muted; a running call's ticks in the running colour.
    let live = live_elapsed.map(|elapsed| {
        crate::tool_cards::format_duration(u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX))
    });
    let trailing = match (row.trailing.as_deref(), live.as_deref()) {
        (Some(trailing), _) => Some((trailing, palette.muted)),
        (None, Some(live)) => Some((live, palette.secondary)),
        (None, None) => None,
    };
    if let Some((text, color)) = trailing {
        let columns = u16::try_from(text.width())
            .unwrap_or(u16::MAX)
            .min(content.width);
        frame.render_widget(
            Paragraph::new(Span::styled(text.to_owned(), base.fg(color))),
            Rect::new(content.right() - columns, content.y, columns, 1),
        );
    }
}

/// Fill a card's label as a chip one space wider than the name on each side. The padding
/// borrows the space that ends the status marker and the first space after the name, which
/// is either the label's own alignment padding or the gap after it, so the row's text, width
/// and copied selection stay the same. The rest of the alignment padding stays plain, so
/// stacked chips differ in width instead of forming one block.
fn fill_label_chip(spans: &mut Vec<Span<'static>>, label: usize, chip: Style, palette: Palette) {
    let content = spans[label].content.to_string();
    let name = content.trim_end_matches(' ');
    let mut text = name.to_owned();
    let mut padding = content[name.len()..].to_owned();
    let plain = spans[label].style;
    if let Some(before) = label
        .checked_sub(1)
        .map(|index| &mut spans[index])
        .filter(|before| before.content.ends_with(' '))
    {
        before.content.to_mut().pop();
        text.insert(0, ' ');
    }
    if !padding.is_empty() {
        padding.remove(0);
        text.push(' ');
    } else if let Some(after) = spans
        .get_mut(label + 1)
        .filter(|after| after.content.starts_with(' '))
    {
        after.content.to_mut().remove(0);
        text.push(' ');
    }
    let mut face = text.as_str();
    let mut decorated = Vec::new();
    if let Some(rest) = face.strip_prefix(' ') {
        decorated.push(Span::styled(" ", palette.chip_edge(chip, true)));
        face = rest;
    }
    let trailing = face.ends_with(' ');
    decorated.push(Span::styled(face.trim_end_matches(' ').to_owned(), chip));
    if trailing {
        decorated.push(Span::styled(" ", palette.chip_edge(chip, false)));
    }
    if !padding.is_empty() {
        decorated.push(Span::styled(padding, plain));
    }
    spans.splice(label..=label, decorated);
}

/// Braille spinner frames, one per 125 ms activity tick.
/// A turning half circle marks work in progress, by shape rather than brightness.
pub(crate) const SPINNER_FRAMES: [&str; 4] = ["◐", "◓", "◑", "◒"];

pub(crate) fn spinner_frame(frame: u8) -> &'static str {
    SPINNER_FRAMES[usize::from(frame) % SPINNER_FRAMES.len()]
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
                .title(if crate::diff_review::is_diff(presentation) {
                    " review · this tool edit · before ↔ after "
                } else {
                    " live retained detail "
                })
                .border_style(palette.border())
                .borders(Borders::ALL),
        ),
        chunks[0],
    );

    let width = usize::from(chunks[1].width.saturating_sub(2)).max(1);
    let height = usize::from(chunks[1].height.saturating_sub(2)).max(1);
    let lines = if crate::diff_review::is_diff(presentation) {
        view.review.lines(presentation, width, height, palette)
    } else {
        view.set_geometry(presentation, width, height);
        let rows = view.visible_rows(presentation);
        if rows.is_empty() {
            vec![Line::styled(
                "(no retained detail rows)",
                Style::default().fg(palette.muted),
            )]
        } else {
            rows.into_iter()
                .map(|row| detail_line(row, palette))
                .collect()
        }
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
        Paragraph::new(if crate::diff_review::is_diff(presentation) {
            "↑/↓ scroll · [/] hunk · Tab edit · s split · c context · Esc close"
        } else {
            "↑/↓ scroll · PgUp/PgDn · Home/End · Esc close"
        })
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
    let base = semantic
        .tone
        .map_or(base, |tone| base.patch(tone_style(tone, palette)));
    let mut style = match semantic.block {
        BlockStyle::Normal => base,
        BlockStyle::Heading(level) => base.patch(palette.semantic_heading(level)),
        BlockStyle::Code => base.fg(palette.foreground).bg(palette.surface),
        BlockStyle::RawHtml => base.fg(palette.muted),
    };
    style = match semantic.inline {
        InlineStyle::Normal => style,
        InlineStyle::Code => style.patch(palette.semantic_inline_code()),
        InlineStyle::Link => style.fg(palette.link()).add_modifier(Modifier::UNDERLINED),
        InlineStyle::QuoteMarker => style.fg(palette.muted),
        InlineStyle::ListMarker => style.patch(palette.semantic_list_marker()),
        InlineStyle::TableBorder => style.fg(palette.muted),
        InlineStyle::ToolName => style.patch(palette.tool_name()),
        InlineStyle::ToolCommand => style.patch(palette.tool_command()),
        InlineStyle::ToolPath => style.patch(palette.tool_path()),
        InlineStyle::ToolArgument => style.patch(palette.tool_argument()),
        InlineStyle::ToolStatus => style,
        // Modifiers, not colour, so the changed words stay visible in monochrome.
        InlineStyle::DiffChange => palette.diff_change(style),
        InlineStyle::DiffGutter => style.fg(palette.muted),
        InlineStyle::DiffSign => style.fg(if style.bg == Some(palette.deletion_background) {
            palette.error
        } else if style.bg == Some(palette.addition_background) {
            palette.success
        } else {
            palette.muted
        }),
    };
    style = palette.syntax(style, semantic.syntax);
    if semantic.syntax == SyntaxClass::Comment {
        style = style.add_modifier(Modifier::ITALIC);
    }
    if semantic.syntax == SyntaxClass::Keyword {
        style = style.add_modifier(Modifier::BOLD);
    }
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

/// Whether the composer draws its frame, whose bottom border then names the mode
/// and model in place of the footer.
fn composer_framed(state: &UiState, padded: bool, area: Rect) -> bool {
    editable(state) && padded && area.height >= 4
}

/// The framed composer's last row: mode first so narrow widths cut the model
/// and effort, never the mode.
fn composer_identity_line(state: &UiState, palette: Palette) -> Line<'static> {
    let separator = || Span::styled(" · ", Style::default().fg(palette.muted));
    let mut spans = Vec::new();
    if state.mode_confirmed {
        let mut letters = state.mode.as_str().chars();
        let label = letters
            .next()
            .map(|first| first.to_uppercase().chain(letters).collect::<String>())
            .unwrap_or_default();
        spans.push(Span::styled(
            label,
            Style::default()
                .fg(palette.primary)
                .add_modifier(Modifier::BOLD),
        ));
    }
    if let Some(provider) = state.provider.as_deref() {
        if !spans.is_empty() {
            spans.push(separator());
        }
        if let Some(model) = state
            .model_catalog
            .as_ref()
            .and_then(|catalog| catalog.selection.effective_model.as_deref())
            .or(state.model.as_deref())
        {
            spans.push(Span::styled(
                sanitize_for_terminal(model),
                Style::default().fg(palette.foreground),
            ));
            spans.push(Span::raw(" "));
        }
        spans.push(Span::styled(
            sanitize_for_terminal(provider),
            Style::default().fg(palette.muted),
        ));
        if let Some(effort) = state.effort.as_deref() {
            spans.push(separator());
            spans.push(Span::styled(
                sanitize_for_terminal(effort),
                Style::default()
                    .fg(palette.warning)
                    .add_modifier(Modifier::BOLD),
            ));
        }
        if state.model_selection_stale {
            spans.push(Span::styled(
                " (last confirmed; selection unavailable)",
                Style::default().fg(palette.muted),
            ));
        }
    }
    Line::from(spans)
}

/// The framed composer's last row: its identity on the left and, during a run, the
/// keys that follow up or steer on the right when both fit.
fn render_composer_identity_row(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    bindings: &Bindings,
    palette: Palette,
) {
    let identity = composer_identity_line(state, palette);
    frame.render_widget(Paragraph::new(identity.clone()), area);
    if !state.active_prompt_editable() {
        return;
    }
    let keys = keycap_hints(
        &format!(
            "{} follow-up · {} steer",
            primary_label(bindings, KeyAction::Submit),
            primary_label(bindings, KeyAction::AlternateSubmit)
        ),
        palette,
    )
    .style(Style::default().fg(palette.muted));
    let keys_width = u16::try_from(keys.width()).unwrap_or(u16::MAX);
    let identity_width = u16::try_from(identity.width()).unwrap_or(u16::MAX);
    if identity_width.saturating_add(2).saturating_add(keys_width) <= area.width {
        frame.render_widget(
            Paragraph::new(keys),
            Rect {
                x: area.right() - keys_width,
                width: keys_width,
                ..area
            },
        );
    }
}

/// Colour the composer's whole left side, corners included, as its accent. It keeps
/// the border's own glyphs and weight, so the corners join in any terminal font.
fn paint_composer_accent(frame: &mut Frame<'_>, area: Rect, color: Color) {
    let buffer = frame.buffer_mut();
    for y in area.y..area.bottom() {
        if let Some(cell) = buffer.cell_mut((area.x, y)) {
            cell.set_fg(color);
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn render_composer(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    layout: Option<&ComposerLayout>,
    focused: bool,
    padded: bool,
    gap: bool,
    palette: Palette,
    bindings: &Bindings,
) -> Option<mouse::Editor> {
    // The border stays quiet while idle or running and takes the waiting or error
    // colour; the accent bar carries the run's state.
    let status = status_color(state, palette);
    let alerting = status == palette.warning || status == palette.error;
    let border_style = Style::default().fg(if alerting { status } else { palette.muted });
    let framed_input = composer_framed(state, padded, area);
    let block = if framed_input {
        Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .border_style(border_style)
    } else {
        Block::default().padding(Padding::horizontal(CONTENT_PADDING))
    }
    .style(palette.base());
    let mut inner = block.inner(area);
    if editable(state) {
        frame.render_widget(block, area);
        if framed_input {
            // Waiting and error colours win; a run shows the running colour, and an
            // idle composer keeps the accent.
            let accent = if alerting {
                status
            } else if state.active_prompt_editable() {
                palette.secondary
            } else {
                palette.accent
            };
            paint_composer_accent(frame, area, accent);
            // On a tall screen a blank row separates the draft from the identity,
            // when the composer has room for both and one draft row.
            let gap = u16::from(gap && inner.height >= 3);
            inner.height = inner.height.saturating_sub(1 + gap);
            // Indented past the prompt prefix so it lines up with the draft.
            let prefix_width = u16::try_from(COMPOSER_PREFIX.width()).unwrap_or(2);
            render_composer_identity_row(
                frame,
                Rect {
                    x: inner.x.saturating_add(prefix_width).min(inner.right()),
                    y: inner.bottom() + gap,
                    width: inner.width.saturating_sub(prefix_width),
                    height: 1,
                },
                state,
                bindings,
                palette,
            );
        }
        let prefix_width = u16::try_from(COMPOSER_PREFIX.width()).unwrap_or(2);
        let prefix_area = Rect {
            x: inner.x,
            y: inner.y,
            width: prefix_width.min(inner.width),
            height: 1,
        };
        if prefix_area.width > 0 && prefix_area.y < inner.bottom() {
            frame.render_widget(
                Paragraph::new(COMPOSER_PREFIX).style(Style::default().fg(palette.primary)),
                prefix_area,
            );
        }
        let editor_area = Rect {
            x: inner.x.saturating_add(prefix_width).min(inner.right()),
            y: inner.y,
            width: inner.width.saturating_sub(prefix_width),
            height: inner.height,
        };
        let layout = layout.expect("editable composer requires a visual layout");
        let vertical_scroll = layout
            .cursor_row
            .saturating_sub(usize::from(editor_area.height.saturating_sub(1)));
        let visible_rows = layout
            .rows
            .iter()
            .skip(vertical_scroll)
            .take(usize::from(editor_area.height))
            .collect::<Vec<_>>();
        if layout.is_empty {
            let placeholder = if state.active_prompt_editable() {
                let hint = "Message to steer or follow up…";
                if hint.width() <= usize::from(editor_area.width) {
                    hint
                } else {
                    "Steer or follow up…"
                }
            } else {
                "Ask Wisp anything…"
            };
            frame.render_widget(
                Paragraph::new(placeholder).style(Style::default().fg(palette.muted)),
                editor_area,
            );
        } else {
            let context = prompt_highlighting::Context::new(
                state.command_catalog.as_deref(),
                state.project_files.snapshot(),
            );
            let mut highlighted_line = None;
            let mut highlights = Vec::new();
            let rows = visible_rows
                .iter()
                .map(|row| {
                    if highlighted_line != Some(row.logical_row) {
                        let line = &layout.lines[row.logical_row];
                        highlights = prompt_highlighting::merge_highlights(
                            line.markdown_highlights.clone(),
                            prompt_highlighting::line_highlights(
                                &line.text,
                                line.truncated,
                                row.logical_row,
                                layout.raw_line_count,
                                &context,
                            ),
                            prompt_highlighting::MAX_HIGHLIGHTS_PER_LINE,
                        );
                        highlighted_line = Some(row.logical_row);
                    }
                    styled_composer_line(
                        row,
                        &highlights,
                        layout.lines[row.logical_row].selection.as_ref(),
                        usize::from(editor_area.width),
                        palette,
                    )
                })
                .collect::<Vec<_>>();
            frame.render_widget(Paragraph::new(rows), editor_area);
        }
        let cursor_x = editor_area
            .x
            .saturating_add(u16::try_from(layout.cursor_column).unwrap_or(u16::MAX));
        let cursor_y = editor_area.y.saturating_add(
            u16::try_from(layout.cursor_row.saturating_sub(vertical_scroll)).unwrap_or(u16::MAX),
        );
        if focused && cursor_x < editor_area.right() && cursor_y < editor_area.bottom() {
            frame.set_cursor_position((cursor_x, cursor_y));
        }
        return Some(mouse::Editor {
            area: editor_area,
            revision: layout.revision,
            rows: visible_rows
                .into_iter()
                .map(|row| mouse::EditorRow {
                    logical_row: row.logical_row,
                    column_start: row.column_start,
                    column_end: row.column_start.saturating_add(row.text.width()),
                })
                .collect(),
        });
    }

    let message = if let Some(operation) = state.session_operation.as_ref() {
        operation.label().into()
    } else {
        match state.view_status {
            ViewStatus::Running if state.cancel_requested => "Cancelling current prompt…".into(),
            ViewStatus::Running => {
                "Prompt in progress. Esc/Ctrl-C cancels; steering arrives in #466.".into()
            }
            ViewStatus::Error => "Wisp's backend stopped. Ctrl-C exits.".into(),
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

#[derive(Clone, Debug, Eq, PartialEq)]
struct ComposerVisualRow {
    logical_row: usize,
    column_start: usize,
    text: String,
}

struct ComposerLogicalLine {
    text: String,
    truncated: bool,
    selection: Option<std::ops::Range<usize>>,
    markdown_highlights: Vec<Highlight>,
}

struct ComposerLayout {
    revision: u64,
    is_empty: bool,
    raw_line_count: usize,
    lines: Vec<ComposerLogicalLine>,
    rows: Vec<ComposerVisualRow>,
    cursor_row: usize,
    cursor_column: usize,
}

/// Reuse the expensive Unicode-aware draft layout until its inputs change.
#[derive(Default)]
pub(crate) struct ComposerLayoutCache {
    revision: Option<u64>,
    width: usize,
    layout: Option<ComposerLayout>,
    #[cfg(test)]
    rebuilds: usize,
}

impl ComposerLayoutCache {
    fn layout(&mut self, editor: &PromptEditor, width: usize) -> &ComposerLayout {
        let width = width.max(1);
        if self.revision != Some(editor.revision()) || self.width != width {
            self.layout = Some(composer_layout(editor, width));
            self.revision = Some(editor.revision());
            self.width = width;
            #[cfg(test)]
            {
                self.rebuilds += 1;
            }
        }
        self.layout.as_ref().expect("composer layout was cached")
    }

    #[cfg(test)]
    fn rebuilds(&self) -> usize {
        self.rebuilds
    }
}

fn composer_editor_width(area_width: u16) -> usize {
    usize::from(area_width.saturating_sub(2))
        .saturating_sub(COMPOSER_PREFIX.width())
        .max(1)
}

#[cfg(test)]
fn composer_visual_row_count(projection: &PromptProjection<'_>, width: usize) -> usize {
    let width = width.max(1);
    projection
        .text()
        .split('\n')
        .map(|line| visual_row_count(line, width))
        .sum::<usize>()
        .saturating_add(usize::from(cursor_needs_continuation_row(
            projection, width,
        )))
}

#[cfg(test)]
fn visual_row_count(line: &str, width: usize) -> usize {
    visual_row_metrics(line, width).0
}

fn visual_row_metrics(line: &str, width: usize) -> (usize, usize, usize) {
    let mut rows = Vec::new();
    append_composer_visual_rows(&mut rows, 0, line, width);
    let final_width = rows.last().map_or(0, |row| row.text.width());
    (
        rows.len(),
        final_width,
        crate::prompt_editor::display_width(line),
    )
}

fn cursor_needs_continuation_row(projection: &PromptProjection<'_>, width: usize) -> bool {
    let Some(line) = projection.text().split('\n').nth(projection.cursor_row()) else {
        return false;
    };
    let (_, final_row_width, line_width) = visual_row_metrics(line, width);
    projection.cursor_column() == line_width && final_row_width == width
}

fn composer_layout(editor: &PromptEditor, width: usize) -> ComposerLayout {
    let width = width.max(1);
    let projection = editor.projection();
    let selection = projection.selection();
    let mut line_start = 0;
    let mut lines = projection
        .text()
        .split('\n')
        .map(|line| {
            let mut end = line.len().min(prompt_highlighting::MAX_LINE_BYTES);
            while !line.is_char_boundary(end) {
                end -= 1;
            }
            let line_end = line_start + line.len();
            let selected_columns = selection.as_ref().and_then(|range| {
                let start = range.start.max(line_start);
                let end = range.end.min(line_end);
                let newline = range.start <= line_end && range.end > line_end;
                (start < end || newline).then(|| {
                    crate::prompt_editor::display_width(
                        &line[..start.saturating_sub(line_start).min(line.len())],
                    )
                        ..crate::prompt_editor::display_width(
                            &line[..end.saturating_sub(line_start)],
                        ) + usize::from(newline)
                })
            });
            line_start = line_end + 1;
            ComposerLogicalLine {
                text: line[..end].to_owned(),
                truncated: end < line.len(),
                selection: selected_columns,
                markdown_highlights: Vec::new(),
            }
        })
        .collect::<Vec<_>>();
    let document_lines = lines
        .iter()
        .map(|line| prompt_highlighting::DocumentLine {
            text: &line.text,
            truncated: line.truncated,
        })
        .collect::<Vec<_>>();
    let markdown_highlights = prompt_highlighting::document_highlights(&document_lines, None);
    for (line, highlights) in lines.iter_mut().zip(markdown_highlights) {
        line.markdown_highlights = highlights;
    }
    let mut rows = Vec::new();
    let mut cursor_row = 0_usize;
    let mut cursor_column = 0_usize;
    for (logical_row, line) in projection.text().split('\n').enumerate() {
        let first_row = rows.len();
        append_composer_visual_rows(&mut rows, logical_row, line, width);
        let line_columns = crate::prompt_editor::display_width(line);
        let selected_newline_wraps = lines[logical_row].selection.as_ref().is_some_and(|range| {
            range.end > line_columns && rows.last().is_some_and(|row| row.text.width() == width)
        });
        if selected_newline_wraps
            || (logical_row == projection.cursor_row()
                && cursor_needs_continuation_row(&projection, width))
        {
            rows.push(ComposerVisualRow {
                logical_row,
                column_start: line_columns,
                text: String::new(),
            });
        }
        if logical_row != projection.cursor_row() {
            continue;
        }
        let relative = rows[first_row..]
            .iter()
            .rposition(|row| row.column_start <= projection.cursor_column())
            .unwrap_or(0);
        cursor_row = first_row + relative;
        cursor_column = projection
            .cursor_column()
            .saturating_sub(rows[cursor_row].column_start);
    }
    ComposerLayout {
        revision: editor.revision(),
        is_empty: editor.text().is_empty(),
        raw_line_count: editor.line_count(),
        lines,
        rows,
        cursor_row,
        cursor_column,
    }
}

fn styled_composer_line(
    row: &ComposerVisualRow,
    highlights: &[Highlight],
    selection: Option<&std::ops::Range<usize>>,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let row_start = row.column_start;
    let row_end = row_start.saturating_add(row.text.width());
    let mut spans = Vec::new();
    let mut byte_start = 0;
    for highlight in highlights {
        let start = highlight.columns.start.max(row_start).min(row_end);
        let end = highlight.columns.end.max(start).min(row_end);
        if start == end {
            continue;
        }
        let start_byte = byte_at_local_display_column(&row.text, start.saturating_sub(row_start));
        let end_byte = byte_at_local_display_column(&row.text, end.saturating_sub(row_start));
        if byte_start < start_byte {
            spans.push(Span::raw(row.text[byte_start..start_byte].to_owned()));
        }
        let style = match highlight.kind {
            PromptHighlightKind::Command => palette.semantic_command(),
            PromptHighlightKind::ResolvedPath => palette.semantic_path(true),
            PromptHighlightKind::UnresolvedPath => palette.semantic_path(false),
            PromptHighlightKind::MarkdownHeading => palette.semantic_heading(1),
            PromptHighlightKind::MarkdownListMarker => palette.semantic_list_marker(),
            PromptHighlightKind::MarkdownInlineCodeDelimiter => palette.semantic_code_delimiter(),
            PromptHighlightKind::MarkdownInlineCode => palette.semantic_inline_code(),
            PromptHighlightKind::MarkdownFenceDelimiter => palette.semantic_fence_delimiter(),
            PromptHighlightKind::MarkdownFenceInfo => palette.semantic_fence_info(),
            PromptHighlightKind::MarkdownFenceBody => palette.semantic_fence_body(),
        };
        spans.push(Span::styled(
            row.text[start_byte..end_byte].to_owned(),
            style,
        ));
        byte_start = end_byte;
    }
    if byte_start < row.text.len() {
        spans.push(Span::raw(row.text[byte_start..].to_owned()));
    }
    let Some(selection) = selection else {
        return Line::from(spans);
    };
    let mut selected_spans: Vec<Span<'static>> = Vec::new();
    let mut column = row_start;
    for span in spans {
        for grapheme in span.content.graphemes(true) {
            let end = column + grapheme.width();
            let selected = column < selection.end && end > selection.start;
            let style = if selected {
                span.style.patch(palette.selection())
            } else {
                span.style
            };
            if let Some(previous) = selected_spans
                .last_mut()
                .filter(|previous| previous.style == style)
            {
                previous.content.to_mut().push_str(grapheme);
            } else {
                selected_spans.push(Span::styled(grapheme.to_owned(), style));
            }
            column = end;
        }
    }
    // A selected newline needs a visible cell even on an otherwise empty line.
    if column >= selection.start && column < selection.end && column - row_start < width {
        selected_spans.push(Span::styled(" ", palette.selection()));
    }
    Line::from(selected_spans)
}

fn byte_at_local_display_column(text: &str, target: usize) -> usize {
    let mut column = 0_usize;
    for (offset, grapheme) in text.grapheme_indices(true) {
        let next = column.saturating_add(grapheme.width());
        if target < next {
            return offset;
        }
        column = next;
    }
    text.len()
}

fn append_composer_visual_rows(
    rows: &mut Vec<ComposerVisualRow>,
    logical_row: usize,
    line: &str,
    width: usize,
) {
    fn push_row(
        rows: &mut Vec<ComposerVisualRow>,
        logical_row: usize,
        column_start: usize,
        text: String,
    ) {
        rows.push(ComposerVisualRow {
            logical_row,
            column_start,
            text,
        });
    }

    let width = width.max(1);
    let mut column_start = 0_usize;
    let mut source_column = 0_usize;
    let mut used = 0_usize;
    let mut text = String::new();
    // Keep whitespace on the preceding visual row so every wrapped row retains
    // exact source display columns for cursor movement, selection, and mouse hits.
    // The tuple is the rendered byte and display-column boundary after that space.
    let mut word_break = None::<(usize, usize)>;
    let mut has_word = false;

    {
        let mut append_piece = |rendered: &str, piece_width: usize, whitespace: bool| {
            while used > 0 && used.saturating_add(piece_width) > width {
                if let Some((byte, columns)) = word_break.filter(|(byte, _)| *byte > 0) {
                    let remainder = text.split_off(byte);
                    push_row(rows, logical_row, column_start, std::mem::take(&mut text));
                    text = remainder;
                    column_start = column_start.saturating_add(columns);
                    used = used.saturating_sub(columns);
                    has_word = !text.is_empty();
                    word_break = None;
                } else {
                    push_row(rows, logical_row, column_start, std::mem::take(&mut text));
                    column_start = column_start.saturating_add(used);
                    used = 0;
                    has_word = false;
                    word_break = None;
                }
            }

            text.push_str(rendered);
            used = used.saturating_add(piece_width);
            if whitespace {
                if has_word {
                    word_break = Some((text.len(), used));
                }
            } else {
                has_word = true;
            }
        };
        for grapheme in line.graphemes(true) {
            let grapheme_width = source_grapheme_display_width(grapheme, source_column);
            if grapheme == "\t" {
                for _ in 0..grapheme_width {
                    append_piece(" ", 1, true);
                }
            } else {
                append_piece(
                    grapheme,
                    grapheme_width,
                    grapheme.chars().all(char::is_whitespace),
                );
            }
            source_column = source_column.saturating_add(grapheme_width);
        }
    }
    push_row(rows, logical_row, column_start, text);
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
        if candidate.width() > width {
            break;
        }
        out = candidate;
    }
    out
}

pub(crate) fn primary_label(bindings: &Bindings, action: KeyAction) -> String {
    let label = bindings.label(action);
    label
        .split(" / ")
        .next()
        .filter(|part| !part.is_empty())
        .unwrap_or("Unbound")
        .to_string()
}

fn centered_welcome_line(line: Line<'static>, width: usize) -> Line<'static> {
    let padding = width.saturating_sub(line.width()) / 2;
    let style = line.style;
    let mut spans = vec![Span::raw(" ".repeat(padding))];
    spans.extend(line.spans);
    Line::from(spans).style(style)
}

fn welcome_action_line(
    label: &'static str,
    shortcut: &str,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let block_width = width.min(44);
    let content_width = label.width().saturating_add(shortcut.width());
    let gap = block_width.saturating_sub(content_width).max(2);
    let line = Line::from(vec![
        Span::styled(label, Style::default().fg(palette.foreground)),
        Span::raw(" ".repeat(gap)),
        Span::styled(shortcut.to_owned(), Style::default().fg(palette.muted)),
    ]);
    centered_welcome_line(line, width)
}

fn center_welcome_panel(mut panel: Vec<Line<'static>>, height: usize) -> Vec<Line<'static>> {
    let top_padding = height.saturating_sub(panel.len()) / 2;
    let mut lines = vec![Line::default(); top_padding];
    lines.append(&mut panel);
    lines
}

fn empty_transcript_lines(
    state: &UiState,
    connection: &ConnectionInfo,
    palette: Palette,
    height: usize,
    width: usize,
    bindings: &Bindings,
    startup_logo: LogoChoice,
) -> Vec<Line<'static>> {
    let muted = Style::default().fg(palette.muted);
    if state.context.loading() {
        let mut lines = vec![Line::styled(
            "Refreshing context… You can keep editing your draft.",
            muted,
        )];
        if height >= 3 {
            if let Some(identity) = header_identity(state) {
                lines.push(Line::default());
                lines.push(Line::styled(sanitize_for_terminal(&identity), muted));
            }
        }
        return lines;
    }
    if !editable(state) {
        return Vec::new();
    }

    let version = sanitize_for_terminal(&connection.backend_version);
    let version_line = || {
        centered_welcome_line(
            Line::from(vec![
                Span::styled(
                    "Wisp ",
                    Style::default()
                        .fg(palette.primary)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(version.clone(), muted),
            ]),
            width,
        )
    };

    if width >= EMPTY_TRANSCRIPT_PANEL_WIDTH {
        let submit = primary_label(bindings, KeyAction::Submit);
        let mut actions = vec![("Ask Wisp anything", submit.as_str())];
        if state.provider.is_none() {
            actions.push(("Connect a provider", "/connect"));
        }
        actions.extend([
            ("Resume a session", "/resume"),
            ("Browse commands", "/"),
            ("Mention a project file", "@"),
        ]);
        let copy_height = 4;
        let action_height = actions.len() + 1;
        // The separator after the logo can be omitted at the exact-fit boundary.
        let action_logo_height = height
            .saturating_add(1)
            .saturating_sub(copy_height + action_height);
        let mut panel = startup_logo::preview_lines(
            startup_logo,
            u16::try_from(width).unwrap_or(u16::MAX),
            u16::try_from(action_logo_height).unwrap_or(u16::MAX),
            palette,
            palette.is_monochrome(),
        );
        if panel.is_empty() {
            panel = startup_logo::preview_lines(
                startup_logo,
                u16::try_from(width).unwrap_or(u16::MAX),
                u16::try_from(height.saturating_sub(copy_height)).unwrap_or(u16::MAX),
                palette,
                palette.is_monochrome(),
            );
        }
        let mark_height = panel.len();
        panel.push(Line::default());
        panel.push(version_line());
        panel.push(centered_welcome_line(
            Line::styled(
                EMPTY_TRANSCRIPT_TAGLINE,
                Style::default().fg(palette.foreground),
            ),
            width,
        ));
        panel.push(centered_welcome_line(
            Line::styled(
                if state.provider.is_none() {
                    "/connect to add a provider"
                } else {
                    EMPTY_TRANSCRIPT_HINT
                },
                muted,
            ),
            width,
        ));

        let mut full_height = panel.len().saturating_add(1).saturating_add(actions.len());
        if height + 1 == full_height {
            // Give the new composer padding its row without hiding connection guidance.
            panel.remove(mark_height);
            full_height -= 1;
        }
        if height >= full_height {
            panel.push(Line::default());
            panel.extend(
                actions
                    .into_iter()
                    .map(|(label, shortcut)| welcome_action_line(label, shortcut, width, palette)),
            );
            return center_welcome_panel(panel, height);
        }
        if height >= panel.len() {
            return center_welcome_panel(panel, height);
        }
    }

    let mut panel = vec![version_line()];
    if height >= 3 && EMPTY_TRANSCRIPT_TAGLINE.width() <= width {
        panel.push(centered_welcome_line(
            Line::styled(
                EMPTY_TRANSCRIPT_TAGLINE,
                Style::default().fg(palette.foreground),
            ),
            width,
        ));
    }
    if height >= 2 {
        let hint = if state.provider.is_none() {
            "/connect to add a provider"
        } else if EMPTY_TRANSCRIPT_HINT.width() <= width {
            EMPTY_TRANSCRIPT_HINT
        } else {
            "Type a prompt or /"
        };
        panel.push(centered_welcome_line(Line::styled(hint, muted), width));
    }
    center_welcome_panel(panel, height)
}

/// The browse hint, shown once in the footer instead of under every card. It needs a
/// transcript to browse; checking for any entry keeps this O(1) per frame.
fn browse_hint(state: &UiState, bindings: &Bindings) -> String {
    if state.transcript.entries().is_empty() {
        String::new()
    } else {
        format!("{} browse", primary_label(bindings, KeyAction::Browse))
    }
}

fn footer_hints(state: &UiState, bindings: &Bindings, width: usize) -> String {
    join_hints(footer_hint_parts(state, bindings, None), width)
}

/// The footer's key hints in order. An offered `panel` hint goes beside the interrupt
/// key during a run, and last otherwise.
fn footer_hint_parts(
    state: &UiState,
    bindings: &Bindings,
    mut panel: Option<String>,
) -> Vec<String> {
    let mut parts: Vec<String> = if matches!(state.view_status, ViewStatus::WaitingForApproval) {
        vec![
            "y once".into(),
            "t tool".into(),
            "a YOLO".into(),
            "n deny".into(),
            "Ctrl+G help".into(),
        ]
    } else if matches!(state.view_status, ViewStatus::WaitingForTrust) {
        vec!["y trust".into(), "n deny".into(), "Ctrl+G help".into()]
    } else if state.cancel_requested {
        vec!["Ctrl+G help".into()]
    } else if matches!(state.view_status, ViewStatus::Running)
        || state.interaction_status == crate::reducer::InteractionStatus::Compacting
    {
        // The composer names the keys that follow up or steer.
        let mut parts = vec!["Esc interrupt".into()];
        parts.extend(panel.take());
        parts.push(browse_hint(state, bindings));
        parts
    } else {
        // Help lists the remaining keys, sending included.
        vec!["Ctrl+G help".into(), browse_hint(state, bindings)]
    };
    parts.extend(panel);
    parts
}

#[allow(clippy::too_many_arguments)]
fn render_footer(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    viewport: &TranscriptViewport,
    notice: Option<&str>,
    palette: Palette,
    bindings: &Bindings,
    jump_painted: bool,
    identity_in_composer: bool,
    column_width: u16,
) {
    let width = usize::from(area.width);
    // Keep the established status priorities when outer presentation margins grow,
    // measured on the conversation's column rather than beside a docked panel.
    let terminal_width = usize::from(column_width);
    // A docked side panel shows the workspace, queue, mode, context, and model, so
    // the footer keeps only the status and hints beside it.
    let panel_docked = column_width < frame.area().width;
    let compaction_notice = state
        .context
        .compaction_notice
        .as_deref()
        .filter(|_| state.context.compaction_notice_is_warning);
    let notice = notice.or(compaction_notice);
    let status_budget = if notice.is_some() {
        if terminal_width >= 80 {
            (terminal_width / 2).max(24)
        } else if !state.configuration_active()
            && (state.view_status == ViewStatus::Running
                || state.interaction_status == crate::reducer::InteractionStatus::Compacting)
        {
            status_label(state).width()
        } else {
            0
        }
    } else {
        (terminal_width / 2).max(19)
    };

    // Right: the status the footer always keeps, label first since it carries the
    // status colour, then the hints that still fit.
    // While a run is going, the status line above the composer names the run, the
    // queue, and the context, so the footer keeps only scroll markers and hints.
    let mut status = Vec::new();
    if panel_docked {
        if !run_in_status_line(state) {
            status.push(status_label(state));
        }
    } else if !run_in_status_line(state) {
        let leading = footer_leading_parts(state, identity_in_composer);
        let leading_width = join_hints(leading.iter().cloned(), status_budget).width();
        let ctx_width = status_budget
            .saturating_sub(leading_width)
            .saturating_sub(if leading_width == 0 { 0 } else { 3 });
        status = leading;
        status.push(crate::context_view::indicator(state, ctx_width));
        status.extend(footer_trailing_parts(state, identity_in_composer));
    }
    if state.history.tail_evicted && viewport.follows_tail() {
        status.push("more ↓".into());
    } else if !jump_painted && viewport.has_unseen_output() {
        status.push("new ↓".into());
    } else if !jump_painted && !viewport.follows_tail() {
        status.push("scrolled".into());
    }
    let status_line = join_hints(status, status_budget);
    let status_width = status_line.width();

    // Left: a notice, or where commands run. On a narrow row the branch goes
    // first, then the hints; the folder stays while it fits beside the status.
    let hint_cap = |room: usize| {
        if terminal_width < 120 {
            room.min(56)
        } else {
            room
        }
    };
    let beside_status = width.saturating_sub(status_width.saturating_add(2));
    let (left, left_style, hints) = match notice {
        Some(notice) => (
            sanitize_for_terminal(notice),
            Style::default().fg(palette.warning),
            String::new(),
        ),
        None => {
            let workspace = if panel_docked {
                Vec::new()
            } else {
                footer_workspace_parts(state)
            };
            let hints = footer_hints(
                state,
                bindings,
                hint_cap(width.saturating_sub(status_width).saturating_sub(3)),
            );
            let hints_width = if hints.is_empty() {
                0
            } else {
                hints.width() + 3
            };
            let left_room = (width / 3).min(beside_status.saturating_sub(hints_width + 2));
            let left = join_hints(workspace.iter().cloned(), left_room);
            if !left.is_empty() || workspace.is_empty() {
                (left, Style::default().fg(palette.muted), hints)
            } else {
                // The folder alone does not fit beside every hint: keep the
                // folder and give the hints what remains.
                let left = join_hints(
                    workspace.into_iter().take(1),
                    (width / 3).min(beside_status),
                );
                let hint_room = beside_status
                    .saturating_sub(left.width())
                    .saturating_sub(if left.is_empty() { 1 } else { 3 });
                let hints = footer_hints(state, bindings, hint_cap(hint_room));
                (left, Style::default().fg(palette.muted), hints)
            }
        }
    };
    let left_width = left.width().min(beside_status);
    // Where the panel is not docked, offer it with what room the workspace and the
    // other hints leave, so it is the first thing a narrow footer drops.
    let hints = if panel_docked || hints.is_empty() || decision_pending(state) {
        hints
    } else {
        let room = beside_status.saturating_sub(left_width + usize::from(left_width > 0) * 2);
        let panel = format!("{} panel", primary_label(bindings, KeyAction::TogglePanel));
        let offer = join_hints(footer_hint_parts(state, bindings, Some(panel)), usize::MAX);
        if offer.width() + 3 <= hint_cap(room) {
            offer
        } else {
            hints
        }
    };

    let mut right = if status_line.is_empty() {
        Vec::new()
    } else {
        status_spans(state, &status_line, palette)
    };
    if !hints.is_empty() {
        if !right.is_empty() {
            right.push(Span::styled(" · ", Style::default().fg(palette.muted)));
        }
        right.extend(keycap_hints(&hints, palette).spans);
    }
    let right = Line::from(right).style(Style::default().fg(palette.muted));
    let right_width = right.width();
    frame.render_widget(
        Paragraph::new(left).style(left_style),
        Rect {
            width: left_width as u16,
            ..area
        },
    );
    frame.render_widget(
        Paragraph::new(right).alignment(Alignment::Right),
        Rect {
            x: area.right().saturating_sub(right_width as u16),
            width: right_width.min(width) as u16,
            ..area
        },
    );
}

/// Style the footer's status line: the label in the status colour and the context
/// meter in the warning colour once the window is mostly full.
fn status_spans(state: &UiState, status_line: &str, palette: Palette) -> Vec<Span<'static>> {
    let muted = Style::default().fg(palette.muted);
    let label = status_label(state);
    let Some(tail) = status_line.strip_prefix(&label) else {
        return vec![Span::styled(status_line.to_owned(), muted)];
    };
    let mut spans = vec![Span::styled(
        label,
        Style::default().fg(status_color(state, palette)),
    )];
    let pressured =
        crate::context_view::fill(state).is_some_and(|fill| fill >= CONTEXT_WARNING_FILL);
    match tail.find("ctx ").filter(|_| pressured) {
        Some(start) => {
            let meter_end = tail[start..]
                .find(" · ")
                .map_or(tail.len(), |end| start + end);
            spans.push(Span::styled(tail[..start].to_owned(), muted));
            spans.push(Span::styled(
                tail[start..meter_end].to_owned(),
                Style::default().fg(palette.warning),
            ));
            spans.push(Span::styled(tail[meter_end..].to_owned(), muted));
        }
        None => spans.push(Span::styled(tail.to_owned(), muted)),
    }
    spans
}

/// The context meter turns to the warning colour from this fill.
pub(crate) const CONTEXT_WARNING_FILL: f64 = 0.7;

/// Style joined `key action` hints with each key as a keycap. Keys never contain
/// spaces, so the first space ends the key; widths match the plain string.
pub(crate) fn keycap_hints(hints: &str, palette: Palette) -> Line<'static> {
    let mut spans = Vec::new();
    for (index, hint) in hints.split(" · ").enumerate() {
        if index > 0 {
            spans.push(Span::raw(" · "));
        }
        match hint.split_once(' ') {
            Some((key, action)) => {
                spans.push(Span::styled(key.to_owned(), palette.keycap()));
                spans.push(Span::raw(format!(" {action}")));
            }
            None => spans.push(Span::raw(hint.to_owned())),
        }
    }
    Line::from(spans)
}

fn editable(state: &UiState) -> bool {
    state.editor_editable()
}

/// What a pending decision shows, before it is fitted to the panel's height.
struct DecisionPanel {
    title: String,
    tag: Option<String>,
    detail: DecisionDetail,
    choices: Vec<(&'static str, String)>,
    footnote: Option<&'static str>,
    /// False when the panel cannot show what confirming would do; it is then
    /// drawn but never armed.
    confirmable: bool,
}

enum DecisionDetail {
    /// Wrapped over as many rows as fit; the last kept row ends in `…` when cut.
    Wrapped(String),
    /// Shown whole or not at all, one row group per line: what a confirmation
    /// confirms. When it does not fit, a hint replaces it and the panel cannot
    /// be confirmed (see [`decision_panel_complete`]).
    Complete(String),
    /// One row that keeps the end of the text, for paths.
    Tail(String),
}

fn decision_panel(state: &UiState, width: usize) -> Option<DecisionPanel> {
    match state.view_status {
        ViewStatus::WaitingForApproval => Some(approval_panel(state, width)),
        ViewStatus::WaitingForTrust => Some(trust_panel(state)),
        _ => None,
    }
}

/// A pending exit or skipped-response warning, which replaces a decision's choices.
fn decision_alert(notice: Option<&str>) -> Option<&str> {
    notice.filter(|notice| {
        notice.contains("Skipped approval response") || notice.contains("again exits")
    })
}

/// Whether the pending decision is shown in full at the terminal size `area`.
///
/// Only a [`DecisionDetail::Complete`] detail can be incomplete: a confirmation
/// must not be accepted while part of what it confirms is hidden. This repeats
/// [`render_interactive`]'s and [`render_decision_panel`]'s layout.
pub(crate) fn decision_panel_complete(state: &UiState, area: Rect, notice: Option<&str>) -> bool {
    if !decision_context_visible(area) {
        return false;
    }
    // An alert takes the choices' place and can push the details out of view.
    if decision_alert(notice).is_some() {
        return !matches!(
            decision_panel(state, decision_panel_width(conversation_surface(area))),
            Some(DecisionPanel {
                detail: DecisionDetail::Complete(_),
                ..
            })
        );
    }
    let surface = conversation_surface(area);
    let panel_area = if surface.height < DECISION_PANEL_SHARED_MIN_HEIGHT {
        surface
    } else {
        Rect {
            height: decision_panel_height(state, surface),
            ..surface
        }
    };
    let width = decision_panel_width(panel_area);
    let Some(panel) = decision_panel(state, width) else {
        return true;
    };
    if !panel.confirmable {
        return false;
    }
    let DecisionDetail::Complete(content) = &panel.detail else {
        return true;
    };
    // The title names what is confirmed (the server), so it must not be cut.
    let tag_width = panel.tag.as_ref().map_or(0, |tag| tag.width() + 3);
    if bounded_decision_preview(&panel.title).width() > width.saturating_sub(tag_width + 2) {
        return false;
    }
    let inner_height = usize::from(panel_area.height.saturating_sub(2));
    // Details, the separator, then every choice.
    complete_detail_rows(content, width).len() + 1 + panel.choices.len() <= inner_height
}

fn approval_panel(state: &UiState, width: usize) -> DecisionPanel {
    // Room for the selection marker, key and gap in front of each label.
    let label_width = width.saturating_sub(5);
    let fitting = |preferred: String, fallback: &str| {
        if preferred.width() <= label_width {
            preferred
        } else {
            fallback.to_owned()
        }
    };
    let Some(pending) = state.pending_approval.as_ref() else {
        return DecisionPanel {
            title: "Allow tool?".into(),
            tag: None,
            detail: DecisionDetail::Wrapped("Tool details unavailable · args: unavailable".into()),
            choices: approval_choices("Allow for session".into(), false, &fitting),
            footnote: Some(APPROVAL_FOOTNOTE),
            confirmable: true,
        };
    };
    if !pending.grants_allowed {
        return confirmation_panel(pending);
    }
    let name = bounded_decision_preview(&pending.name);
    DecisionPanel {
        title: format!("Allow {name}?"),
        tag: Some(bounded_decision_preview(&pending.safety)),
        detail: DecisionDetail::Wrapped(approval_detail(&pending.name, &pending.arguments)),
        choices: approval_choices(
            fitting(
                format!("Allow {name} for this session"),
                "Allow for session",
            ),
            pending.session_only_yolo,
            &fitting,
        ),
        footnote: Some(if pending.session_only_yolo {
            SESSION_ONLY_YOLO_FOOTNOTE
        } else {
            APPROVAL_FOOTNOTE
        }),
        confirmable: true,
    }
}

const APPROVAL_FOOTNOTE: &str = "YOLO is saved for this project. Change with /permissions.";
const SESSION_ONLY_YOLO_FOOTNOTE: &str =
    "YOLO lasts for this session; it is never saved in this folder.";

/// Choices of a tool that needs a confirmation on every call.
pub(crate) const CONFIRMATION_CHOICES: usize = 2;

/// A confirm-every-call tool's panel: what exactly it would do, then confirm or
/// deny. No grant is offered, and the detail is shown whole or not at all.
fn confirmation_panel(pending: &crate::reducer::PendingApproval) -> DecisionPanel {
    let unshowable = |title: String| DecisionPanel {
        title,
        tag: Some("confirm".into()),
        detail: DecisionDetail::Complete(
            "This proposal cannot be shown in full, so it cannot be confirmed. Deny it.".into(),
        ),
        choices: vec![("1", "Confirm".into()), ("2", "Deny".into())],
        footnote: None,
        confirmable: false,
    };
    let Some(arguments) = pending.arguments.as_object() else {
        return unshowable("Proposal unavailable".into());
    };
    let text = |key: &str| arguments.get(key).and_then(serde_json::Value::as_str);
    // Every listed field must have the shape the backend applies; anything else
    // could mean the panel shows less than what confirming would save.
    let strings = |key: &str| -> Option<Vec<String>> {
        match arguments.get(key) {
            None => Some(Vec::new()),
            Some(value) => value
                .as_array()?
                .iter()
                .map(|item| item.as_str().map(str::to_owned))
                .collect(),
        }
    };
    let Some(server) = text("name").map(bounded_decision_preview) else {
        return unshowable("Proposal unavailable".into());
    };
    // A field the panel does not show could still be saved; refuse it.
    let known: &[&str] = match pending.name.as_str() {
        "propose_mcp_server" => &[
            "name",
            "command",
            "args",
            "url",
            "env_from",
            "headers_from",
            "replace",
        ],
        "propose_mcp_server_change" => &["name", "action"],
        _ => &[],
    };
    if arguments.keys().any(|key| !known.contains(&key.as_str())) {
        return unshowable(match pending.name.as_str() {
            "propose_mcp_server" => format!("Add MCP server {server}?"),
            "propose_mcp_server_change" => format!("Change MCP server {server}?"),
            name => format!("Confirm {}?", bounded_decision_preview(name)),
        });
    }
    let (title, detail) = match pending.name.as_str() {
        "propose_mcp_server" => {
            let (Some(args), Some(forwarded)) = (strings("args"), strings("env_from")) else {
                return unshowable(format!("Add MCP server {server}?"));
            };
            let headers: Vec<(String, String)> = match arguments.get("headers_from") {
                None => Vec::new(),
                Some(serde_json::Value::Object(headers)) => {
                    let mut parsed = Vec::new();
                    for (header, template) in headers {
                        let Some((_scheme, variable)) = template
                            .as_str()
                            .and_then(crate::mcp_manager::parse_header_template)
                        else {
                            return unshowable(format!("Add MCP server {server}?"));
                        };
                        parsed.push((header.clone(), variable.to_owned()));
                    }
                    parsed
                }
                Some(_) => return unshowable(format!("Add MCP server {server}?")),
            };
            let mut rows = Vec::new();
            match (text("command"), text("url")) {
                (Some(command), None) => rows.push(format!(
                    "runs: {}",
                    crate::mcp_manager::shell_display(command, &args)
                )),
                (None, Some(url)) if args.is_empty() => rows.push(format!("connects to: {url}")),
                _ => return unshowable(format!("Add MCP server {server}?")),
            }
            if !forwarded.is_empty() {
                rows.push(format!(
                    "forwards from your environment: {}",
                    forwarded.join(", ")
                ));
            }
            match (text("url"), headers.is_empty()) {
                (_, true) => {}
                (Some(url), false) => {
                    let host = crate::mcp_manager::url_host(url);
                    rows.extend(headers.iter().map(|(header, variable)| {
                        crate::mcp_manager::header_forward_row(header, variable, host)
                    }));
                }
                // Headers on a command server would not be shown as sent anywhere.
                (None, false) => return unshowable(format!("Add MCP server {server}?")),
            }
            match arguments.get("replace") {
                None | Some(serde_json::Value::Bool(false)) => {}
                Some(serde_json::Value::Bool(true)) => rows.push(format!(
                    "replaces the server saved as {server}, dropping its saved values and settings"
                )),
                Some(_) => return unshowable(format!("Add MCP server {server}?")),
            }
            if text("command").is_some() {
                rows.push("Wisp runs this command every time it starts.".into());
            }
            (format!("Add MCP server {server}?"), rows.join("\n"))
        }
        "propose_mcp_server_change" => match text("action") {
            Some("remove") => (
                format!("Remove MCP server {server}?"),
                format!("Deletes the saved server {server}, including saved values, and stops it."),
            ),
            Some("disable") => (
                format!("Disable MCP server {server}?"),
                format!("Stops {server}; its settings are kept."),
            ),
            _ => return unshowable(format!("Change MCP server {server}?")),
        },
        name => return unshowable(format!("Confirm {}?", bounded_decision_preview(name))),
    };
    DecisionPanel {
        title,
        tag: Some("confirm".into()),
        detail: DecisionDetail::Complete(detail),
        choices: vec![("1", "Confirm".into()), ("2", "Deny".into())],
        footnote: Some("Wisp asks every time; YOLO and session grants never apply."),
        confirmable: true,
    }
}

fn approval_choices(
    tool_session: String,
    session_only_yolo: bool,
    fitting: &dyn Fn(String, &str) -> String,
) -> Vec<(&'static str, String)> {
    let yolo = if session_only_yolo {
        fitting("YOLO for this session".into(), "YOLO (session)")
    } else {
        fitting("YOLO for this project".into(), "YOLO (saved)")
    };
    vec![
        ("1", "Allow once".into()),
        ("2", tool_session),
        ("3", yolo),
        ("4", "Deny".into()),
    ]
}

/// The built-in shell tool (`src/wisp/tools/shell/tool.py`). Other tools may take a
/// `command` argument without running a shell, e.g. MCP tools with arbitrary schemas.
const SHELL_TOOL_NAME: &str = "bash";

/// Show the built-in shell tool's command as `$ command`, followed by every other
/// argument, because an approval must never hide what it approves. Every other
/// tool shows its complete argument object.
fn approval_detail(name: &str, arguments: &serde_json::Value) -> String {
    let command = (name == SHELL_TOOL_NAME)
        .then(|| arguments.as_object())
        .flatten()
        .and_then(|object| object.get("command"))
        .and_then(serde_json::Value::as_str);
    let Some(command) = command else {
        return format!("args: {}", bounded_json_preview(arguments));
    };
    let mut rest = arguments.as_object().cloned().unwrap_or_default();
    rest.remove("command");
    if rest.is_empty() {
        format!("$ {command}")
    } else {
        format!(
            "$ {command} · args: {}",
            bounded_json_preview(&serde_json::Value::Object(rest))
        )
    }
}

fn trust_panel(state: &UiState) -> DecisionPanel {
    let path = state
        .pending_trust_project_path
        .as_deref()
        .map(bounded_decision_tail_preview)
        .unwrap_or_else(|| "unknown project".into());
    DecisionPanel {
        title: "Trust this project?".into(),
        tag: None,
        detail: DecisionDetail::Tail(path),
        choices: vec![("y", "Trust project".into()), ("n", "Deny".into())],
        footnote: None,
        confirmable: true,
    }
}

/// Split sanitized text into rows of at most `width` columns, keeping at most
/// `max_rows` and marking a cut with `…`.
fn wrapped_decision_rows(content: &str, width: usize, max_rows: usize) -> Vec<String> {
    let safe = bounded_decision_preview(content);
    if width == 0 || max_rows == 0 {
        return Vec::new();
    }
    let mut rows = vec![String::new()];
    let mut row_width = 0;
    for grapheme in safe.graphemes(true) {
        let grapheme_width = grapheme.width();
        if row_width + grapheme_width > width {
            rows.push(String::new());
            row_width = 0;
        }
        rows.last_mut().expect("one row").push_str(grapheme);
        row_width += grapheme_width;
    }
    if rows.len() > max_rows {
        rows.truncate(max_rows);
        let last = rows.last_mut().expect("kept row");
        *last = decision_row(&format!("{last}…"), width);
    }
    rows
}

/// Every row of a [`DecisionDetail::Complete`] detail, sanitized and unbounded.
fn complete_detail_rows(content: &str, width: usize) -> Vec<String> {
    let mut rows = Vec::new();
    for line in sanitize_for_terminal(content).split('\n') {
        let mut row = String::new();
        let mut row_width = 0;
        for grapheme in line.graphemes(true) {
            let grapheme_width = grapheme.width();
            if row_width + grapheme_width > width && row_width > 0 {
                rows.push(std::mem::take(&mut row));
                row_width = 0;
            }
            row.push_str(grapheme);
            row_width += grapheme_width;
        }
        rows.push(row);
    }
    rows
}

const COMPLETE_DETAIL_HINT: &str = "Make the terminal larger to see everything before confirming.";

fn decision_detail_rows(detail: &DecisionDetail, width: usize, max_rows: usize) -> Vec<String> {
    match detail {
        DecisionDetail::Complete(content) => {
            let rows = complete_detail_rows(content, width);
            if rows.len() <= max_rows {
                rows
            } else {
                wrapped_decision_rows(COMPLETE_DETAIL_HINT, width, max_rows)
            }
        }
        DecisionDetail::Wrapped(content) => wrapped_decision_rows(content, width, max_rows),
        DecisionDetail::Tail(content) => vec![decision_tail_row(content, width)],
    }
}

/// Columns inside the panel's borders and padding.
fn decision_panel_width(area: Rect) -> usize {
    usize::from(area.width.saturating_sub(4)).max(1)
}

/// The panel's preferred height: borders, every detail row, a separator, the
/// choices, and the footnote with its gap.
fn decision_panel_height(state: &UiState, area: Rect) -> u16 {
    let width = decision_panel_width(area);
    let Some(panel) = decision_panel(state, width) else {
        return 0;
    };
    let details = match &panel.detail {
        DecisionDetail::Complete(content) => complete_detail_rows(content, width).len(),
        detail => decision_detail_rows(detail, width, DECISION_DETAIL_MAX_ROWS).len(),
    };
    let footnote = if panel.footnote.is_some() { 2 } else { 0 };
    let preferred = 2 + details + 1 + panel.choices.len() + footnote;
    u16::try_from(preferred)
        .unwrap_or(u16::MAX)
        .min(area.height.saturating_sub(DECISION_PANEL_RESERVED_ROWS))
}

pub(crate) fn choice_line(
    key: &str,
    label: &str,
    selected: bool,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let label = decision_row(label, width.saturating_sub(key.width() + 4));
    // The selected row is drawn in the selection style, so its key keeps that
    // style's colours instead of the primary accent.
    let key_style = if selected {
        Style::default().add_modifier(Modifier::BOLD)
    } else {
        Style::default()
            .fg(palette.primary)
            .add_modifier(Modifier::BOLD)
    };
    let line = Line::from(vec![
        Span::raw(if selected { "› " } else { "  " }),
        Span::styled(key.to_owned(), key_style),
        Span::raw("  "),
        Span::raw(label),
    ]);
    if selected {
        line.style(palette.selection())
    } else {
        line
    }
}

/// Draw the pending approval or trust decision as a panel docked at the bottom
/// of `area`. Rows are kept in priority order as the panel shrinks: the first
/// detail row and the choices always, then a separator, further detail rows,
/// and the footnote. A pending exit or skipped-response warning replaces the
/// choices so it is never hidden.
fn render_decision_panel(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    notice: Option<&str>,
    choice: Option<usize>,
    palette: Palette,
) {
    let width = decision_panel_width(area);
    let Some(panel) = decision_panel(state, width) else {
        return;
    };
    let tag_width = panel.tag.as_ref().map_or(0, |tag| tag.width() + 3);
    let title = decision_row(&panel.title, width.saturating_sub(tag_width + 2));
    let hint = if width >= 40 {
        " ↑↓ select · Enter confirm · Esc deny "
    } else {
        " ↑↓ Enter · Esc deny "
    };
    let mut block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(palette.warning))
        .padding(Padding::horizontal(1))
        .style(palette.composer())
        .title(
            Line::from(format!(" {title} ")).style(Style::default().add_modifier(Modifier::BOLD)),
        );
    if let Some(tag) = &panel.tag {
        block = block.title_top(
            Line::from(format!(" {tag} "))
                .style(Style::default().fg(palette.muted))
                .right_aligned(),
        );
    }
    if hint.width() + 2 <= usize::from(area.width) {
        block = block.title_bottom(
            Line::from(hint)
                .style(Style::default().fg(palette.muted))
                .right_aligned(),
        );
    }
    let inner = block.inner(area);
    frame.render_widget(Clear, area);
    frame.render_widget(block, area);

    let alert = decision_alert(notice);
    let bottom: Vec<Line<'static>> = match alert {
        Some(alert) => wrapped_decision_rows(alert, width, usize::from(inner.height))
            .into_iter()
            .map(|row| Line::styled(row, Style::default().fg(palette.warning)))
            .collect(),
        None => panel
            .choices
            .iter()
            .enumerate()
            .map(|(index, (key, label))| {
                choice_line(key, label, choice == Some(index), width, palette)
            })
            .collect(),
    };
    let height = usize::from(inner.height);
    let mut spare = height.saturating_sub(1 + bottom.len());
    let separator = spare > 0;
    spare = spare.saturating_sub(usize::from(separator));
    let max_detail_rows = match panel.detail {
        DecisionDetail::Complete(_) => 1 + spare,
        _ => (1 + spare).min(DECISION_DETAIL_MAX_ROWS),
    };
    let details = decision_detail_rows(&panel.detail, width, max_detail_rows);
    spare = spare.saturating_sub(details.len().saturating_sub(1));
    let footnote = panel.footnote.filter(|_| alert.is_none() && spare >= 2);

    let mut lines: Vec<Line<'static>> = details.into_iter().map(Line::from).collect();
    if separator {
        lines.push(Line::default());
    }
    lines.extend(bottom);
    if let Some(footnote) = footnote {
        lines.push(Line::default());
        lines.push(Line::styled(
            decision_row(footnote, width),
            Style::default().fg(palette.muted),
        ));
    }
    frame.render_widget(Paragraph::new(lines), inner);
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

/// Fit sanitized text to `width`, keeping its end (for paths) behind a leading `…`.
pub(crate) fn tail_row(content: &str, width: usize) -> String {
    decision_tail_row(&bounded_decision_tail_preview(content), width)
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
        ActiveCommand, ActiveCommandType, AgentMode, BackendEvent, CommandIdSource, CommandKind,
        InteractionStatus, PendingApproval, SessionIdentity, reduce,
    };
    use proptest::{
        prelude::*,
        test_runner::{Config, RngAlgorithm, TestRng, TestRunner},
    };
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use ratatui::style::Color;
    use serde_json::json;
    use wisp_protocol::events::{ContextAccountingMethod, ContextBudget, ContextEstimate};

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
            duration_ms: None,
            started_at_ms: None,
            process_started_at_ms: None,
            process_ended_at_ms: None,
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
        let mut composer_layout_cache = ComposerLayoutCache::default();
        terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    state,
                    &mut viewport,
                    &mut row_cache,
                    &mut composer_layout_cache,
                    editor,
                    &connection(),
                    notice,
                    None,
                    true,
                    None,
                    0,
                    Palette::default(),
                    bindings,
                    LogoChoice::AdalBraille,
                    None,
                    None,
                    None,
                );
            })
            .unwrap();
        terminal.backend().to_string()
    }

    fn render_to_string_with_activity(
        width: u16,
        height: u16,
        state: &UiState,
        activity_frame: u8,
    ) -> String {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut row_cache = TranscriptRowCache::default();
        let mut composer_layout_cache = ComposerLayoutCache::default();
        terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    state,
                    &mut viewport,
                    &mut row_cache,
                    &mut composer_layout_cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                    None,
                    true,
                    None,
                    activity_frame,
                    Palette::default(),
                    &Bindings::default(),
                    LogoChoice::AdalBraille,
                    None,
                    None,
                    None,
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
        assert!(rendered.contains("model-x fake"));
        assert!(rendered.contains("hello"));
        assert!(rendered.contains("Ctrl+G help"), "{rendered}");
        assert!(!rendered.contains("Enter send"), "{rendered}");
        assert!(!rendered.contains("/ commands"), "{rendered}");
        // Narrow footers drop optional hints; help remains available on Ctrl+G.
        assert!(!rendered.contains("rpc v"));
        assert!(!rendered.contains("events v"));
        assert!(!rendered.contains("backend "));
        assert!(!rendered.contains("PgUp"));
        assert!(!rendered.contains("theme"));
    }

    #[test]
    fn empty_idle_transcript_shows_a_responsive_wisp_welcome() {
        let state = UiState::new("fake".into(), Some("model-x".into()), None);
        let rendered = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(rendered.contains("Wisp 0.9.0"));
        assert!(rendered.contains(EMPTY_TRANSCRIPT_TAGLINE));
        assert!(rendered.contains(EMPTY_TRANSCRIPT_HINT));
        assert!(rendered.contains("Ask Wisp anything"));
        assert!(rendered.contains("Resume a session"));
        assert!(rendered.contains("Browse commands"));
        assert!(rendered.contains("Mention a project file"));
        assert!(!rendered.contains("Connect a provider"));
        assert!(rendered.contains("model-x fake"));

        let compact = render_to_string(30, 8, &state, &PromptEditor::default());
        assert!(compact.contains("Wisp 0.9.0"));
        assert!(compact.contains("Type a prompt or /"));
        assert!(compact.contains("idle"));
        assert!(!compact.contains("Resume a session"));

        let mut unconfigured = state;
        unconfigured.provider = None;
        // The framed composer's identity row leaves 13 transcript rows at 18; the
        // action list is guaranteed from 14 (see the welcome height contract).
        let rendered = render_to_string(80, 19, &unconfigured, &PromptEditor::default());
        assert!(rendered.contains("Connect a provider"));
        assert!(rendered.contains("/connect"));
    }

    #[test]
    fn empty_welcome_renders_selected_logo_with_its_color_treatment() {
        let state = UiState::new("fake".into(), Some("model-x".into()), None);
        let bindings = Bindings::default();
        for logo in LogoChoice::ALL {
            let lines = empty_transcript_lines(
                &state,
                &connection(),
                Palette::default(),
                35,
                80,
                &bindings,
                logo,
            );
            assert!(lines.len() <= 35, "{}", logo.name());
            assert!(lines.iter().all(|line| line.width() <= 80));
        }

        let monochrome = crate::theme::default_theme().palette(true);
        let lines = empty_transcript_lines(
            &state,
            &connection(),
            monochrome,
            14,
            80,
            &bindings,
            LogoChoice::AdalBraille,
        );
        let mark = lines
            .iter()
            .flat_map(|line| line.spans.iter())
            .find(|span| {
                span.style.fg == Some(monochrome.primary) && !span.content.trim().is_empty()
            })
            .expect("monochrome mark row");
        assert_eq!(mark.style.fg, Some(monochrome.primary));
        assert_eq!(mark.style.bg, None);
    }

    #[test]
    fn welcome_copy_fits_supported_transcript_sizes() {
        for configured in [true, false] {
            let mut state = UiState::new("fake".into(), Some("model-x".into()), None);
            if !configured {
                state.provider = None;
            }
            for width in [28, 29, 30, 39, 40, 44, 78] {
                for height in 1..=20 {
                    let lines = empty_transcript_lines(
                        &state,
                        &connection(),
                        Palette::default(),
                        height,
                        width,
                        &Bindings::default(),
                        LogoChoice::AdalBraille,
                    );
                    assert!(lines.len() <= height);
                    assert!(lines.iter().all(|line| line.width() <= width));
                }
            }
        }
    }

    #[test]
    fn welcome_actions_survive_responsive_logo_size_transitions() {
        let bindings = Bindings::default();
        for logo in LogoChoice::ALL {
            let configured = UiState::new("fake".into(), Some("model-x".into()), None);
            for height in 13..=35 {
                let text = empty_transcript_lines(
                    &configured,
                    &connection(),
                    Palette::default(),
                    height,
                    80,
                    &bindings,
                    logo,
                )
                .into_iter()
                .map(|line| line.to_string())
                .collect::<String>();
                assert!(
                    text.contains("Ask Wisp anything"),
                    "{} at {height}",
                    logo.name()
                );
                assert!(
                    text.contains("Resume a session"),
                    "{} at {height}",
                    logo.name()
                );
            }

            let mut disconnected = configured;
            disconnected.provider = None;
            for height in 14..=35 {
                let text = empty_transcript_lines(
                    &disconnected,
                    &connection(),
                    Palette::default(),
                    height,
                    80,
                    &bindings,
                    logo,
                )
                .into_iter()
                .map(|line| line.to_string())
                .collect::<String>();
                assert!(
                    text.contains("Connect a provider"),
                    "{} at {height}",
                    logo.name()
                );
            }
        }
    }

    #[test]
    fn footer_keeps_mode_context_and_identity_beside_a_notice() {
        let mut state = UiState::new(
            "fake".into(),
            Some("custom-model".into()),
            Some("effort".into()),
        );
        state.mode = AgentMode::Plan;
        state.mode_confirmed = true;
        let rendered = render_to_string_with_notice(
            100,
            18,
            &state,
            &PromptEditor::default(),
            Some("Model catalog unavailable. Use /model <model> or press r to retry."),
        );
        assert!(
            rendered.contains("Plan · custom-model fake · effort"),
            "{rendered}"
        );
        assert!(rendered.contains("ctx"), "{rendered}");
        assert!(rendered.contains('~'), "{rendered}");
        assert!(rendered.contains("custom-model fake"), "{rendered}");
        assert!(rendered.contains("effort"), "{rendered}");
        assert!(rendered.contains("Model catalog unavailable"), "{rendered}");

        state.context.compaction_notice =
            Some("threshold compaction completed: 10 entries replaced, 4 retained.".into());
        let completed = render_to_string(100, 18, &state, &PromptEditor::default());
        assert!(!completed.contains("compaction completed"), "{completed}");

        state.context.compaction_notice = Some("threshold compaction failed.".into());
        state.context.compaction_notice_is_warning = true;
        let failed = render_to_string(100, 18, &state, &PromptEditor::default());
        assert!(failed.contains("compaction failed"), "{failed}");
    }

    #[test]
    fn footer_keeps_compact_context_and_leaves_out_the_session_on_an_80_column_run() {
        let mut state = UiState::new("fake".into(), Some("model-x".into()), None);
        state.mode = AgentMode::Build;
        state.mode_confirmed = true;
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.context.budget = Some(ContextBudget {
            estimate: ContextEstimate { total_tokens: 4000 },
            observed_tokens: None,
            observed_is_current: false,
            effective_tokens: None,
            accounting_method: ContextAccountingMethod::FullyEstimated,
            context_window: Some(10_000),
            reserve_tokens: 1000,
        });
        state.selected_session = Some(SessionIdentity {
            session_id: "demo".into(),
            session_path: "/sessions/demo.jsonl".into(),
            session_name: Some("demo-session".into()),
        });
        // While running, the status line above the composer carries the context.
        let running = render_to_string(80, 18, &state, &PromptEditor::default());
        let status = running
            .lines()
            .find(|line| line.contains("◐"))
            .unwrap_or_else(|| panic!("status line: {running}"));
        assert!(status.contains("ctx ~40%"), "{running}");

        state.view_status = ViewStatus::Idle;
        state.interaction_status = InteractionStatus::Idle;
        state.current_command = None;
        let idle = render_to_string(100, 18, &state, &PromptEditor::default());
        assert!(!idle.contains("demo-session"), "{idle}");
        assert!(!idle.contains("demo.jsonl"), "{idle}");
        assert!(idle.contains("ctx"), "{idle}");
    }

    #[test]
    fn newly_submitted_prompt_is_visible_before_the_reply_begins() {
        let mut state = UiState::new("fake".into(), None, None);
        state.transcript.append_prompt("older prompt".into());
        state.transcript.complete_message(
            1,
            (0..80)
                .map(|index| format!("older-assistant-line-{index}"))
                .collect::<Vec<_>>()
                .join("\n"),
        );
        state
            .transcript
            .append_prompt("CURRENT-PROMPT-MARKER".into());
        state.view_status = ViewStatus::Running;
        state.interaction_status = crate::reducer::InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });

        let rendered = render_to_string(80, 18, &state, &PromptEditor::default());

        assert!(rendered.contains("CURRENT-PROMPT-MARKER"), "{rendered}");
        assert!(!rendered.contains("older-assistant-line-79"), "{rendered}");
    }

    #[test]
    fn follow_tail_pins_a_compact_prompt_above_a_long_assistant_echo() {
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
        assert!(rendered.contains("Pasted content #1"), "{rendered}");
        assert!(rendered.contains("2006"), "{rendered}");
        assert!(rendered.contains("6011"), "{rendered}");
        assert!(rendered.contains("🙂END"), "{rendered}");
    }

    #[test]
    fn follow_tail_keeps_the_current_prompt_and_long_assistant_tail_visible() {
        for (width, height) in [(80, 18), (36, 12)] {
            let mut state = UiState::new("fake".into(), None, None);
            state.transcript.append_prompt("LATEST-USER".into());
            state.transcript.complete_message(
                1,
                (0..80)
                    .map(|index| format!("assistant-line-{index}"))
                    .chain(std::iter::once("FINAL-ASSISTANT-LINE".into()))
                    .collect::<Vec<_>>()
                    .join("\n"),
            );

            let rendered = render_to_string(width, height, &state, &PromptEditor::default());
            assert!(rendered.contains("LATEST-USER"), "{rendered}");
            assert!(rendered.contains("FINAL-ASSISTANT-LINE"), "{rendered}");
        }
    }

    #[test]
    fn follow_tail_only_shows_the_latest_turn_when_it_fits() {
        let mut state = UiState::new("fake".into(), None, None);
        state.transcript.append_prompt("older-prompt".into());
        state.transcript.complete_message(
            1,
            (0..40)
                .map(|index| format!("OLDER-ASSISTANT-LINE-{index}"))
                .collect::<Vec<_>>()
                .join("\n"),
        );
        state
            .transcript
            .append_prompt("latest-prompt-marker".into());
        state.transcript.complete_message(2, "short-ok".into());
        let rendered = render_to_string(80, 18, &state, &PromptEditor::default());
        let latest = rendered
            .find("latest-prompt-marker")
            .expect("latest user turn");
        let short = rendered.find("short-ok").expect("latest assistant");
        assert!(latest < short, "{rendered}");
        assert!(!rendered.contains("OLDER-ASSISTANT-LINE-"), "{rendered}");
        assert!(!rendered.contains("older-prompt"), "{rendered}");
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
        let welcome_action = rendered
            .lines()
            .find(|line| line.contains("Ask Wisp anything"))
            .unwrap();
        assert!(welcome_action.contains("Ctrl+Enter"));
        assert!(!rendered.contains("F4 history"));
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
        assert!(
            running.contains("Enter follow-up · Alt+Enter steer"),
            "{running}"
        );
        assert!(running.contains("Esc interrupt"), "{running}");
        assert!(!running.contains("working"), "{running}");

        state.current_command = None;
        state.interaction_status = InteractionStatus::Compacting;
        let compacting = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(compacting.contains("Esc interrupt"), "{compacting}");
        assert!(compacting.contains("Compacting context"), "{compacting}");
        assert!(!compacting.contains("Enter send"), "{compacting}");
    }

    fn status_row(state: &UiState, width: u16, elapsed: Option<u64>) -> (String, TestBackend) {
        let mut terminal = Terminal::new(TestBackend::new(width, 1)).unwrap();
        terminal
            .draw(|frame| {
                render_status_line(
                    frame,
                    frame.area(),
                    state,
                    0,
                    elapsed.map(std::time::Duration::from_secs),
                    Palette::default(),
                );
            })
            .unwrap();
        let backend = terminal.backend().clone();
        let text = (0..width)
            .map(|x| backend.buffer()[(x, 0)].symbol().to_owned())
            .collect::<String>();
        (text, backend)
    }

    fn running_state() -> UiState {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.transcript.append_prompt("first".into());
        state.transcript.complete_message(1, "done".into());
        let prompt = state.transcript.append_prompt("check ci".into());
        state.run_progress = Some(crate::reducer::RunProgress { prompt, turn: 6 });
        state
    }

    #[test]
    fn status_line_names_the_run() {
        let mut state = running_state();
        // Between steps the phase stands in for a running call.
        let (idle_step, _) = status_row(&state, 100, Some(151));
        assert!(
            idle_step.starts_with("◐ Working · turn 6 0 tools · 2m 31s"),
            "{idle_step}"
        );

        state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "b1".into(),
                name: "bash".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"command": "gh pr checks 766 --watch"}),
            });
        let (text, backend) = status_row(&state, 100, Some(151));
        assert!(
            text.starts_with("◐  Bash  gh pr checks 766 --watch "),
            "{text}"
        );
        assert!(text.contains(" · turn 6 1 tool · 2m 31s"), "{text}");
        assert!(text.trim_end().ends_with("ctx ~?"), "{text}");
        let palette = Palette::default();
        assert_eq!(style_at_text(&backend, "◐").unwrap().0, palette.secondary);
        assert_eq!(
            style_at_text(&backend, "Bash").map(|style| (Some(style.0), Some(style.1))),
            Some((palette.chip("Bash").fg, palette.chip("Bash").bg))
        );

        // A short row clips the command, then drops the turn details, and keeps
        // the context on the right.
        let (narrow, _) = status_row(&state, 40, Some(151));
        assert!(narrow.contains('…'), "{narrow}");
        assert!(!narrow.contains("turn 6"), "{narrow}");
        assert!(narrow.trim_end().ends_with("ctx ~?"), "{narrow}");

        // Cancelling outranks the call still running.
        state.cancel_requested = true;
        let (cancelling, _) = status_row(&state, 100, None);
        assert!(
            cancelling.starts_with("◐ Cancelling · turn 6 1 tool"),
            "{cancelling}"
        );
        state.cancel_requested = false;

        // A follow-up the run takes in joins it, so the tool count carries on.
        state.transcript.observe_tool_result({
            let mut result = tool_result("b1", "ok");
            result.name = "bash".into();
            result
        });
        state.transcript.append_prompt("follow-up".into());
        let (joined, _) = status_row(&state, 100, None);
        assert!(joined.contains("turn 6 1 tool"), "{joined}");
    }

    #[test]
    fn running_card_shows_live_timer() {
        let mut state = running_state();
        state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "b1".into(),
                name: "bash".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"command": "sleep 5"}),
            });
        let mut terminal = Terminal::new(TestBackend::new(80, 20)).unwrap();
        terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    &state,
                    &mut TranscriptViewport::default(),
                    &mut TranscriptRowCache::default(),
                    &mut ComposerLayoutCache::default(),
                    &PromptEditor::default(),
                    &connection(),
                    None,
                    None,
                    true,
                    None,
                    0,
                    Palette::default(),
                    &Bindings::default(),
                    LogoChoice::AdalBraille,
                    None,
                    None,
                    None,
                );
            })
            .unwrap();
        let backend = terminal.backend();
        let buffer = backend.buffer();
        let row = (0..buffer.area.height)
            .find(|&y| {
                (0..buffer.area.width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
                    .contains("sleep 5")
            })
            .expect("card row");
        let text = (0..buffer.area.width)
            .map(|x| buffer[(x, row)].symbol())
            .collect::<String>();
        // The card's own clock ticks at the right edge in the running colour.
        let end = (0..buffer.area.width)
            .rev()
            .find(|&x| buffer[(x, row)].symbol() == "s")
            .expect("timer");
        assert!(
            usize::from(end) > text.find("sleep 5").unwrap() + 10,
            "{text}"
        );
        assert_eq!(
            buffer[(end, row)].fg,
            Palette::default().secondary,
            "{text}"
        );
    }

    #[test]
    fn composer_cap_follows_gap() {
        let state = UiState::new("fake".into(), None, None);
        let mut editor = PromptEditor::default();
        editor.insert_paste(&"line\n".repeat(20));
        let mut cache = ComposerLayoutCache::default();
        let mut height = |rows: u16| {
            let area = Rect::new(0, 0, 60, rows);
            let layout = cache.layout(&editor, composer_editor_width(area.width));
            composer_height(area, &state, Some(layout))
        };
        // A framed composer shows six rows of a long draft with or without the blank
        // row, which is the only row the higher ceiling adds.
        assert_eq!(height(14), 8);
        assert_eq!(height(20), 9);
        assert_eq!(height(30), 10);
    }

    #[test]
    fn status_line_fits_short_screens() {
        let state = running_state();
        let mut editor = PromptEditor::default();
        editor.insert_paste(&"line\n".repeat(20));
        let mut cache = ComposerLayoutCache::default();
        for height in 8..20 {
            let area = Rect::new(0, 0, 60, height);
            let layout = cache.layout(&editor, composer_editor_width(area.width));
            // The transcript keeps two rows beside the status line and the footer.
            assert!(
                composer_height(area, &state, Some(layout)) + status_line_height(&state) + 3
                    <= height,
                "height {height}"
            );
        }
    }

    #[test]
    fn status_line_shows_queue_and_pressure() {
        let mut state = running_state();
        let mut ids = TestIds::default();
        reduce(
            &mut state,
            crate::reducer::UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["steer".into()],
                follow_up: vec!["later".into()],
            }),
            &mut ids,
        )
        .unwrap();
        state.context.budget = Some(ContextBudget {
            estimate: ContextEstimate {
                total_tokens: 7_100,
            },
            observed_tokens: None,
            observed_is_current: false,
            effective_tokens: None,
            accounting_method: ContextAccountingMethod::FullyEstimated,
            context_window: Some(10_000),
            reserve_tokens: 1000,
        });
        let (text, backend) = status_row(&state, 100, None);
        assert!(text.trim_end().ends_with("2 queued · ctx ~71%"), "{text}");
        let palette = Palette::default();
        assert_eq!(
            style_at_text(&backend, "2 queued").unwrap().0,
            palette.secondary
        );
        assert_eq!(style_at_text(&backend, "~71%").unwrap().0, palette.warning);
    }

    #[test]
    fn activity_indicator_changes_only_for_active_work() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        let first = render_to_string_with_activity(80, 18, &state, 0);
        let second = render_to_string_with_activity(80, 18, &state, 1);
        assert!(first.contains("◐ Working"), "{first}");
        assert!(second.contains("◓ Working"), "{second}");

        state.interaction_status = InteractionStatus::Compacting;
        let compacting = render_to_string_with_activity(80, 18, &state, 2);
        assert!(compacting.contains("◑ Compacting context"), "{compacting}");

        state.view_status = ViewStatus::Idle;
        state.interaction_status = InteractionStatus::Idle;
        let idle_first = render_to_string_with_activity(80, 18, &state, 0);
        let idle_second = render_to_string_with_activity(80, 18, &state, 1);
        assert!(idle_first.contains("idle"), "{idle_first}");
        assert_eq!(idle_first, idle_second);
    }

    #[test]
    fn narrow_footer_keeps_activity_visible_beside_notices() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        for width in [30, 60] {
            for (interaction, label) in [
                (InteractionStatus::Running, "◐ Working"),
                (InteractionStatus::Compacting, "◐ Compacting context"),
            ] {
                state.interaction_status = interaction;
                let rendered = render_to_string_with_notice(
                    width,
                    18,
                    &state,
                    &PromptEditor::default(),
                    Some("Prompt history is empty"),
                );
                assert!(rendered.contains(label), "{rendered}");
                assert!(rendered.contains("Prompt hist"), "{rendered}");
            }
        }
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
    fn running_composer_names_its_keys() {
        let mut state = running_state();
        let mut ids = TestIds::default();
        reduce(
            &mut state,
            crate::reducer::UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["first\u{1b}[2J\u{202e}spoof".into()],
                follow_up: vec!["later one".into()],
            }),
            &mut ids,
        )
        .unwrap();

        let empty = render_to_string(100, 20, &state, &PromptEditor::default());
        assert!(empty.contains("Message to steer or follow up…"), "{empty}");
        assert!(
            empty.contains("Enter follow-up · Alt+Enter steer"),
            "{empty}"
        );
        // The status line counts the queue; the composer no longer lists it.
        assert!(empty.contains("2 queued"), "{empty}");
        assert!(!empty.contains("spoof"), "{empty}");
        assert!(!empty.contains("later one"), "{empty}");
        // The footer keeps the hints a run needs and leaves status to the line above.
        assert!(empty.contains("Esc interrupt"), "{empty}");
        assert!(!empty.contains("working"), "{empty}");

        let mut editor = PromptEditor::default();
        editor.insert_paste("new steering");
        let drafted = render_to_string(100, 20, &state, &editor);
        assert!(drafted.contains("new steering"), "{drafted}");

        // Too narrow for the queue beside the phase: what is running stays.
        let tiny = render_to_string(30, 8, &state, &editor);
        assert!(!tiny.contains("terminal too"), "{tiny}");
        assert!(tiny.contains("◐ Working"), "{tiny}");
        assert!(!tiny.contains("turn 6"), "{tiny}");
    }

    #[test]
    fn approval_and_trust_are_truthful_blocking_states() {
        let mut home = UiState::unconfigured();
        home.view_status = ViewStatus::WaitingForApproval;
        home.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "bash".into(),
            arguments: json!({"command": "ls"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: true,
        });
        let approval = render_to_string(80, 18, &home, &PromptEditor::default());
        // In the home folder YOLO is a session grant; the panel must not promise
        // that it is saved for the project.
        assert!(approval.contains("3  YOLO for this session"), "{approval}");
        assert!(!approval.contains("for this project"), "{approval}");
        assert!(
            approval.contains("YOLO lasts for this session; it is never saved in this folder.")
        );

        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "bash".into(),
            arguments: json!({"command": "rm -rf /tmp/example"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "ask".into(),
            grants_allowed: true,
            session_only_yolo: false,
        });
        let approval = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(approval.contains("approval"));
        assert!(approval.contains("Allow bash?"));
        assert!(approval.contains(" ask "));
        assert!(approval.contains("$ rm -rf /tmp/example"));
        assert!(approval.contains("1  Allow once"));
        assert!(approval.contains("2  Allow bash for this session"));
        assert!(approval.contains("3  YOLO for this project"));
        assert!(approval.contains("4  Deny"));
        assert!(approval.contains("Enter confirm"));
        assert!(approval.contains("y once"));
        // The panel docks in place of the composer instead of covering the transcript.
        let panel_top = approval
            .lines()
            .position(|line| line.contains("Allow bash?"))
            .unwrap();
        assert!(panel_top > 2, "{approval}");

        state.pending_approval = Some(PendingApproval {
            call_id: "call-1b".into(),
            name: "bash".into(),
            arguments: json!({"command": "ls", "cwd": "/tmp/elsewhere"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        });
        let with_other_arguments = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(with_other_arguments.contains("$ ls"));
        assert!(
            with_other_arguments.contains("/tmp/elsewhere"),
            "arguments besides the command stay visible: {with_other_arguments}"
        );

        // Only the built-in shell tool gets the `$` form; an MCP tool's `command`
        // argument keeps its name, since that tool may not run a shell.
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1c".into(),
            name: "mcp__deploy__run".into(),
            arguments: json!({"command": "promote", "target": "prod"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        });
        let mcp = render_to_string(80, 18, &state, &PromptEditor::default());
        assert!(!mcp.contains("$ promote"), "{mcp}");
        assert!(
            mcp.contains(r#"args: {"command":"promote","target":"prod"}"#),
            "{mcp}"
        );

        state.pending_approval = Some(PendingApproval {
            call_id: "call-2".into(),
            name: "shell\u{1b}[2J\u{202e}spoof\nnext".into(),
            arguments: json!({}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "ask\u{2066}safe".into(),
            grants_allowed: true,
            session_only_yolo: false,
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
            grants_allowed: true,
            session_only_yolo: false,
        });
        let bounded = wrapped_decision_rows(
            &approval_detail("bash", &state.pending_approval.as_ref().unwrap().arguments),
            usize::MAX,
            usize::MAX,
        )
        .join("\n");
        assert!(bounded.contains('…'));
        assert!(bounded.len() < DECISION_PREVIEW_GRAPHEMES + 180);

        state.pending_approval = Some(PendingApproval {
            call_id: "call-4".into(),
            name: "very-long-tool-name-".repeat(20),
            arguments: json!({"command": "rm -rf /tmp/example"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        });
        let narrow = render_to_string(30, 14, &state, &PromptEditor::default());
        assert!(narrow.contains("3  YOLO (saved)"));
        assert!(narrow.contains("2  Allow for session"));
        assert!(narrow.contains("rm -rf"));
        let minimum_approval = render_to_string(30, 8, &state, &PromptEditor::default());
        for choice in [
            "1  Allow once",
            "2  Allow for session",
            "3  YOLO (saved)",
            "4  Deny",
        ] {
            assert!(minimum_approval.contains(choice), "{minimum_approval}");
        }
        // One detail row fits; a non-shell tool keeps its argument names.
        assert!(
            minimum_approval.contains("args: {\"command\""),
            "{minimum_approval}"
        );
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
            assert!(!compact_notice.contains("3  YOLO (saved)"));
            assert!(compact_notice.contains("Allow very"));
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
        assert!(trust.contains("trust"));
        assert!(trust.contains("Trust this project?"));
        assert!(trust.contains("y  Trust project"));
        assert!(trust.contains("n  Deny"));
        assert!(trust.contains("/workspace/project�[2J�spoof next"));
        assert!(!trust.contains('\u{1b}'));
        assert!(!trust.contains('\u{202e}'));

        state.pending_trust_project_path = Some(format!(
            "/very/long/shared/prefix/{}/distinct-project",
            "nested/".repeat(30)
        ));
        let narrow_trust = render_to_string(30, 14, &state, &PromptEditor::default());
        assert!(narrow_trust.contains("y  Trust project"));
        assert!(narrow_trust.contains("distinct-project"));
        assert!(!narrow_trust.contains("/very/long/shared/prefix"));
        let minimum_trust = render_to_string(30, 8, &state, &PromptEditor::default());
        assert!(minimum_trust.contains("y  Trust project"));
        assert!(minimum_trust.contains("n  Deny"));
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
            assert!(compact_notice.contains("Trust this project?"));
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
    fn terminal_sanitizer_neutralizes_generated_controls_and_bidi_overrides() {
        let config = Config {
            cases: 256,
            failure_persistence: None,
            ..Config::default()
        };
        let rng = TestRng::from_seed(RngAlgorithm::ChaCha, &[0x48; 32]);
        let mut runner = TestRunner::new_with_rng(config, rng);
        let strategy = proptest::collection::vec(any::<char>(), 0..512)
            .prop_map(|characters| characters.into_iter().collect::<String>());

        runner
            .run(&strategy, |source| {
                let sanitized = sanitize_for_terminal(&source);
                let terminal_safe = sanitized.chars().all(|character| {
                    character == '\n'
                        || (!character.is_control() && !crate::is_bidi_control(character))
                });
                prop_assert!(terminal_safe);
                prop_assert!(std::str::from_utf8(sanitized.as_bytes()).is_ok());
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn terminal_sanitizer_neutralizes_named_escape_families() {
        let source = concat!(
            "safe",
            "\u{1b}[2J",
            "\u{1b}]0;owned\u{7}",
            "\u{1b}]8;;https://example.invalid\u{7}link\u{1b}]8;;\u{7}",
            "\u{1b}]52;c;QUJD\u{7}",
            "\u{1b}[?1049h",
            "\u{009b}31m",
            "\u{202e}",
            "tail",
        );
        let sanitized = sanitize_for_terminal(source);

        for control in ['\u{1b}', '\u{7}', '\u{009b}', '\u{202e}'] {
            assert!(!sanitized.contains(control));
        }
        assert!(sanitized.contains("safe"));
        assert!(sanitized.contains("tail"));
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
        assert!(rendered.contains("idle"));
        assert!(!rendered.contains("terminal too"));
    }

    #[test]
    fn permission_gate_keeps_tool_context_and_all_choices_at_short_sizes() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "bash".into(),
            arguments: json!({"command": "rm -rf /tmp/example"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "ask".into(),
            grants_allowed: true,
            session_only_yolo: false,
        });
        state
            .transcript
            .append_prompt("context-for-approval".into());

        for height in [8, 10, 11, 12] {
            let rendered = render_to_string(80, height, &state, &PromptEditor::default());
            assert!(rendered.contains("Allow bash?"), "{rendered}");
            assert!(rendered.contains("$ rm -rf /tmp/example"), "{rendered}");
            for choice in [
                "1  Allow once",
                "2  Allow bash for this session",
                "3  YOLO for this project",
                "4  Deny",
            ] {
                assert!(rendered.contains(choice), "{rendered}");
            }
        }
        // From 11 rows the panel docks below the transcript instead of replacing it.
        let docked = render_to_string(80, 14, &state, &PromptEditor::default());
        assert!(docked.contains("context-for-approval"), "{docked}");
    }

    #[test]
    fn monochrome_decision_panel_marks_the_highlight_without_colour() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "shell".into(),
            arguments: json!({"command": "ls"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        });
        let palette = crate::theme::resolve("glass").unwrap().palette(true);
        let mut terminal = Terminal::new(TestBackend::new(60, 10)).unwrap();
        terminal
            .draw(|frame| {
                render_decision_panel(frame, frame.area(), &state, None, Some(1), palette);
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        let row = (0..10)
            .find(|y| {
                (0..60)
                    .map(|x| buffer[(x, *y)].symbol())
                    .collect::<String>()
                    .contains("› 2")
            })
            .expect("highlighted row");
        let marker = (0..60).find(|x| buffer[(*x, row)].symbol() == "›").unwrap();
        assert!(
            buffer[(marker, row)].modifier.contains(Modifier::REVERSED),
            "the monochrome highlight must not rely on colour"
        );
    }

    #[test]
    fn glass_permission_gate_uses_an_opaque_surface() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "shell".into(),
            arguments: json!({"command": "rm -rf /tmp/example"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "ask".into(),
            grants_allowed: true,
            session_only_yolo: false,
        });
        let palette = crate::theme::resolve("glass").unwrap().palette(false);
        assert_eq!(palette.base().bg, None);

        let mut terminal = Terminal::new(TestBackend::new(80, 11)).unwrap();
        terminal
            .draw(|frame| {
                render_decision_panel(frame, frame.area(), &state, None, None, palette);
            })
            .unwrap();

        let buffer = terminal.backend().buffer();
        assert_eq!(buffer[(1, 1)].bg, palette.surface);
        assert_eq!(buffer[(78, 9)].bg, palette.surface);
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
    fn selecting_a_newline_after_a_full_row_shows_its_cell_before_the_caret_line() {
        use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
        let mut editor = PromptEditor::default();
        editor.insert_paste("abcd\nx");
        for (code, modifiers) in [
            (KeyCode::Home, KeyModifiers::ALT),
            (KeyCode::End, KeyModifiers::NONE),
            (KeyCode::Right, KeyModifiers::SHIFT),
        ] {
            editor.handle_key(KeyEvent::new(code, modifiers));
        }
        assert_eq!(editor.selection_range(), Some(4..5));
        let layout = composer_layout(&editor, 4);
        assert_eq!(layout.rows.len(), 3);
        assert_eq!(layout.cursor_row, 2);
        let line = styled_composer_line(
            &layout.rows[1],
            &[],
            layout.lines[0].selection.as_ref(),
            4,
            Palette::default(),
        );
        assert_eq!(line.spans[0].content, " ");
        assert_eq!(line.spans[0].style, Palette::default().selection());
    }

    #[test]
    fn composer_selection_covers_tabs_wide_graphemes_wraps_and_newlines() {
        let mut editor = PromptEditor::default();
        editor.insert_paste("a\t界e\u{301}\n\nlast");
        editor.handle_key(crossterm::event::KeyEvent::new(
            crossterm::event::KeyCode::Char('a'),
            crossterm::event::KeyModifiers::ALT,
        ));
        let layout = composer_layout(&editor, 4);
        assert_eq!(layout.lines[0].selection, Some(0..8));
        assert_eq!(layout.lines[1].selection, Some(0..1));
        for monochrome in [false, true] {
            let palette = crate::theme::default_theme().palette(monochrome);
            for row in &layout.rows {
                let line = styled_composer_line(
                    row,
                    &[],
                    layout.lines[row.logical_row].selection.as_ref(),
                    4,
                    palette,
                );
                for span in line.spans {
                    assert_eq!(span.style, palette.selection());
                }
            }
        }
        assert_eq!(editor.text(), "a\t界e\u{301}\n\nlast");
    }

    #[test]
    fn composer_selection_overrides_color_but_keeps_semantic_modifiers() {
        let row = ComposerVisualRow {
            logical_row: 0,
            column_start: 0,
            text: "/theme".into(),
        };
        let palette = Palette::default();
        let line = styled_composer_line(
            &row,
            &[Highlight {
                columns: 0..6,
                kind: PromptHighlightKind::Command,
            }],
            Some(&(1..4)),
            80,
            palette,
        );
        assert_eq!(
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect::<String>(),
            "/theme"
        );
        assert_eq!(line.spans[1].content, "the");
        assert_eq!(line.spans[1].style.bg, palette.selection().bg);
        assert!(line.spans[1].style.add_modifier.contains(Modifier::BOLD));
        assert_ne!(line.spans[0].style.bg, palette.selection().bg);
        assert_ne!(line.spans[2].style.bg, palette.selection().bg);
    }

    #[test]
    fn composer_styles_commands_and_project_references_without_changing_geometry() {
        use std::sync::Arc;
        use wisp_protocol::events::{ProjectFileEntry, ProjectFileKind, ProjectFileSnapshot};

        let mut state = UiState::new("fake".into(), None, None);
        state.project_files.snapshot = Some(Arc::new(ProjectFileSnapshot {
            generation: 1,
            entries: vec![ProjectFileEntry {
                path: "src/main.rs".into(),
                kind: ProjectFileKind::File,
            }],
            truncated: false,
        }));
        let mut editor = PromptEditor::default();
        editor.restore_prompt("/theme @src/main.rs @missing");
        let cursor = (editor.cursor_row(), editor.cursor_column());
        let backend = TestBackend::new(80, 12);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut row_cache = TranscriptRowCache::default();
        let mut composer_cache = ComposerLayoutCache::default();
        terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    &state,
                    &mut viewport,
                    &mut row_cache,
                    &mut composer_cache,
                    &editor,
                    &connection(),
                    None,
                    None,
                    true,
                    None,
                    0,
                    Palette::default(),
                    &Bindings::default(),
                    LogoChoice::AdalBraille,
                    None,
                    None,
                    None,
                );
            })
            .unwrap();

        let command = style_at_text(terminal.backend(), "/theme").unwrap();
        let resolved = style_at_text(terminal.backend(), "@src/main.rs").unwrap();
        let unresolved = style_at_text(terminal.backend(), "@missing").unwrap();
        assert_eq!(command.0, Palette::default().primary);
        assert!(command.2.contains(Modifier::BOLD));
        assert_eq!(resolved.0, Palette::default().success);
        assert!(resolved.2.contains(Modifier::UNDERLINED));
        assert_eq!(unresolved.0, Palette::default().warning);
        assert!(unresolved.2.contains(Modifier::UNDERLINED));
        assert_eq!((editor.cursor_row(), editor.cursor_column()), cursor);
        assert_eq!(editor.text(), "/theme @src/main.rs @missing");
    }

    #[test]
    fn workspace_label_shortens() {
        let home = Some("/Users/ana");
        assert_eq!(workspace_label("/Users/ana", home), "~");
        assert_eq!(workspace_label("/Users/ana/Wisp", home), "~/Wisp");
        assert_eq!(
            workspace_label("/Users/anabel/Wisp", home),
            "/Users/anabel/Wisp"
        );
        assert_eq!(workspace_label("/srv/wisp", None), "/srv/wisp");
        assert_eq!(
            workspace_label("/Users/ana/work/organization/team/deeply-nested-repo", home),
            "…/deeply-nested-repo"
        );
    }

    #[test]
    fn workspace_parts_are_cleaned() {
        let mut state = UiState::new("fake".into(), None, None);
        state.workspace_cwd = Some("/srv/evil\u{1b}[2Jdir".into());
        state.git_branch = Some(format!("main\u{202e}{}", "x".repeat(60)));
        let parts = footer_workspace_parts(&state);
        assert!(
            parts.iter().all(|part| !part.contains('\u{1b}')),
            "{parts:?}"
        );
        assert!(
            parts.iter().all(|part| !part.contains('\u{202e}')),
            "{parts:?}"
        );
        assert!(parts[1].width() <= BRANCH_MAX_COLUMNS, "{parts:?}");
        assert!(parts[1].ends_with('…'), "{parts:?}");
    }

    #[test]
    fn tall_composer_spaces_identity() {
        let rows = |height: u16| {
            let mut state =
                UiState::new("fake".into(), Some("model-x".into()), Some("high".into()));
            state.mode_confirmed = true;
            let mut terminal = Terminal::new(TestBackend::new(100, height)).unwrap();
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        &state,
                        &mut TranscriptViewport::default(),
                        &mut TranscriptRowCache::default(),
                        &PromptEditor::default(),
                        &connection(),
                        None,
                    );
                })
                .unwrap();
            let buffer = terminal.backend().buffer().clone();
            (0..height)
                .map(|y| {
                    (0..buffer.area.width)
                        .map(|x| buffer[(x, y)].symbol())
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
        };
        // From 24 rows a blank row sits between the draft and the identity.
        let tall = rows(30);
        assert!(tall[25].contains("› Ask Wisp anything"), "{}", tall[25]);
        let gap = tall[26].trim();
        assert!(
            gap.starts_with('│') && gap.trim_matches('│').trim().is_empty(),
            "{gap}"
        );
        assert!(
            tall[27].contains("Build · model-x fake · high"),
            "{}",
            tall[27]
        );
        // A shorter screen keeps that row for the transcript.
        let short = rows(20);
        assert!(short[16].contains("› Ask Wisp anything"), "{}", short[16]);
        assert!(
            short[17].contains("Build · model-x fake · high"),
            "{}",
            short[17]
        );
    }

    #[test]
    fn framed_composer_moves_identity() {
        let draw = |height: u16| {
            let mut state =
                UiState::new("fake".into(), Some("model-x".into()), Some("high".into()));
            state.mode_confirmed = true;
            state.workspace_cwd = Some("/srv/wisp".into());
            state.git_branch = Some("feat/x".into());
            let mut terminal = Terminal::new(TestBackend::new(100, height)).unwrap();
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        &state,
                        &mut TranscriptViewport::default(),
                        &mut TranscriptRowCache::default(),
                        &PromptEditor::default(),
                        &connection(),
                        None,
                    );
                })
                .unwrap();
            let buffer = terminal.backend().buffer().clone();
            let row = |y: u16| {
                (0..buffer.area.width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
            };
            (row(height - 3), row(height - 1))
        };

        let (identity, footer) = draw(20);
        assert!(
            identity
                .trim_start()
                .starts_with("│  Build · model-x fake · high"),
            "{identity}"
        );
        assert!(
            footer.trim_start().starts_with("/srv/wisp · feat/x"),
            "{footer}"
        );
        assert!(footer.contains("idle · ctx"), "{footer}");
        assert!(!footer.contains("model-x"), "{footer}");
        assert!(!footer.contains("build"), "{footer}");

        // Too short to frame the composer: the footer keeps mode and identity.
        let (_, footer) = draw(12);
        assert!(footer.contains("idle · build"), "{footer}");
        assert!(footer.contains("fake/model-x · high"), "{footer}");
    }

    #[test]
    fn narrow_footer_drops_branch_then_hints() {
        let footer = |width: u16| {
            let mut state = UiState::new("fake".into(), Some("model-x".into()), None);
            state.workspace_cwd = Some("/srv/wisp".into());
            state.git_branch = Some("feat/footer".into());
            let mut terminal = Terminal::new(TestBackend::new(width, 20)).unwrap();
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        &state,
                        &mut TranscriptViewport::default(),
                        &mut TranscriptRowCache::default(),
                        &PromptEditor::default(),
                        &connection(),
                        None,
                    );
                })
                .unwrap();
            let buffer = terminal.backend().buffer().clone();
            (0..width)
                .map(|x| buffer[(x, 19)].symbol())
                .collect::<String>()
        };

        let wide = footer(100);
        assert!(wide.contains("/srv/wisp · feat/footer"), "{wide}");
        assert!(wide.contains("idle · ctx"), "{wide}");
        assert!(wide.contains("help"), "{wide}");

        let narrow = footer(60);
        assert!(narrow.contains("/srv/wisp"), "{narrow}");
        assert!(!narrow.contains("feat/footer"), "{narrow}");
        assert!(narrow.contains("idle · ctx"), "{narrow}");

        let tight = footer(40);
        assert!(tight.contains("/srv/wisp"), "{tight}");
        assert!(!tight.contains("help"), "{tight}");
        assert!(tight.contains("ctx"), "{tight}");
    }

    #[test]
    fn footer_keys_render_as_keycaps() {
        let state = UiState::new("fake".into(), None, None);
        let mut terminal = Terminal::new(TestBackend::new(100, 20)).unwrap();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    &state,
                    &mut TranscriptViewport::default(),
                    &mut TranscriptRowCache::default(),
                    &PromptEditor::default(),
                    &connection(),
                    None,
                );
            })
            .unwrap();
        let palette = Palette::default();
        let key = style_at_text(terminal.backend(), "Ctrl+G").unwrap();
        assert_eq!((key.0, key.1), (palette.foreground, palette.panel));
        let action = style_at_text(terminal.backend(), " help").unwrap();
        assert_eq!(action.0, palette.muted);
        assert_ne!(action.1, palette.panel);
    }

    #[test]
    fn composer_border_tracks_status() {
        let border = |state: &UiState| {
            let mut terminal = Terminal::new(TestBackend::new(80, 20)).unwrap();
            let mut viewport = TranscriptViewport::default();
            let mut row_cache = TranscriptRowCache::default();
            let mut composer_cache = ComposerLayoutCache::default();
            terminal
                .draw(|frame| {
                    render_interactive(
                        frame,
                        state,
                        &mut viewport,
                        &mut row_cache,
                        &mut composer_cache,
                        &PromptEditor::default(),
                        &connection(),
                        None,
                        None,
                        true,
                        None,
                        0,
                        Palette::default(),
                        &Bindings::default(),
                        LogoChoice::AdalBraille,
                        None,
                        None,
                        None,
                    );
                })
                .unwrap();
            terminal.backend().clone()
        };
        let color = |backend: &TestBackend, text: &str| style_at_text(backend, text).unwrap().0;
        let palette = Palette::default();
        let mut state = UiState::new("fake".into(), Some("model-x".into()), Some("high".into()));
        state.context.budget = Some(ContextBudget {
            estimate: ContextEstimate { total_tokens: 8000 },
            observed_tokens: None,
            observed_is_current: false,
            effective_tokens: None,
            accounting_method: ContextAccountingMethod::FullyEstimated,
            context_window: Some(10_000),
            reserve_tokens: 1000,
        });
        let idle = border(&state);
        // The border dims at idle while the accent bar and effort stay lit.
        assert_eq!(color(&idle, "╮"), palette.muted);
        // The accent runs the composer's full height, corners included.
        assert_eq!(color(&idle, "╭"), palette.accent);
        assert_eq!(color(&idle, "╰"), palette.accent);
        assert_eq!(color(&idle, "│›"), palette.accent);
        assert_eq!(color(&idle, "high"), palette.warning);
        // An 80% full window turns the meter amber.
        assert_eq!(color(&idle, "▰"), palette.warning);
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        assert!(state.editor_editable());
        // A run keeps the border quiet and shows its colour on the accent bar.
        let running = border(&state);
        assert_eq!(color(&running, "╮"), palette.muted);
        assert_eq!(color(&running, "╭"), palette.secondary);

        state.view_status = ViewStatus::Idle;
        state.interaction_status = InteractionStatus::Idle;
        state.current_command = None;
        state.context.budget.as_mut().unwrap().estimate.total_tokens = 2000;
        assert_eq!(color(&border(&state), "▰"), palette.muted);
    }

    #[test]
    fn composer_styles_markdown_structure_without_changing_source() {
        let state = UiState::new("fake".into(), None, None);
        let source = "# Heading\n- list with `code`\n```rust\nfn main() {}\n```";
        let mut editor = PromptEditor::default();
        editor.restore_prompt(source);
        let backend = TestBackend::new(80, 14);
        let mut terminal = Terminal::new(backend).unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut row_cache = TranscriptRowCache::default();
        let mut composer_cache = ComposerLayoutCache::default();
        terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    &state,
                    &mut viewport,
                    &mut row_cache,
                    &mut composer_cache,
                    &editor,
                    &connection(),
                    None,
                    None,
                    true,
                    None,
                    0,
                    Palette::default(),
                    &Bindings::default(),
                    LogoChoice::AdalBraille,
                    None,
                    None,
                    None,
                );
            })
            .unwrap();

        let heading = style_at_text(terminal.backend(), "# Heading").unwrap();
        let marker = style_at_text(terminal.backend(), "-").unwrap();
        let inline = style_at_text(terminal.backend(), "code").unwrap();
        let fence = style_at_text(terminal.backend(), "```").unwrap();
        let info = style_at_text(terminal.backend(), "rust").unwrap();
        let body = style_at_text(terminal.backend(), "fn main() {}").unwrap();
        let palette = Palette::default();
        assert_eq!(heading.0, palette.primary);
        assert!(heading.2.contains(Modifier::BOLD));
        assert_eq!(marker.0, palette.muted);
        assert!(!marker.2.contains(Modifier::BOLD));
        assert_eq!(inline.0, palette.foreground);
        assert_eq!(inline.1, palette.panel);
        assert_eq!(fence.0, palette.secondary);
        assert!(fence.2.contains(Modifier::BOLD));
        assert_eq!(info.0, palette.accent);
        assert!(info.2.contains(Modifier::ITALIC));
        assert_eq!(body.0, palette.secondary);
        assert_eq!(editor.text(), source);
    }

    #[test]
    fn monochrome_and_wrapped_composer_highlights_keep_non_color_semantics() {
        let row = ComposerVisualRow {
            logical_row: 0,
            column_start: 5,
            text: "missing".into(),
        };
        let line = styled_composer_line(
            &row,
            &[Highlight {
                columns: 0..12,
                kind: PromptHighlightKind::UnresolvedPath,
            }],
            None,
            80,
            crate::theme::default_theme().palette(true),
        );
        assert_eq!(line.spans[0].content, "missing");
        assert!(
            line.spans[0]
                .style
                .add_modifier
                .contains(Modifier::UNDERLINED | Modifier::DIM)
        );
    }

    #[test]
    fn folded_multiline_drafts_do_not_highlight_commands_as_executable() {
        let mut state = UiState::new("fake".into(), None, None);
        state.command_catalog = Some(std::sync::Arc::from([
            wisp_protocol::events::CommandDescriptor {
                name: "help".into(),
                description: String::new(),
                slash_command: "/help".into(),
                slash_aliases: Vec::new(),
                order: 0,
            },
        ]));
        let mut editor = PromptEditor::default();
        editor.insert_paste(&format!("/help {}\ncontinued", "x".repeat(2_001)));
        let layout = composer_layout(&editor, 80);
        let context = prompt_highlighting::Context::new(state.command_catalog.as_deref(), None);
        let highlights = prompt_highlighting::line_highlights(
            &layout.lines[0].text,
            layout.lines[0].truncated,
            0,
            layout.raw_line_count,
            &context,
        );

        assert!(highlights.is_empty());
        assert_eq!(layout.raw_line_count, 2);
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
    fn composer_layout_wraps_tabs_wide_text_and_a_full_cursor_row() {
        for (prompt, width, expected) in [
            ("ab界c", 4, vec!["ab界", "c"]),
            ("abc\tz", 4, vec!["abc ", "z"]),
            ("abc界xyz", 4, vec!["abc", "界xy", "z"]),
            ("alpha beta tail", 10, vec!["alpha ", "beta tail"]),
            ("abcd", 4, vec!["abcd", ""]),
        ] {
            let mut editor = PromptEditor::default();
            editor.restore_prompt(prompt);
            let projection = editor.projection();
            let layout = composer_layout(&editor, width);
            assert_eq!(
                layout
                    .rows
                    .iter()
                    .map(|row| row.text.as_str())
                    .collect::<Vec<_>>(),
                expected
            );
            assert_eq!(
                composer_visual_row_count(&projection, width),
                layout.rows.len()
            );
            assert_eq!(layout.cursor_row, layout.rows.len() - 1);
            assert!(layout.cursor_column < width);
            assert_eq!(editor.text(), prompt);
        }
    }

    #[test]
    fn composer_layout_cache_reuses_large_unfolded_drafts_until_inputs_change() {
        let mut editor = PromptEditor::default();
        editor.replace_range(0..0, &"x".repeat(70_000));
        let mut cache = ComposerLayoutCache::default();

        assert!(cache.layout(&editor, 40).rows.len() > 1_000);
        assert_eq!(cache.rebuilds(), 1);
        assert!(cache.layout(&editor, 40).rows.len() > 1_000);
        assert_eq!(cache.rebuilds(), 1);

        editor.handle_key(crossterm::event::KeyEvent::new(
            crossterm::event::KeyCode::Left,
            crossterm::event::KeyModifiers::NONE,
        ));
        cache.layout(&editor, 40);
        assert_eq!(cache.rebuilds(), 2);
        cache.layout(&editor, 39);
        assert_eq!(cache.rebuilds(), 3);
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

        let screen = terminal.backend().to_string();
        assert!(screen.contains("↓ new output · Ctrl+End"), "{screen}");
        assert!(!screen.contains("new ↓"), "{screen}");
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

        assert!(terminal.backend().to_string().contains("more ↓"));
    }

    fn observe_mcp_call(
        state: &mut UiState,
        call_id: &str,
        tool: &str,
        id: &str,
    ) -> crate::transcript::TranscriptEntryId {
        state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: call_id.into(),
                name: format!("mcp__linear__{tool}"),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"id": id}),
            })
    }

    fn finish_call(state: &mut UiState, call_id: &str, name: &str) {
        let mut result = tool_result(call_id, "ok");
        result.name = name.into();
        state.transcript.observe_tool_result(result);
    }

    fn draw_cards(state: &UiState, cache: &mut TranscriptRowCache) -> String {
        // Fit the expanded command fixture as well as its collapsed summaries.
        let mut terminal = Terminal::new(TestBackend::new(100, 32)).unwrap();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    state,
                    &mut TranscriptViewport::default(),
                    cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                );
            })
            .unwrap();
        terminal.backend().to_string()
    }

    #[test]
    fn consecutive_tools_group_without_merging_different_tools() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("plan".into());
        observe_mcp_call(&mut state, "l1", "list_issues", "HY-0");
        finish_call(&mut state, "l1", "mcp__linear__list_issues");
        for (call_id, tool, id) in [
            ("g1", "get_issue", "HY-1"),
            ("g2", "get_issue", "HY-2"),
            ("g3", "get_issue", "HY-3"),
            ("s1", "save_issue", "HY-4"),
            ("s2", "save_issue", "HY-5"),
        ] {
            observe_mcp_call(&mut state, call_id, tool, id);
            finish_call(&mut state, call_id, &format!("mcp__linear__{tool}"));
        }
        for (call_id, command) in [("b1", "cargo fmt"), ("b2", "cargo test")] {
            state
                .transcript
                .observe_tool_call(crate::tool_cards::ToolCallInput {
                    call_id: call_id.into(),
                    name: "bash".into(),
                    detail_source: crate::tool_detail::ToolDetailSource::None,
                    arguments: json!({"command": command}),
                });
            finish_call(&mut state, call_id, "bash");
        }
        let first = state.transcript.entries()[7].id;
        let mut cache = TranscriptRowCache::default();
        let screen = draw_cards(&state, &mut cache);
        assert!(
            // The preview uses each command's own row text, which names `cargo test`.
            screen.contains("Bash    ▸ 2 commands · cargo fmt, Rust tests"),
            "{screen}"
        );
        assert_eq!(screen.matches("cargo fmt").count(), 1, "{screen}");
        cache.fold_mut().expand_group(first);
        let screen = draw_cards(&state, &mut cache);
        assert!(
            screen.contains("✓  MCP     ▸ linear/list_issues  id=HY-0"),
            "{screen}"
        );
        assert!(
            screen.contains("✓  MCP     ▸ linear/get_issue ×3  id=HY-1 … id=HY-3"),
            "{screen}"
        );
        assert!(
            screen.contains("✓  MCP     ▸ linear/save_issue ×2  id=HY-4 · id=HY-5"),
            "{screen}"
        );
        // Expanding the group reveals every command's individual row.
        assert!(
            screen.contains("cargo fmt") && screen.contains("Rust tests"),
            "{screen}"
        );
    }

    #[test]
    fn mcp_group_draws_chip() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("plan".into());
        for (call_id, id) in [("g1", "HY-1"), ("g2", "HY-2")] {
            observe_mcp_call(&mut state, call_id, "get_issue", id);
            finish_call(&mut state, call_id, "mcp__linear__get_issue");
        }
        let mut terminal = Terminal::new(TestBackend::new(100, 12)).unwrap();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    &state,
                    &mut TranscriptViewport::default(),
                    &mut TranscriptRowCache::default(),
                    &PromptEditor::default(),
                    &connection(),
                    None,
                );
            })
            .unwrap();
        let palette = Palette::default();
        let (_, chip_bg, _) = style_at_text(terminal.backend(), "MCP ").unwrap();
        assert_eq!(Some(chip_bg), palette.chip("MCP").bg);
        let (marker_fg, marker_bg, _) = style_at_text(terminal.backend(), "✓").unwrap();
        assert_ne!(Some(marker_bg), palette.chip("MCP").bg);
        assert_eq!(marker_fg, palette.success);
    }

    #[test]
    fn live_call_leads_header() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("plan".into());
        observe_mcp_call(&mut state, "g1", "get_issue", "HY-1");
        finish_call(&mut state, "g1", "mcp__linear__get_issue");
        observe_mcp_call(&mut state, "g2", "get_issue", "HY-2");
        let mut cache = TranscriptRowCache::default();
        let screen = draw_cards(&state, &mut cache);
        assert!(
            screen.contains("MCP     ▸ linear/get_issue ×2  id=HY-2 · 1 of 2 done"),
            "{screen}"
        );
        // The cached header follows a later member's result.
        finish_call(&mut state, "g2", "mcp__linear__get_issue");
        let screen = draw_cards(&state, &mut cache);
        assert!(
            screen.contains("✓  MCP     ▸ linear/get_issue ×2  id=HY-1 · id=HY-2"),
            "{screen}"
        );
        assert!(!screen.contains("of 2 done"), "{screen}");
    }

    #[test]
    fn approval_keeps_question_marker() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("plan".into());
        observe_mcp_call(&mut state, "s1", "save_issue", "HY-1");
        finish_call(&mut state, "s1", "mcp__linear__save_issue");
        state
            .transcript
            .observe_approval_requested(crate::tool_cards::ToolCallInput {
                call_id: "s2".into(),
                name: "mcp__linear__save_issue".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"id": "HY-2"}),
            });
        let screen = draw_cards(&state, &mut TranscriptRowCache::default());
        assert!(
            screen.contains(
                "?  MCP     ▸ linear/save_issue ×2  id=HY-2 · 1 of 2 done · awaiting approval"
            ),
            "{screen}"
        );
    }

    #[test]
    fn narrow_group_keeps_count() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("plan".into());
        for call_id in ["c1", "c2", "c3"] {
            observe_mcp_call(&mut state, call_id, "create_pull_request_review", "1");
            finish_call(
                &mut state,
                call_id,
                "mcp__linear__create_pull_request_review",
            );
        }
        let mut terminal = Terminal::new(TestBackend::new(36, 12)).unwrap();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    &state,
                    &mut TranscriptViewport::default(),
                    &mut TranscriptRowCache::default(),
                    &PromptEditor::default(),
                    &connection(),
                    None,
                );
            })
            .unwrap();
        let screen = terminal.backend().to_string();
        assert!(screen.contains("✓  MCP     ▸ linear/… ×3"), "{screen}");
    }

    #[test]
    fn group_preview_counts_hidden_members() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("check".into());
        for (call_id, command) in [
            ("b1", "git status"),
            ("b2", "git diff --stat"),
            ("b3", "git log -1"),
            ("b4", "git branch --show-current"),
            ("b5", "git remote -v"),
        ] {
            state
                .transcript
                .observe_tool_call(crate::tool_cards::ToolCallInput {
                    call_id: call_id.into(),
                    name: "bash".into(),
                    detail_source: crate::tool_detail::ToolDetailSource::None,
                    arguments: json!({"command": command}),
                });
            finish_call(&mut state, call_id, "bash");
        }
        let screen = |width| {
            let mut terminal = Terminal::new(TestBackend::new(width, 12)).unwrap();
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        &state,
                        &mut TranscriptViewport::default(),
                        &mut TranscriptRowCache::default(),
                        &PromptEditor::default(),
                        &connection(),
                        None,
                    );
                })
                .unwrap();
            terminal.backend().to_string()
        };
        let wide = screen(80);
        assert!(
            wide.contains("▸ 5 commands · git status, git diff --stat +3"),
            "{wide}"
        );
        let narrow = screen(36);
        assert!(narrow.contains("▸ 5 commands"), "{narrow}");
        assert!(!narrow.contains("git status"), "{narrow}");
    }

    #[test]
    fn group_preview_is_relative() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("look".into());
        state.transcript.set_tool_cwd("/repo");
        for (call_id, path) in [("r1", "/repo/src/a.rs"), ("r2", "/repo/src/b.rs")] {
            state
                .transcript
                .observe_tool_call(crate::tool_cards::ToolCallInput {
                    call_id: call_id.into(),
                    name: "read".into(),
                    detail_source: crate::tool_detail::ToolDetailSource::None,
                    arguments: json!({"path": path}),
                });
            finish_call(&mut state, call_id, "read");
        }
        let screen = draw_cards(&state, &mut TranscriptRowCache::default());
        assert!(
            screen.contains("▸ 2 actions · read src/a.rs, read src/b.rs"),
            "{screen}"
        );
    }

    #[test]
    fn preview_counts_unknown_members() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("look".into());
        state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "r1".into(),
                name: "read".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"path": "src/a.rs"}),
            });
        finish_call(&mut state, "r1", "read");
        // A result with no call event leaves its arguments unknown.
        finish_call(&mut state, "r2", "read");
        let mut cache = TranscriptRowCache::default();
        let screen = draw_cards(&state, &mut cache);
        assert!(
            screen.contains("▸ 2 actions · read src/a.rs +1"),
            "{screen}"
        );
    }

    #[test]
    fn diff_preview_rows_share_a_surface() {
        use std::fmt::Write as _;
        let old_text = format!(
            "keep\n{}",
            (0..9).fold(String::new(), |mut lines, line| {
                let _ = writeln!(lines, "old {line}");
                lines
            })
        );
        let new_text = format!(
            "keep\n{}",
            (0..9).fold(String::new(), |mut lines, line| {
                let _ = writeln!(lines, "new {line}");
                lines
            })
        );
        let (screen, backend) = render_file_card(
            "edit",
            json!({
                "path": "notes.txt",
                "edits": [{"oldText": old_text, "newText": new_text}]
            }),
            None,
            false,
            80,
        );
        let palette = Palette::default();
        // Unchanged lines and the "more changed lines" row sit on the same surface
        // as the tinted changes, so the preview reads as one block.
        assert_eq!(
            style_at_text(&backend, "keep").map(|style| style.1),
            Some(palette.surface),
            "{screen}"
        );
        assert_eq!(
            style_at_text(&backend, "more changed lines").map(|style| style.1),
            Some(palette.surface),
            "{screen}"
        );
        assert_eq!(
            style_at_text(&backend, "− old 0").map(|style| style.1),
            Some(palette.deletion_background),
            "{screen}"
        );
    }

    #[test]
    fn shell_output_is_framed() {
        let draw = |lines: usize| {
            let mut state = UiState::unconfigured();
            state.transcript.append_prompt("run".into());
            let card = state
                .transcript
                .observe_tool_call(crate::tool_cards::ToolCallInput {
                    call_id: "b1".into(),
                    name: "bash".into(),
                    detail_source: crate::tool_detail::ToolDetailSource::None,
                    arguments: json!({"command": "cargo test"}),
                });
            let body = (0..lines)
                .map(|line| format!("line {line}"))
                .collect::<Vec<_>>()
                .join("\n");
            let mut result = tool_result("b1", &format!("Command exited with code 3: {body}"));
            result.name = "bash".into();
            result.exit_code = Some(3);
            result.output_has_exit_status = true;
            if lines > 500 {
                // Like the live event: the output and its tail are both cut to 500
                // lines, while the source counts still describe all of it.
                let keep = |range: std::ops::Range<usize>| {
                    range
                        .map(|line| format!("line {line}"))
                        .collect::<Vec<_>>()
                        .join("\n")
                };
                result.output = format!("Command exited with code 3: {}", keep(0..500));
                result.output_tail = Some(keep(lines - 500..lines));
            }
            state.transcript.observe_tool_result(result);
            let mut cache = TranscriptRowCache::default();
            cache.fold_mut().expand(card);
            let mut terminal = Terminal::new(TestBackend::new(100, 32)).unwrap();
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        &state,
                        &mut TranscriptViewport::default(),
                        &mut cache,
                        &PromptEditor::default(),
                        &connection(),
                        None,
                    );
                })
                .unwrap();
            terminal.backend().clone()
        };
        let palette = Palette::default();

        // Short output loses the completion envelope and closes with the exit status.
        let short = draw(3);
        let text = short.to_string();
        assert!(!text.contains("Command exited"), "{text}");
        assert!(text.contains("│  line 0"), "{text}");
        assert_eq!(style_at_text(&short, "line 0").unwrap().1, palette.surface);
        assert_eq!(style_at_text(&short, "exit 3").unwrap().1, palette.surface);

        // A long tail counts the lines above it instead of the bytes.
        let long = draw(40);
        let text = long.to_string();
        assert!(text.contains("(32 earlier lines)"), "{text}");
        assert!(!text.contains("bytes omitted"), "{text}");
        assert!(text.contains("line 39"), "{text}");
        assert!(text.contains("exit 3"), "{text}");

        // Lines the projected tail already dropped still count.
        let projected = draw(600).to_string();
        assert!(projected.contains("(592 earlier lines)"), "{projected}");
    }

    #[test]
    fn fold_arrows_share_a_column() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("run".into());
        for (call_id, command) in [("b1", "cargo fmt"), ("b2", "cargo check")] {
            state
                .transcript
                .observe_tool_call(crate::tool_cards::ToolCallInput {
                    call_id: call_id.into(),
                    name: "bash".into(),
                    detail_source: crate::tool_detail::ToolDetailSource::None,
                    arguments: json!({"command": command}),
                });
            finish_call(&mut state, call_id, "bash");
        }
        state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "r1".into(),
                name: "read".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"path": "README.md"}),
            });
        finish_call(&mut state, "r1", "read");
        let screen = draw_cards(&state, &mut TranscriptRowCache::default());
        let column = |needle: &str| {
            let line = screen
                .lines()
                .find(|line| line.contains(needle))
                .unwrap_or_else(|| panic!("missing {needle:?}: {screen}"));
            line[..line.find(needle).unwrap()].chars().count()
        };
        // A group and a single foldable card put the arrow, then their text, in the
        // same columns.
        assert_eq!(column("▸ 2 commands"), column("▸ README.md"), "{screen}");
    }

    #[test]
    fn finished_rows_show_duration() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("run".into());
        state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "b1".into(),
                name: "bash".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"command": "cargo test"}),
            });
        let mut result = tool_result("b1", "ok");
        result.name = "bash".into();
        result.duration_ms = Some(252_000);
        state.transcript.observe_tool_result(result);
        let screen = draw_cards(&state, &mut TranscriptRowCache::default());
        // The duration sits in a right-aligned column, off the action text.
        let row = screen
            .lines()
            .find(|line| line.contains("Rust tests"))
            .expect("action row");
        assert!(!row.contains("· 4m 12s"), "{screen}");
        assert!(
            row.trim_end_matches(['"', ' ']).ends_with("4m 12s"),
            "{screen}"
        );
    }

    #[test]
    fn turn_summary_closes_the_rail() {
        let draw = |state: &UiState| {
            let mut terminal = Terminal::new(TestBackend::new(80, 16)).unwrap();
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        state,
                        &mut TranscriptViewport::default(),
                        &mut TranscriptRowCache::default(),
                        &PromptEditor::default(),
                        &connection(),
                        None,
                    );
                })
                .unwrap();
            terminal.backend().clone()
        };
        let rail_at = |backend: &TestBackend, text: &str| {
            let buffer = backend.buffer();
            let y = (0..buffer.area.height)
                .find(|&y| {
                    (0..buffer.area.width)
                        .map(|x| buffer[(x, y)].symbol())
                        .collect::<String>()
                        .contains(text)
                })
                .unwrap_or_else(|| panic!("missing {text:?}"));
            let x = (0..buffer.area.width)
                .find(|&x| !buffer[(x, y)].symbol().trim().is_empty())
                .unwrap();
            let cell = &buffer[(x, y)];
            (cell.symbol().to_owned(), cell.fg)
        };
        let palette = Palette::default();
        let mut state = running_state();
        state.transcript.complete_message(2, "Done.".into());
        // While the turn runs, its rail takes the running colour through the reply.
        let running = draw(&state);
        assert_eq!(
            rail_at(&running, "Done."),
            ("│".to_owned(), palette.running_rail())
        );

        state.view_status = ViewStatus::Idle;
        state.interaction_status = InteractionStatus::Idle;
        state.current_command = None;
        assert!(
            state
                .transcript
                .attach_run_summary("Build · gpt_x · 4m 12s · 31 tok/s".into())
        );
        let finished = draw(&state);
        let screen = finished.to_string();
        // The rail settles to grey and ends in `╰` at the totals, which follow the tail.
        assert_eq!(
            rail_at(&finished, "Done."),
            ("│".to_owned(), palette.rail())
        );
        assert_eq!(
            rail_at(&finished, "Build · gpt_x · 4m 12s · 31 tok/s"),
            ("╰".to_owned(), palette.rail())
        );
        assert_eq!(
            style_at_text(&finished, "Build · gpt_x").map(|style| style.0),
            Some(palette.muted),
            "{screen}"
        );
        // The earlier turn's gap before the next prompt has no rail.
        let earlier = state.transcript.entries()[1].id;
        let gap = TranscriptRow {
            anchor: RowAnchor {
                entry_id: earlier,
                position: RowPosition::Spacer,
            },
            role: TranscriptRole::Assistant,
            kind: TranscriptRowKind::Spacer,
            tone: TranscriptRowTone::Default,
            spans: Vec::new(),
            gutter: RowGutter::None,
            fold_hint: crate::transcript_view::FoldHint::None,
            fold_arrow: None,
            trailing: None,
            live_timer: false,
        };
        let prompts = TurnBounds {
            first: Some(0),
            latest: Some(2),
        };
        assert_eq!(turn_rail(&state, &gap, prompts, palette), None, "{screen}");
        let reply = TranscriptRow {
            kind: TranscriptRowKind::Content,
            anchor: RowAnchor {
                entry_id: earlier,
                position: RowPosition::Content(0),
            },
            ..gap
        };
        assert_eq!(
            turn_rail(&state, &reply, prompts, palette),
            Some(("│", palette.rail()))
        );
    }

    #[test]
    fn summary_closes_a_cancelled_turn() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("test".into());
        state
            .transcript
            .complete_message(1, "Running the tests.".into());
        state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "b1".into(),
                name: "bash".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"command": "cargo test"}),
            });
        // The turn ends on the card, which carries the totals.
        assert!(
            state
                .transcript
                .attach_run_summary("4.0s · 1 tool · cancelled".into())
        );
        let screen = draw_cards(&state, &mut TranscriptRowCache::default());
        let lines = screen.lines().collect::<Vec<_>>();
        let card = lines
            .iter()
            .position(|line| line.contains("Bash"))
            .expect("card row");
        let summary = lines
            .iter()
            .position(|line| line.contains("╰4.0s · 1 tool · cancelled"))
            .unwrap_or_else(|| panic!("{screen}"));
        assert!(summary > card, "{screen}");
    }

    #[test]
    fn denied_group_names_state() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("plan".into());
        observe_mcp_call(&mut state, "s1", "save_issue", "HY-1");
        finish_call(&mut state, "s1", "mcp__linear__save_issue");
        state
            .transcript
            .observe_approval_requested(crate::tool_cards::ToolCallInput {
                call_id: "s2".into(),
                name: "mcp__linear__save_issue".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"id": "HY-2"}),
            });
        state
            .transcript
            .observe_approval_resolved("s2", false, None);
        let screen = draw_cards(&state, &mut TranscriptRowCache::default());
        assert!(
            screen.contains("⊘  MCP     ▸ linear/save_issue ×2  id=HY-1 · id=HY-2 · denied"),
            "{screen}"
        );
    }

    #[test]
    fn expanded_mcp_group_lists_calls() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("plan".into());
        let mut first = None;
        for (call_id, id) in [("g1", "HY-1"), ("g2", "HY-2"), ("g3", "HY-3")] {
            let entry = observe_mcp_call(&mut state, call_id, "get_issue", id);
            first.get_or_insert(entry);
            finish_call(&mut state, call_id, "mcp__linear__get_issue");
        }
        let mut cache = TranscriptRowCache::default();
        assert!(draw_cards(&state, &mut cache).contains("×3"));
        cache.fold_mut().expand_group(first.unwrap());
        let screen = draw_cards(&state, &mut cache);
        assert!(screen.contains("×3"), "{screen}");
        for id in ["HY-1", "HY-2", "HY-3"] {
            assert!(
                screen.contains(&format!("✓  MCP     ▸ linear/get_issue  id={id}")),
                "{screen}"
            );
        }
    }

    #[test]
    fn stacked_cards_have_quiet_spacing() {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("check".into());
        for (call_id, command) in [("b1", "cargo fmt"), ("b2", "cargo test")] {
            state
                .transcript
                .observe_tool_call(crate::tool_cards::ToolCallInput {
                    call_id: call_id.into(),
                    name: "bash".into(),
                    detail_source: crate::tool_detail::ToolDetailSource::None,
                    arguments: json!({"command": command}),
                });
            let mut result = tool_result(call_id, "ok");
            result.name = "bash".into();
            state.transcript.observe_tool_result(result);
        }
        // `Process` fills the label width, so its chip borrows the gap's first space instead.
        state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "p1".into(),
                name: "bash".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"operation": "poll", "process_id": "process-1"}),
            });
        let mut poll = tool_result("p1", "");
        poll.name = "bash".into();
        poll.process_id = Some("process-1".into());
        poll.process_state = Some("running".into());
        state.transcript.observe_tool_result(poll);
        let mut cache = TranscriptRowCache::default();
        cache
            .fold_mut()
            .expand_group(state.transcript.entries()[1].id);
        let mut terminal = Terminal::new(TestBackend::new(40, 18)).unwrap();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    &state,
                    &mut TranscriptViewport::default(),
                    &mut cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                );
            })
            .unwrap();
        let screen = terminal.backend().to_string();
        let lines = screen.lines().collect::<Vec<_>>();
        let first = lines
            .iter()
            .position(|line| line.contains("cargo fmt"))
            .unwrap();
        // A plain row separates the prompt from the persistent group header.
        assert!(lines[first - 1].contains("▾ 2 commands"), "{screen}");
        assert!(
            lines[first - 2].trim_matches(['"', ' ']).is_empty(),
            "{screen}"
        );
        assert!(
            lines[first + 1].trim_matches(['"', ' ', '│']).is_empty(),
            "{screen}"
        );
        assert!(lines[first + 1].contains('│'), "{screen}");
        assert!(lines[first + 2].contains("Rust tests"), "{screen}");
        let palette = Palette::default();
        let user_bg = palette.user_text().bg;
        let gap_row = u16::try_from(first - 2).unwrap();
        assert_ne!(
            Some(terminal.backend().buffer()[(5, gap_row)].bg),
            user_bg,
            "{screen}"
        );
        let chip = palette.chip("Process");
        let chip_bg = chip.bg;
        let buffer = terminal.backend().buffer();
        let (x, y) = (0..buffer.area.height)
            .find_map(|y| {
                let row = (0..buffer.area.width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>();
                row.find("Process")
                    .map(|byte| (u16::try_from(row[..byte].chars().count()).unwrap(), y))
            })
            .expect("process card");
        let bg_at = |x: u16| Some(buffer[(x, y)].bg);
        assert_eq!(
            (bg_at(x - 1), bg_at(x), bg_at(x + 7)),
            (
                palette.chip_edge(chip, true).bg,
                chip_bg,
                palette.chip_edge(chip, false).bg,
            ),
            "{screen}"
        );
        assert_ne!(bg_at(x + 8), chip_bg, "{screen}");
        // A plain column keeps the status marker off the chip.
        assert_ne!(bg_at(x - 2), chip_bg, "{screen}");
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
                .contains("Read      README.md · awaiting approval")
        );
        let palette = Palette::default();
        let (pending_fg, pending_bg, pending_modifiers) =
            style_at_text(pending.backend(), "Read      README.md").unwrap();
        let chip = palette.chip("Read");
        assert_eq!((Some(pending_fg), Some(pending_bg)), (chip.fg, chip.bg));
        // Awaiting approval is marked by shape as well as colour.
        assert_eq!(
            style_at_text(pending.backend(), "?  Read").unwrap().0,
            Palette::default().primary
        );
        assert!(pending_modifiers.contains(Modifier::BOLD));
        // The note names the state outside the chip, in the row's muted text.
        assert_eq!(
            style_at_text(pending.backend(), "awaiting approval")
                .unwrap()
                .0,
            palette.muted
        );

        state
            .transcript
            .observe_approval_resolved("call-1", true, None);
        state
            .transcript
            .observe_tool_result(tool_result("call-1", "contents"));
        let complete = draw(&state);
        assert!(
            complete
                .backend()
                .to_string()
                .contains("Read    ▸ README.md")
        );
        assert!(!complete.backend().to_string().contains("contents"));
        let (success_fg, success_bg, success_modifiers) =
            style_at_text(complete.backend(), "Read    ▸ README.md").unwrap();
        assert_eq!((Some(success_fg), Some(success_bg)), (chip.fg, chip.bg));
        // The chip spans the marker's space, the name and one space; the alignment padding
        // after it stays plain, so stacked chips of different names differ in width.
        assert_eq!(
            Some(style_at_text(complete.backend(), " Read ").unwrap().1),
            palette.chip_edge(chip, true).bg
        );
        assert_ne!(
            Some(
                style_at_text(complete.backend(), "    ▸ README.md")
                    .unwrap()
                    .1
            ),
            chip.bg
        );
        // Only the card selected in browse shows how to expand it; every foldable
        // card shows its muted arrow.
        assert!(!complete.backend().to_string().contains("to expand"));
        assert_eq!(
            style_at_text(complete.backend(), "▸ README.md").unwrap().0,
            palette.muted
        );
        assert_eq!(
            style_at_text(complete.backend(), "✓  Read").unwrap().0,
            Palette::default().success
        );
        assert_eq!(
            style_at_text(complete.backend(), "README.md").unwrap().0,
            Palette::default().foreground
        );
        assert!(
            !style_at_text(complete.backend(), "README.md")
                .unwrap()
                .2
                .contains(Modifier::UNDERLINED)
        );
        assert!(success_modifiers.contains(Modifier::BOLD));

        let mut shell = UiState::unconfigured();
        shell.transcript.append_prompt("build".into());
        shell
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "shell-1".into(),
                name: "bash".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: json!({"command": "cargo test --workspace"}),
            });
        let mut shell_result = tool_result("shell-1", "ok");
        shell_result.name = "bash".into();
        shell.transcript.observe_tool_result(shell_result);
        let shell = draw(&shell);
        let command = style_at_text(shell.backend(), "Rust tests").unwrap();
        assert_eq!(command.0, Palette::default().secondary);
        assert!(!command.2.contains(Modifier::BOLD));
    }

    fn render_edit_card(arguments: serde_json::Value, width: u16) -> (String, TestBackend) {
        render_file_card("edit", arguments, None, false, width)
    }

    fn render_file_card(
        name: &str,
        arguments: serde_json::Value,
        before_text: Option<&str>,
        created: bool,
        width: u16,
    ) -> (String, TestBackend) {
        let mut state = UiState::unconfigured();
        state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "file-card".into(),
                name: name.into(),
                detail_source: crate::tool_detail::project_tool_detail_source(
                    name,
                    arguments.as_object().unwrap(),
                ),
                arguments: crate::tool_cards::bounded_tool_arguments(name, &arguments),
            });
        let mut completed = tool_result("file-card", "Done");
        completed.name = name.into();
        completed.before_text = before_text.map(str::to_owned);
        completed.created = created;
        state.transcript.observe_tool_result(completed);
        // 25 rows: a 24-row screen's transcript plus the composer's blank row.
        let mut terminal = Terminal::new(TestBackend::new(width, 25)).unwrap();
        terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    &state,
                    &mut TranscriptViewport::default(),
                    &mut TranscriptRowCache::default(),
                    &mut ComposerLayoutCache::default(),
                    &PromptEditor::default(),
                    &connection(),
                    None,
                    None,
                    true,
                    None,
                    0,
                    Palette::default(),
                    &Bindings::default(),
                    LogoChoice::AdalBraille,
                    None,
                    None,
                    None,
                );
            })
            .unwrap();
        let backend = terminal.backend().clone();
        (backend.to_string(), backend)
    }

    #[test]
    fn wide_edit_card_splits() {
        let arguments = json!({
            "path": "docs/configuration.md",
            "edits": [{
                "oldText": "## Provider diagnostics\n- Codex transport errors report the exception text.\nNo URLs.\n",
                "newText": "## Provider diagnostics\n- Codex transport diagnostics include exception type.\n- Add a read timeout setting.\nNo URLs.\n"
            }]
        });
        let (wide, backend) = render_file_card("edit", arguments.clone(), None, false, 200);
        // At 160 columns and wider, old and new share a row: the old line on the
        // left beside its replacement, then the added line beside a blank side.
        let row = wide
            .lines()
            .find(|line| line.contains("errors report the exception text"))
            .unwrap_or_else(|| panic!("{wide}"));
        assert!(row.contains("diagnostics include exception type"), "{wide}");
        assert!(!wide.contains("@@ edit"), "{wide}");
        let added = wide
            .lines()
            .find(|line| line.contains("Add a read timeout setting"))
            .unwrap();
        assert!(!added.contains('−'), "{wide}");
        let palette = Palette::default();
        assert_eq!(
            style_at_text(&backend, "- Codex transport").map(|style| style.1),
            Some(palette.deletion_background),
            "{wide}"
        );
        assert_eq!(
            style_at_text(&backend, "Add a read").map(|style| style.1),
            Some(palette.addition_background),
            "{wide}"
        );
        // Below the threshold the card stays unified: one side per row.
        let (normal, _) = render_file_card("edit", arguments, None, false, 159);
        let row = normal
            .lines()
            .find(|line| line.contains("errors report the exception text"))
            .unwrap_or_else(|| panic!("{normal}"));
        assert!(!row.contains("diagnostics include"), "{normal}");
        assert!(!normal.contains("@@ -1"), "{normal}");
    }

    #[test]
    fn edit_card_marks_rewrapped_words_and_separates_edits() {
        let (rendered, backend) = render_edit_card(
            json!({
                "path": "src/wisp/providers/google.py",
                "edits": [
                    {
                        "oldText": "retain portable text and calls, not the provider-native ``Part`` payload, so they\ncannot safely rebuild an active tool exchange as fresh context.",
                        "newText": "retain portable text and calls, not the provider-native ``Part`` payload,\nso they cannot safely rebuild an active tool exchange as fresh context."
                    },
                    {"oldText": "wait 30 seconds", "newText": "wait 60 seconds"}
                ]
            }),
            110,
        );
        assert!(
            rendered.contains("Edit      src/wisp/providers/google.py  +3 -3"),
            "{rendered}"
        );
        assert!(rendered.contains("− retain portable"), "{rendered}");
        assert!(rendered.contains("+ so they cannot"), "{rendered}");
        assert!(
            rendered.contains("⋯"),
            "the second edit is separated: {rendered}"
        );
        assert!(!rendered.contains("@@") && !rendered.contains("No newline"));
        let (_, _, moved) = style_at_text(&backend, "so they").unwrap();
        assert!(moved.contains(Modifier::UNDERLINED), "{moved:?}");
        let (_, _, unchanged) = style_at_text(&backend, "retain portable").unwrap();
        assert!(!unchanged.contains(Modifier::UNDERLINED));
        let (_, _, changed_number) = style_at_text(&backend, "30").unwrap();
        assert!(changed_number.contains(Modifier::UNDERLINED));
    }

    #[test]
    fn wrapped_deletion_aligns_continuation() {
        let (rendered, _) = render_edit_card(
            json!({
                "path": "notes.txt",
                "edits": [{"oldText": "alpha beta gamma delta epsilon zeta eta theta iota", "newText": "x"}]
            }),
            40,
        );
        let lines: Vec<&str> = rendered.lines().collect();
        let sign = lines
            .iter()
            .position(|line| line.contains("− alpha"))
            .unwrap_or_else(|| panic!("{rendered}"));
        let column = |line: &str, text: &str| line.find(text).map(|at| line[..at].chars().count());
        assert_eq!(
            column(lines[sign + 1], "↳ "),
            column(lines[sign], "− alpha"),
            "{rendered}"
        );
        assert!(!rendered.contains('↪'), "{rendered}");
    }

    #[test]
    fn monochrome_diff_emphasis_does_not_rely_on_colour() {
        let style = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                inline: InlineStyle::DiffChange,
                ..TranscriptSpanStyle::default()
            },
            crate::theme::resolve("glass").unwrap().palette(true),
        );
        assert!(
            style
                .add_modifier
                .contains(Modifier::UNDERLINED | Modifier::BOLD)
        );
    }

    #[test]
    fn write_card_numbers_lines_and_shows_a_changed_trailing_newline() {
        let (rendered, _) = render_file_card(
            "write",
            json!({"path": "notes.txt", "content": "a\nB\nc"}),
            Some("a\nb\nc\n"),
            false,
            100,
        );
        assert!(rendered.contains("   2      − b"), "{rendered}");
        assert!(rendered.contains("      2 + B"), "{rendered}");
        assert!(
            rendered.contains("⏎ trailing newline removed"),
            "{rendered}"
        );

        let (created, _) = render_file_card(
            "write",
            json!({"path": "new.txt", "content": "first\nsecond\n"}),
            None,
            true,
            100,
        );
        assert!(created.contains("new.txt  new file"), "{created}");
        assert!(created.contains("      1 + first"), "{created}");

        let (unterminated, _) = render_file_card(
            "write",
            json!({"path": "new.txt", "content": "first\nsecond"}),
            None,
            true,
            100,
        );
        assert!(unterminated.contains("      2 + second"), "{unterminated}");
        assert!(!unterminated.contains('⏎'), "{unterminated}");

        // Appending an unterminated line keeps the old final line as context.
        let (appended, _) = render_file_card(
            "write",
            json!({"path": "notes.txt", "content": "a\nb"}),
            Some("a\n"),
            false,
            100,
        );
        assert!(
            appended.contains("⏎ trailing newline removed"),
            "{appended}"
        );
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
        // Counts sit on the action line; the card has no `M path` heading or `@@` rows.
        assert!(
            collapsed.contains("Edit      file.txt  +1 -1"),
            "{collapsed}"
        );
        assert!(!collapsed.contains("M file.txt"));
        assert!(!collapsed.contains("@@"));
        assert!(!collapsed.contains("No newline"));
        assert!(collapsed.contains("− old�[2J"));
        assert!(collapsed.contains("+ new value"));
        // The browse hint lives once in the footer, not under each card.
        assert!(!collapsed.contains("Enter details"), "{collapsed}");
        assert!(collapsed.contains("Ctrl+G help · F6 browse"), "{collapsed}");
        assert!(collapsed.contains("Ctrl+G help"));
        assert!(!collapsed.contains('\u{1b}'));

        let backend = TestBackend::new(60, 20);
        let mut browse_terminal = Terminal::new(backend).unwrap();
        let mut browse_viewport = TranscriptViewport::default();
        let mut browse_cache = TranscriptRowCache::default();
        let mut browse_composer_cache = ComposerLayoutCache::default();
        browse_terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    &state,
                    &mut browse_viewport,
                    &mut browse_cache,
                    &mut browse_composer_cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                    Some(BrowseTarget::Entry(card_id)),
                    true,
                    None,
                    0,
                    Palette::default(),
                    &Bindings::default(),
                    LogoChoice::AdalBraille,
                    None,
                    None,
                    None,
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
        assert_eq!(selected_background, Palette::default().background);
        let (added_fg, added_background, _) =
            style_at_text(browse_terminal.backend(), "+ new value").unwrap();
        let (deleted_fg, deleted_background, _) =
            style_at_text(browse_terminal.backend(), "− old�[2J").unwrap();
        assert_eq!(added_fg, Palette::default().success);
        assert_eq!(added_background, Palette::default().addition_background);
        assert_eq!(deleted_fg, Palette::default().error);
        assert_eq!(deleted_background, Palette::default().deletion_background);

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
        let mut composer_layout_cache = ComposerLayoutCache::default();
        terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    &state,
                    &mut viewport,
                    &mut row_cache,
                    &mut composer_layout_cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                    Some(BrowseTarget::Entry(card_id)),
                    false,
                    None,
                    0,
                    Palette::default(),
                    &Bindings::default(),
                    LogoChoice::AdalBraille,
                    None,
                    None,
                    None,
                );
                let area = overlay_area(frame.area()).unwrap();
                clear_overlay(frame, area, Palette::default());
                render_detail_overlay(frame, area, &state, &mut detail_view, Palette::default());
            })
            .unwrap();
        let rendered = terminal.backend().to_string();
        assert!(rendered.contains("this tool edit"));
        assert!(rendered.contains("− old�[2J"));
        assert!(rendered.contains("+ new value"));
        let (added_fg, _, _) = style_at_text(terminal.backend(), "+ new value").unwrap();
        let (deleted_fg, _, _) = style_at_text(terminal.backend(), "− old�[2J").unwrap();
        assert_eq!(added_fg, Palette::default().success);
        assert_eq!(deleted_fg, Palette::default().error);
    }

    #[test]
    fn card_decorations_keep_text() {
        let span = |text: &str, inline| crate::markdown::TranscriptSpan {
            text: text.into(),
            affinity: crate::markdown::SourceAffinity::default(),
            style: TranscriptSpanStyle {
                inline,
                ..TranscriptSpanStyle::default()
            },
        };
        let row = |kind, spans, gutter, fold_hint| TranscriptRow {
            anchor: RowAnchor {
                entry_id: TranscriptEntryId::from_raw(1),
                position: RowPosition::Content(0),
            },
            role: TranscriptRole::Tool,
            kind,
            tone: TranscriptRowTone::Success,
            spans,
            gutter,
            fold_hint,
            fold_arrow: None,
            trailing: None,
            live_timer: false,
        };
        let action = row(
            TranscriptRowKind::CardAction,
            vec![
                span("✓  ", InlineStyle::ToolStatus),
                span("Read", InlineStyle::ToolName),
                span("  a.rs", InlineStyle::Normal),
            ],
            RowGutter::None,
            crate::transcript_view::FoldHint::Expand,
        );
        let detail = row(
            TranscriptRowKind::CardDetail,
            vec![span("out", InlineStyle::Normal)],
            RowGutter::Connector,
            crate::transcript_view::FoldHint::None,
        );
        for no_color in [false, true] {
            let palette = crate::theme::default_theme().palette(no_color);
            let mut terminal = Terminal::new(TestBackend::new(40, 3)).unwrap();
            terminal
                .draw(|frame| {
                    for (y, row) in [&action, &detail].into_iter().enumerate() {
                        let area = Rect::new(0, y as u16, 40, 1);
                        render_transcript_row(
                            frame,
                            area,
                            row.clone(),
                            None,
                            false,
                            palette,
                            None,
                            None,
                            None,
                        );
                    }
                    let selected = Some(action.anchor);
                    let area = Rect::new(0, 2, 40, 1);
                    render_transcript_row(
                        frame,
                        area,
                        action.clone(),
                        selected,
                        false,
                        palette,
                        None,
                        None,
                        None,
                    );
                })
                .unwrap();
            let screen = terminal.backend().to_string();
            // The hint appears only on the selected copy of the action row.
            assert_eq!(screen.matches("✓  Read  a.rs").count(), 2, "{screen}");
            assert_eq!(screen.matches("(→ to expand)").count(), 1, "{screen}");
            assert!(screen.contains("└ out"), "{screen}");

            let (fg, bg, modifiers) = style_at_text(terminal.backend(), "Read ").unwrap();
            let chip = palette.chip("Read");
            if no_color {
                assert!(modifiers.contains(Modifier::REVERSED));
            } else {
                assert_eq!((Some(fg), Some(bg)), (chip.fg, chip.bg));
            }
            // The chip ends with the gap's first space; the second stays plain.
            let (_, gap_bg, gap_modifiers) = style_at_text(terminal.backend(), " a.rs").unwrap();
            assert!(!gap_modifiers.contains(Modifier::REVERSED));
            assert_ne!(Some(gap_bg), chip.bg);
            assert_eq!(
                style_at_text(terminal.backend(), "└").unwrap().0,
                palette.muted
            );
        }
    }

    #[test]
    fn selected_row_keeps_styles() {
        for no_color in [false, true] {
            let palette = crate::theme::default_theme().palette(no_color);
            let mut terminal = Terminal::new(TestBackend::new(30, 2)).unwrap();
            let anchor = RowAnchor {
                entry_id: TranscriptEntryId::from_raw(1),
                position: crate::transcript_view::RowPosition::Content(0),
            };
            let row = TranscriptRow {
                anchor,
                role: TranscriptRole::Tool,
                kind: TranscriptRowKind::CardDetail,
                tone: TranscriptRowTone::Default,
                spans: vec![crate::markdown::TranscriptSpan {
                    text: "selected code".into(),
                    affinity: crate::markdown::SourceAffinity::default(),
                    style: TranscriptSpanStyle {
                        block: BlockStyle::Code,
                        inline: InlineStyle::Code,
                        syntax: SyntaxClass::Keyword,
                        emphasis: true,
                        ..TranscriptSpanStyle::default()
                    },
                }],
                gutter: RowGutter::None,
                fold_hint: crate::transcript_view::FoldHint::None,
                fold_arrow: None,
                trailing: None,
                live_timer: false,
            };
            // Browse marks a selection only with a fold hint; this row has none, so the
            // selected and unselected renders match cell for cell.
            terminal
                .draw(|frame| {
                    for (y, selected) in [None, Some(anchor)].into_iter().enumerate() {
                        let area = Rect::new(0, y as u16, 30, 1);
                        render_transcript_row(
                            frame,
                            area,
                            row.clone(),
                            selected,
                            false,
                            palette,
                            None,
                            None,
                            None,
                        );
                    }
                })
                .unwrap();
            let buffer = terminal.backend().buffer();
            for x in 0..30 {
                assert_eq!(buffer[(x, 0)], buffer[(x, 1)], "column {x}");
            }
            assert!(
                buffer[(1, 0)]
                    .modifier
                    .contains(Modifier::BOLD | Modifier::ITALIC)
            );
        }
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
    fn reported_tool_table_keeps_borders_and_tail_on_screen() {
        let mut state = UiState::new("fake".into(), None, None);
        state
            .transcript
            .append_exchange("Are these sufficient built-in tools?".into());
        state.transcript.complete_message(1, concat!(
            "| read | Reads text files, optionally selecting a range of lines. |\n",
            "| write | Creates or overwrites text files, creating parent directories when needed. |\n",
            "| edit | Makes precise, exact-text replacements without rewriting the whole file. |\n",
            "| ls | Lists files and folders in a directory. |\n",
            "| find | Finds files using patterns, such as *.py. |\n",
            "| grep | Searches file contents for references to a function. |\n",
            "| bash | Runs terminal commands: tests, builds, Git, scripts, etc. It can also start long-running commands, retrieve their output, and cancel them. |\n",
            "| skill | Loads specialized instructions and supporting resources, such as Wisp development or GitHub workflows. |\n",
            "Additional tools come through MCP integrations."
        ).into());
        for (width, height) in [(100, 40), (60, 20)] {
            let rendered = render_to_string(width, height, &state, &PromptEditor::default());
            assert!(rendered.contains("└"), "{rendered}");
            assert!(rendered.contains("│ skill │"), "{rendered}");
            assert!(rendered.contains("MCP integrations."), "{rendered}");
        }
    }

    #[test]
    fn streamed_and_historical_assistant_events_render_markdown() {
        let chunks = [
            "## Result\n\nUse **bold** and `code`.\n\n",
            "| Item | Count |\n| --- | ---: |\n",
            "| short | 1 |\n",
            "| longer name | 12 |\n\n",
            "- [x] Done\n- [ ] Pending\n",
        ];
        let source = chunks.concat();
        let mut state = UiState::new("fake".into(), None, None);
        state.transcript.append_exchange("show result".into());
        let mut ids = TestIds::default();
        let mut terminal = Terminal::new(TestBackend::new(80, 32)).unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        for chunk in chunks {
            let event = wisp_protocol::events::deserialize(json!({
                "type": "message.delta", "turn": 1, "delta": chunk,
                "timestamp": "2026-09-13T00:00:00Z",
                "role": "assistant", "content_index": 0,
                "content_kind": "text"
            }))
            .unwrap();
            reduce(
                &mut state,
                crate::reducer::UiAction::BackendEvent(BackendEvent::from_live(&event).unwrap()),
                &mut ids,
            )
            .unwrap();
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        &state,
                        &mut viewport,
                        &mut cache,
                        &PromptEditor::default(),
                        &connection(),
                        None,
                    )
                })
                .unwrap();
        }
        let streamed = terminal.backend().to_string();
        assert!(streamed.contains("Use bold and code."));
        assert!(streamed.contains("Item        │ Count"));
        assert!(streamed.contains("longer name │    12"));
        assert!(streamed.contains("☑ Done"));
        assert!(streamed.contains("☐ Pending"));
        let (_, _, heading) = style_at_text(terminal.backend(), "Result").unwrap();
        let (_, _, bold) = style_at_text(terminal.backend(), "bold").unwrap();
        assert!(heading.contains(Modifier::BOLD));
        assert!(bold.contains(Modifier::BOLD));

        let completed = wisp_protocol::events::deserialize(json!({
            "type": "message.completed", "message_entry_id": null, "turn": 1, "content": source,
            "timestamp": "2026-09-13T00:00:00Z",
            "role": "assistant", "tool_calls": [], "usage": null,
            "finish_reason": "stop", "response_id": null, "cost": null,
            "context_observation": null
        }))
        .unwrap();
        reduce(
            &mut state,
            crate::reducer::UiAction::BackendEvent(BackendEvent::from_live(&completed).unwrap()),
            &mut ids,
        )
        .unwrap();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    &state,
                    &mut viewport,
                    &mut cache,
                    &PromptEditor::default(),
                    &connection(),
                    None,
                )
            })
            .unwrap();
        assert_eq!(
            terminal.backend().to_string(),
            render_to_string(80, 32, &state, &PromptEditor::default())
        );

        let history = BackendEvent::from_projection_value(&json!({
            "type": "rpc.messages", "command_id": "history-1",
            "session_id": null, "session_path": null, "active_leaf_id": null,
            "truncated": false, "next_before_entry_id": null, "next_after_entry_id": null,
            "messages": [{
                "entry_id": "answer", "role": "assistant", "content": source,
                "content_truncated": false, "tool_calls": []
            }]
        }))
        .unwrap();
        let BackendEvent::MessagesReported { messages, .. } = history else {
            panic!("history event expected");
        };
        state.transcript = messages.transcript;
        for width in [80, 38] {
            let rendered = render_to_string(width, 32, &state, &PromptEditor::default());
            assert!(rendered.contains("Use bold and code."));
            assert!(rendered.contains("longer name │    12"));
            assert!(rendered.contains("☑ Done"));
            assert!(!rendered.contains("**bold**"));
            assert!(!rendered.contains("| --- |"));
        }
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
        assert_eq!(code.fg, Some(palette.foreground));
        assert_eq!(code.bg, Some(palette.panel));

        let link = markdown_span_style(
            Style::default(),
            TranscriptSpanStyle {
                inline: InlineStyle::Link,
                ..TranscriptSpanStyle::default()
            },
            palette,
        );
        assert!(link.add_modifier.contains(Modifier::UNDERLINED));
        assert_eq!(link.fg, Some(palette.secondary));
        // A theme whose secondary matches its body text keeps links in the accent.
        let mut flat = palette;
        flat.secondary = palette.foreground;
        assert_eq!(flat.link(), flat.primary);

        let tool_command = markdown_span_style(
            Style::default().fg(palette.muted),
            TranscriptSpanStyle {
                inline: InlineStyle::ToolCommand,
                ..TranscriptSpanStyle::default()
            },
            palette,
        );
        assert_eq!(tool_command.fg, Some(palette.secondary));
        assert!(!tool_command.add_modifier.contains(Modifier::BOLD));

        let tool_path = markdown_span_style(
            Style::default().fg(palette.muted),
            TranscriptSpanStyle {
                inline: InlineStyle::ToolPath,
                ..TranscriptSpanStyle::default()
            },
            palette,
        );
        assert_eq!(tool_path.fg, Some(palette.foreground));
        assert!(!tool_path.add_modifier.contains(Modifier::UNDERLINED));

        let tool_argument = markdown_span_style(
            Style::default().fg(palette.muted),
            TranscriptSpanStyle {
                inline: InlineStyle::ToolArgument,
                ..TranscriptSpanStyle::default()
            },
            palette,
        );
        assert_eq!(tool_argument.fg, Some(palette.secondary));

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
    fn transcript_live_view_only_renders_the_current_retained_turn() {
        let mut state = UiState::unconfigured();
        state.transcript.append_exchange("first prompt".into());
        state.transcript.complete_message(1, "first answer".into());
        state.transcript.append_exchange("second prompt".into());
        state.transcript.complete_message(2, "second answer".into());

        let rendered = render_to_string(80, 24, &state, &PromptEditor::default());

        assert!(!rendered.contains("first prompt"));
        assert!(!rendered.contains("first answer"));
        assert!(rendered.contains("second prompt"));
        assert!(rendered.contains("second answer"));
        assert_eq!(state.transcript.entries().len(), 4);
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
    fn render_bash_card(
        command: &str,
        result: Option<(bool, Option<&str>)>,
        width: u16,
        expanded: bool,
    ) -> String {
        let mut state = UiState::unconfigured();
        state.transcript.append_prompt("run it".into());
        let card = state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "bash-card".into(),
                name: "bash".into(),
                arguments: json!({"command": command}),
                detail_source: crate::tool_detail::ToolDetailSource::None,
            });
        if let Some((is_error, process_state)) = result {
            let mut completed = tool_result("bash-card", "done");
            completed.name = "bash".into();
            completed.is_error = is_error;
            completed.process_state = process_state.map(str::to_owned);
            state.transcript.observe_tool_result(completed);
        }
        let mut cache = TranscriptRowCache::default();
        if expanded {
            cache.fold_mut().expand(card);
        }
        let mut terminal = Terminal::new(TestBackend::new(width, 18)).unwrap();
        terminal
            .draw(|frame| {
                render_interactive(
                    frame,
                    &state,
                    &mut TranscriptViewport::default(),
                    &mut cache,
                    &mut ComposerLayoutCache::default(),
                    &PromptEditor::default(),
                    &connection(),
                    None,
                    None,
                    true,
                    None,
                    0,
                    Palette::default(),
                    &Bindings::default(),
                    LogoChoice::AdalBraille,
                    None,
                    None,
                    None,
                );
            })
            .unwrap();
        terminal.backend().to_string()
    }

    #[test]
    fn long_actions_stay_on_one_row_until_the_card_is_expanded() {
        let command =
            "cargo test --workspace --all-features -- --nocapture tests::a_very_long_test_name";
        let collapsed = render_bash_card(command, Some((false, None)), 40, false);
        let action = collapsed
            .lines()
            .find(|line| line.contains("✓  Bash"))
            .expect("action row");
        assert!(action.contains("Rust tests"), "{collapsed}");
        assert!(!collapsed.contains("a_very_long_test_name"), "{collapsed}");

        let expanded = render_bash_card(command, Some((false, None)), 40, true);
        let action = expanded
            .lines()
            .find(|line| line.contains("✓  Bash"))
            .expect("action row");
        assert!(!action.contains('…'), "{expanded}");
        assert!(expanded.contains("ong_test_name"), "{expanded}");

        // A diff's change counts stay visible when its path is cut.
        let (diff, _) = render_file_card(
            "edit",
            json!({
                "path": "src/wisp/providers/a/deeply/nested/module/with/a/long/name.py",
                "edits": [{"oldText": "a\n", "newText": "b\n"}]
            }),
            None,
            false,
            40,
        );
        let diff_action = diff
            .lines()
            .find(|line| line.contains("Edit  "))
            .expect("diff action row");
        assert!(diff_action.contains('…'), "{diff}");
        assert!(diff_action.contains("+1 -1"), "{diff}");
    }

    #[test]
    fn each_tool_state_renders_its_marker_at_thirty_columns() {
        for (result, marker) in [
            (None, "●"),
            (Some((false, None)), "✓"),
            (Some((true, None)), "✗"),
            (Some((false, Some("cancelled"))), "⊘"),
        ] {
            let rendered = render_bash_card("echo a long enough command to cut", result, 30, false);
            let action = rendered
                .lines()
                .find(|line| line.contains(marker))
                .unwrap_or_else(|| panic!("{marker}: {rendered}"));
            assert!(action.contains('…'), "{rendered}");
            if marker == "⊘" {
                // The status note is kept whole; the command is cut instead.
                assert!(action.contains("… · cancelled"), "{rendered}");
            }
        }
    }

    #[test]
    fn footer_offers_browse_only_when_there_is_something_to_browse() {
        let mut state = UiState::unconfigured();
        assert!(!footer_hints(&state, &Bindings::default(), 120).contains("browse"));
        state.transcript.append_prompt("hello".into());
        assert!(footer_hints(&state, &Bindings::default(), 120).contains("F6 browse"));
        // It survives the 56-column cap that narrower terminals apply.
        assert!(footer_hints(&state, &Bindings::default(), 56).contains("F6 browse"));

        // A prompt run with input ready (steering) offers it too, since cards arrive then.
        let mut running = UiState::new("fake".into(), None, None);
        running.view_status = ViewStatus::Running;
        running.interaction_status = crate::reducer::InteractionStatus::Running;
        running.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        running.transcript.append_prompt("hello".into());
        assert!(running.active_prompt_editable());
        let hints = footer_hints(&running, &Bindings::default(), 200);
        assert_eq!(hints, "Esc interrupt · F6 browse");
    }

    #[test]
    fn running_footer_orders_panel_hint() {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.transcript.append_prompt("hello".into());
        let footer = |width| {
            render_to_string(width, 18, &state, &PromptEditor::default())
                .lines()
                .last()
                .unwrap()
                .to_owned()
        };
        let wide = footer(140);
        assert!(
            wide.contains("Esc interrupt · Ctrl+B panel · F6 browse"),
            "{wide}"
        );
        // A narrow row drops the panel hint before the others.
        let narrow = footer(40);
        assert!(narrow.contains("Esc interrupt · F6 browse"), "{narrow}");
        assert!(!narrow.contains("panel"), "{narrow}");
    }

    #[test]
    fn composer_effort_is_bold_warning() {
        let mut state = UiState::new("fake".into(), Some("model-x".into()), None);
        state.effort = Some("high".into());
        let line = composer_identity_line(&state, Palette::default());
        let effort = line
            .spans
            .iter()
            .find(|span| span.content == "high")
            .unwrap();
        assert_eq!(effort.style.fg, Some(Palette::default().warning));
        assert!(effort.style.add_modifier.contains(Modifier::BOLD));
    }

    #[test]
    fn status_markers_are_distinct_single_column_shapes() {
        use crate::tool_cards::ToolStatus;
        let marker = |status| crate::transcript_view::status_marker(status).trim_end();
        for status in [
            ToolStatus::Requested,
            ToolStatus::AwaitingApproval,
            ToolStatus::Running,
            ToolStatus::Done,
            ToolStatus::Error,
            ToolStatus::Denied,
            ToolStatus::Cancelled,
        ] {
            // A double-width or emoji glyph would shift the whole action line.
            assert_eq!(marker(status).width(), 1, "{status:?}");
        }
        for frame in SPINNER_FRAMES {
            assert_eq!(frame.width(), 1);
        }
        let outcomes = [
            marker(ToolStatus::Done),
            marker(ToolStatus::Error),
            marker(ToolStatus::Denied),
            marker(ToolStatus::AwaitingApproval),
            marker(ToolStatus::Running),
        ];
        let distinct: std::collections::HashSet<_> = outcomes.iter().collect();
        assert_eq!(distinct.len(), outcomes.len());
        assert_eq!(marker(ToolStatus::Denied), marker(ToolStatus::Cancelled));
    }

    #[test]
    fn only_active_tool_markers_spin_and_terminal_results_stay_steady() {
        use crate::tool_cards::ToolCallInput;
        let draw = |state: &UiState, tick, palette, selected| {
            let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
            let mut viewport = TranscriptViewport::default();
            let mut cache = TranscriptRowCache::default();
            let mut composer_layout_cache = ComposerLayoutCache::default();
            terminal
                .draw(|frame| {
                    render_interactive(
                        frame,
                        state,
                        &mut viewport,
                        &mut cache,
                        &mut composer_layout_cache,
                        &PromptEditor::default(),
                        &connection(),
                        None,
                        selected,
                        true,
                        None,
                        tick,
                        palette,
                        &Bindings::default(),
                        LogoChoice::AdalBraille,
                        None,
                        None,
                        None,
                    );
                })
                .unwrap();
            terminal.backend().buffer().clone()
        };
        // Spinner cells that turn between two ticks. Live timers read the wall clock and
        // may also tick, but nothing else may change.
        let spinning = |first: &ratatui::buffer::Buffer, later: &ratatui::buffer::Buffer| {
            let spinner = |cell: &ratatui::buffer::Cell| SPINNER_FRAMES.contains(&cell.symbol());
            let timer = |cell: &ratatui::buffer::Cell| {
                cell.symbol()
                    .chars()
                    .all(|character| character.is_ascii_digit() || "<.ms ".contains(character))
            };
            let changed = first
                .content
                .iter()
                .zip(&later.content)
                .filter(|(a, b)| a != b)
                .collect::<Vec<_>>();
            assert!(
                changed
                    .iter()
                    .all(|(a, b)| (spinner(a) && spinner(b)) || (timer(a) && timer(b)))
            );
            changed
                .iter()
                .filter(|(a, b)| spinner(a) && spinner(b))
                .count()
        };
        for palette in [Palette::default(), crate::theme::themes()[0].palette(true)] {
            for tool in ["bash", "read"] {
                for outcome in ["success", "error", "denied", "cancelled"] {
                    let mut state = UiState::new("fake".into(), None, None);
                    state.view_status = ViewStatus::Running;
                    state.interaction_status = crate::reducer::InteractionStatus::Running;
                    state.transcript.append_prompt("run a check".into());
                    if tool == "read" {
                        state.transcript.observe_tool_call(ToolCallInput {
                            call_id: "earlier".into(),
                            name: "read".into(),
                            arguments: json!({"path": "earlier.txt"}),
                            detail_source: crate::tool_detail::ToolDetailSource::None,
                        });
                        state
                            .transcript
                            .observe_tool_result(tool_result("earlier", "finished"));
                    }
                    let call = ToolCallInput {
                        call_id: "pulse".into(),
                        name: tool.into(),
                        arguments: json!({"command": "cargo test"}),
                        detail_source: crate::tool_detail::ToolDetailSource::None,
                    };
                    // The card's marker and the status line both turn while the call runs.
                    state.transcript.observe_tool_call(call.clone());
                    assert_eq!(
                        spinning(
                            &draw(&state, 0, palette, None),
                            &draw(&state, 1, palette, None)
                        ),
                        2
                    );
                    // Awaiting approval, the card's `?` holds still.
                    state.transcript.observe_approval_requested(call);
                    assert_eq!(
                        spinning(
                            &draw(&state, 0, palette, None),
                            &draw(&state, 1, palette, None)
                        ),
                        1
                    );
                    if outcome == "denied" {
                        state
                            .transcript
                            .observe_approval_resolved("pulse", false, None);
                    } else {
                        state
                            .transcript
                            .observe_approval_resolved("pulse", true, None);
                        let first = draw(&state, 0, palette, None);
                        let later = draw(&state, 1, palette, None);
                        let selected = state
                            .transcript
                            .entries()
                            .iter()
                            .find(|entry| entry.tool_card().is_some())
                            .map(|entry| BrowseTarget::Entry(entry.id));
                        assert_eq!(
                            spinning(
                                &draw(&state, 0, palette, selected),
                                &draw(&state, 1, palette, selected)
                            ),
                            2
                        );
                        // Only the markers change between ticks, and they change shape,
                        // so the activity also reads in monochrome.
                        assert_eq!(spinning(&first, &later), 2, "{outcome}");
                        let mut result = tool_result("pulse", "finished");
                        result.is_error = outcome == "error";
                        if outcome == "cancelled" {
                            result.process_state = Some("cancelled".into());
                        }
                        state.transcript.observe_tool_result(result);
                    }
                    // Once the call settles, only the status line keeps turning.
                    let settled = draw(&state, 0, palette, None);
                    assert_eq!(
                        spinning(&settled, &draw(&state, 1, palette, None)),
                        1,
                        "{outcome}"
                    );
                    let marker = match outcome {
                        "success" => "✓",
                        "error" => "✗",
                        _ => "⊘",
                    };
                    assert!(
                        settled.content.iter().any(|cell| cell.symbol() == marker),
                        "{outcome}"
                    );
                }
            }
        }
    }

    #[test]
    fn status_line_follows_the_run() {
        let status = |rendered: &str| {
            // The status line is the last spinning row; running cards spin above it.
            rendered
                .lines()
                .rev()
                .find(|line| SPINNER_FRAMES.iter().any(|frame| line.contains(frame)))
                .unwrap_or_else(|| panic!("status line: {rendered}"))
                .to_owned()
        };
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::Running;
        state.interaction_status = crate::reducer::InteractionStatus::Running;
        state.transcript.append_prompt("check this".into());
        let waiting = render_to_string_with_activity(80, 18, &state, 0);
        assert!(status(&waiting).contains("◐ Working"), "{waiting}");
        state
            .transcript
            .append_message_delta(1, "I will check that.");
        state
            .transcript
            .complete_message(1, "I will check that.".into());
        state.transcript.append_thinking_delta(2, "Next step");
        let thinking = render_to_string_with_activity(80, 18, &state, 1);
        assert!(status(&thinking).contains("◓ Thinking"), "{thinking}");
        for index in 0..20 {
            state
                .transcript
                .observe_tool_call(crate::tool_cards::ToolCallInput {
                    call_id: format!("tool-{index}"),
                    name: "bash".into(),
                    arguments: json!({"command": format!("check {index}")}),
                    detail_source: crate::tool_detail::ToolDetailSource::None,
                });
        }
        // The latest call in progress names the run, at every size.
        for (width, height) in [(80, 18), (30, 8)] {
            let running = render_to_string_with_activity(width, height, &state, 1);
            let line = status(&running);
            // A narrow row clips the command.
            assert!(line.contains(" Bash  c"), "{running}");
            assert!(width < 80 || line.contains(" Bash  check 19"), "{running}");
            assert!(!line.contains("Working"), "{running}");
        }
        for index in 0..20 {
            state
                .transcript
                .observe_tool_result(tool_result(&format!("tool-{index}"), "done"));
        }
        let between_steps = render_to_string_with_activity(80, 18, &state, 2);
        assert!(
            status(&between_steps).contains("◑ Working"),
            "{between_steps}"
        );
        assert!(
            status(&between_steps).contains("20 tools"),
            "{between_steps}"
        );

        state
            .transcript
            .observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: "process-poll".into(),
                name: "bash".into(),
                arguments: json!({"operation": "poll", "process_id": "process-1"}),
                detail_source: crate::tool_detail::ToolDetailSource::None,
            });
        let mut process_result = tool_result("process-poll", "still running");
        process_result.name = "bash".into();
        process_result.process_id = Some("process-1".into());
        process_result.process_state = Some("running".into());
        state.transcript.observe_tool_result(process_result);
        let process = render_to_string_with_activity(80, 18, &state, 3);
        assert!(
            status(&process).contains(" Process  Background command"),
            "{process}"
        );
        // Once the model answers again, the background process no longer names the run.
        state.transcript.append_message_delta(3, "Checking again.");
        let answering = render_to_string_with_activity(80, 18, &state, 3);
        assert!(status(&answering).contains("◒ Working"), "{answering}");

        for status in [
            ViewStatus::Idle,
            ViewStatus::Error,
            ViewStatus::WaitingForApproval,
            ViewStatus::WaitingForTrust,
        ] {
            state.view_status = status;
            state.interaction_status = crate::reducer::InteractionStatus::Idle;
            let done = render_to_string_with_activity(80, 18, &state, 1);
            assert!(!done.contains("◓"), "{done}");
        }
    }
}
