//! The side panel: what the session is, what it costs, and what it changed, beside
//! the transcript on a wide terminal or over it as an overlay opened with Ctrl+B.
//!
//! The panel only reads state. The reducer keeps its data loaded while it is on
//! screen (see `UiAction::SetSidePanelVisible`); this module decides where it goes
//! and draws it.

use std::sync::Arc;
use std::time::Duration;

use crate::keybindings::{Action as KeyAction, Bindings};
use crate::reducer::git_diff::{GitDiff, GitDiffSnapshot};
use crate::reducer::{UiState, ViewStatus};
use crate::theme::Palette;
use crate::transcript::{RunOutcome, TranscriptRole};
use crate::ui::{self, sanitize_for_terminal};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Padding, Paragraph};
use unicode_width::UnicodeWidthStr;
use wisp_protocol::commands::PermissionMode;
use wisp_protocol::events::McpServerStatus;

/// The panel's width, its border included.
pub(crate) const PANEL_WIDTH: u16 = 56;

/// What the transcript keeps beside a docked panel: replies wrap at 100 columns,
/// plus the turn rail and the conversation margins.
const MIN_TRANSCRIPT_WIDTH: u16 = 104;

/// Changed files listed before the rest are counted on one line.
const MAX_FILE_ROWS: usize = 8;

const SPINNER_OUTCOME: &str = "…";

/// Whether a terminal this wide docks the panel beside the transcript.
pub(crate) fn docks(width: u16) -> bool {
    width >= PANEL_WIDTH + MIN_TRANSCRIPT_WIDTH
}

/// Where the panel is and whether the reducer knows it is on screen.
#[derive(Debug, Default)]
pub(crate) struct SidePanel {
    /// Hidden with Ctrl+B on a terminal wide enough to dock it.
    hidden: bool,
    /// Opened with Ctrl+B on a terminal too narrow to dock it.
    overlay: bool,
    /// The width of the last frame, which decides whether the panel docks.
    width: u16,
    /// The visibility last reported to the reducer.
    reported_visible: bool,
    files: Option<FileSummary>,
}

impl SidePanel {
    pub(crate) fn docked(&self) -> bool {
        !self.hidden && docks(self.width)
    }

    pub(crate) fn overlay_open(&self) -> bool {
        self.overlay && !docks(self.width)
    }

    fn visible(&self) -> bool {
        self.docked() || self.overlay_open()
    }

    /// Ctrl+B: hide or show the docked panel, or open or close the overlay.
    pub(crate) fn toggle(&mut self) {
        if docks(self.width) {
            self.hidden = !self.hidden;
        } else {
            self.overlay = !self.overlay;
        }
    }

    /// Whether Ctrl+B would open or close the overlay rather than the docked panel.
    pub(crate) fn toggles_overlay(&self) -> bool {
        !docks(self.width)
    }

    pub(crate) fn close_overlay(&mut self) {
        self.overlay = false;
    }

    /// Follow the terminal's width. An overlay open when the terminal grows wide
    /// enough becomes the docked panel.
    pub(crate) fn observe_width(&mut self, width: u16) {
        if self.overlay && docks(width) {
            self.overlay = false;
            self.hidden = false;
        }
        self.width = width;
    }

    /// The visibility to report to the reducer, once, after it changes.
    pub(crate) fn take_visibility_change(&mut self) -> Option<bool> {
        let visible = self.visible();
        (visible != self.reported_visible).then(|| {
            self.reported_visible = visible;
            visible
        })
    }

    /// Count the changed files' lines once per snapshot. While a new snapshot loads,
    /// the last counts stay on screen rather than blinking out after every run.
    pub(crate) fn sync_files(&mut self, files: &GitDiff) {
        let Some(snapshot) = &files.snapshot else {
            // A failed read replaces the counts with its error.
            if files.error.is_some() {
                self.files = None;
            }
            return;
        };
        if self
            .files
            .as_ref()
            .is_none_or(|summary| !Arc::ptr_eq(&summary.snapshot, snapshot))
        {
            self.files = Some(FileSummary::new(snapshot.clone()));
        }
    }
}

/// The panel's area on a terminal too narrow to dock it: the right-hand side of the
/// conversation, above the composer, so the prompt stays in view.
pub(crate) fn overlay_area(above_composer: Rect) -> Rect {
    let width = PANEL_WIDTH.min(above_composer.width);
    Rect {
        x: above_composer.right() - width,
        width,
        ..above_composer
    }
}

/// Split a frame into the conversation and the docked panel at its right.
pub(crate) fn split(area: Rect) -> (Rect, Rect) {
    let width = PANEL_WIDTH.min(area.width);
    (
        Rect {
            width: area.width - width,
            ..area
        },
        Rect {
            x: area.right() - width,
            width,
            ..area
        },
    )
}

#[derive(Debug)]
struct FileSummary {
    snapshot: Arc<GitDiffSnapshot>,
    /// The listed files only: the rest are counted, not diffed, so a large
    /// working tree cannot stall a frame.
    rows: Vec<FileRow>,
}

#[derive(Debug)]
struct FileRow {
    status: char,
    path: String,
    additions: usize,
    deletions: usize,
    /// Whether line counts are known: an untracked file, or one Git could not read,
    /// shows none.
    counted: bool,
}

impl FileSummary {
    fn new(snapshot: Arc<GitDiffSnapshot>) -> Self {
        let rows = snapshot
            .files
            .iter()
            .take(MAX_FILE_ROWS)
            .map(|file| {
                let status = match file.status.as_str() {
                    "untracked" => '?',
                    "added" => 'A',
                    "deleted" => 'D',
                    _ => 'M',
                };
                let counted = status != '?' && file.notice.is_empty();
                let (additions, deletions) = if counted {
                    line_counts(&file.before, &file.after)
                } else {
                    (0, 0)
                };
                FileRow {
                    status,
                    path: sanitize_for_terminal(&file.path),
                    additions,
                    deletions,
                    counted,
                }
            })
            .collect();
        Self { snapshot, rows }
    }
}

fn line_counts(before: &str, after: &str) -> (usize, usize) {
    let diff = similar::TextDiff::from_lines(before, after);
    diff.iter_all_changes()
        .fold((0, 0), |(added, deleted), change| match change.tag() {
            similar::ChangeTag::Insert => (added + 1, deleted),
            similar::ChangeTag::Delete => (added, deleted + 1),
            similar::ChangeTag::Equal => (added, deleted),
        })
}

/// What leads the panel: the session at idle, the run while one is going, or the
/// decision it waits on.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Focus {
    Idle,
    Running,
    Approval,
}

impl Focus {
    fn of(state: &UiState) -> Self {
        if state.pending_approval.is_some() {
            Self::Approval
        } else if ui::run_in_status_line(state) {
            Self::Running
        } else {
            // A project trust prompt keeps the idle layout: it comes before any run,
            // and the header's status label says what it waits on.
            Self::Idle
        }
    }
}

/// Draw the panel into `area`. A docked panel has a rule on its left; an overlay is
/// boxed so it reads as lying over the transcript.
///
/// The sections follow what Wisp is doing. At idle: the session, context, model,
/// changed files, turns and MCP. While a run goes, `Now` and the message queue come
/// first and the turn list folds to one row; while it waits for approval, the
/// decision and the held queue come first.
#[allow(clippy::too_many_arguments)]
pub(crate) fn render(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    panel: &SidePanel,
    bindings: &Bindings,
    activity_frame: u8,
    run_elapsed: Option<Duration>,
    palette: Palette,
) {
    let overlay = !panel.docked();
    let block = if overlay {
        Block::default()
            .borders(Borders::ALL)
            .border_style(palette.border())
            .padding(Padding::horizontal(1))
    } else {
        Block::default()
            .borders(Borders::LEFT)
            .border_style(Style::default().fg(palette.rail()))
            .padding(Padding::new(2, 1, 1, 0))
    };
    let inner = block.inner(area);
    frame.render_widget(block.style(palette.overlay()), area);
    let width = usize::from(inner.width);
    let height = usize::from(inner.height);
    if width == 0 || height == 0 {
        return;
    }

    let toggle_key = ui::primary_label(bindings, KeyAction::TogglePanel);
    let hint = if overlay {
        format!("{toggle_key} close · Esc close")
    } else {
        format!("{toggle_key} hide panel")
    };
    let focus = Focus::of(state);
    let mut sections = vec![header_lines(state, width, palette)];
    match focus {
        Focus::Idle => {}
        Focus::Running => {
            sections.push(now_lines(
                state,
                activity_frame,
                run_elapsed,
                width,
                palette,
            ));
            sections.extend(queue_lines(state, false, bindings, width, palette));
        }
        Focus::Approval => {
            sections.extend(approval_lines(state, width, palette));
            sections.extend(queue_lines(state, true, bindings, width, palette));
        }
    }
    sections.extend([
        context_lines(state, width, palette),
        model_lines(state, focus, width, palette),
        file_lines(state, panel, width, palette),
    ]);
    let mcp = mcp_line(state, width, palette);
    let turns = if focus == Focus::Idle {
        // The turn list takes what the other sections leave, keeping its newest turns.
        let fixed: usize = sections.iter().map(|lines| lines.len() + 1).sum::<usize>()
            + mcp.as_ref().map_or(0, |_| 2)
            + 1;
        turn_lines(state, width, height.saturating_sub(fixed + 1), palette)
    } else {
        folded_turn_lines(state, focus, width, palette)
    };
    if !turns.is_empty() {
        sections.push(turns);
    }
    if let Some(mcp) = mcp {
        sections.push(vec![mcp]);
    }

    let mut lines = Vec::new();
    for section in sections {
        if !lines.is_empty() {
            lines.push(Line::default());
        }
        lines.extend(section);
    }
    lines.truncate(height.saturating_sub(1));
    let hint_row = Rect {
        y: inner.bottom() - 1,
        height: 1,
        ..inner
    };
    frame.render_widget(Paragraph::new(lines), inner);
    frame.render_widget(
        Paragraph::new(ui::keycap_hints(&hint, palette)).style(Style::default().fg(palette.muted)),
        hint_row,
    );
}

/// A row with `left` at the start and `right` against the end; the left side is
/// clipped to leave room for the right.
fn spread(left: Vec<Span<'static>>, right: Vec<Span<'static>>, width: usize) -> Line<'static> {
    let right_width: usize = right.iter().map(|span| span.content.width()).sum();
    let room = width.saturating_sub(right_width + usize::from(right_width > 0));
    let mut spans = clip_spans(left, room);
    let left_width: usize = spans.iter().map(|span| span.content.width()).sum();
    spans.push(Span::raw(
        " ".repeat(width.saturating_sub(left_width + right_width)),
    ));
    spans.extend(right);
    Line::from(spans)
}

fn clip_spans(spans: Vec<Span<'static>>, width: usize) -> Vec<Span<'static>> {
    let mut used = 0;
    let mut clipped = Vec::new();
    for span in spans {
        let span_width = span.content.width();
        if used + span_width <= width {
            used += span_width;
            clipped.push(span);
        } else {
            let text = ui::clip_columns(&span.content, width - used);
            clipped.push(Span::styled(text, span.style));
            break;
        }
    }
    clipped
}

fn heading(title: &str, palette: Palette) -> Span<'static> {
    Span::styled(
        title.to_owned(),
        Style::default()
            .fg(palette.muted)
            .add_modifier(Modifier::BOLD),
    )
}

/// The session's name, or its first prompt before it has one, and where it works.
fn header_lines(state: &UiState, width: usize, palette: Palette) -> Vec<Line<'static>> {
    let title = state
        .selected_session
        .as_ref()
        .and_then(|session| session.session_name.as_deref())
        .map(sanitize_for_terminal)
        .or_else(|| {
            state
                .transcript
                .entries()
                .iter()
                .find(|entry| entry.role == TranscriptRole::User)
                .map(|entry| first_line(entry.display_content()))
        })
        .unwrap_or_else(|| "New session".to_owned());
    let mut lines = vec![spread(
        vec![Span::styled(
            title,
            Style::default().add_modifier(Modifier::BOLD),
        )],
        vec![Span::styled(
            ui::status_label(state),
            Style::default().fg(ui::status_color(state, palette)),
        )],
        width,
    )];
    let workspace = ui::footer_workspace_parts(state);
    if !workspace.is_empty() {
        lines.push(Line::styled(
            ui::clip_columns(&workspace.join(" · "), width),
            Style::default().fg(palette.muted),
        ));
    }
    lines
}

fn first_line(text: &str) -> String {
    sanitize_for_terminal(
        text.lines()
            .find(|line| !line.trim().is_empty())
            .unwrap_or(""),
    )
    .trim()
    .to_owned()
}

/// `Now`: what the run is doing and for how long, then the turn it has reached, its
/// tool count and time.
fn now_lines(
    state: &UiState,
    activity_frame: u8,
    run_elapsed: Option<Duration>,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    let running = Style::default().fg(palette.secondary);
    let muted = Style::default().fg(palette.muted);
    let activity = ui::run_activity(state);
    let spinner = Span::styled(format!("{} ", ui::spinner_frame(activity_frame)), running);
    let doing = match activity.item {
        Some(item) => spread(
            vec![
                spinner,
                Span::styled(format!(" {} ", item.label), palette.chip(&item.label)),
                Span::raw(" "),
                Span::raw(item.detail),
            ],
            item.elapsed
                .map(|elapsed| vec![Span::styled(duration(elapsed), running)])
                .unwrap_or_default(),
            width,
        ),
        None => Line::from(vec![
            spinner,
            Span::styled(ui::activity_phase(state), running),
        ]),
    };
    let mut totals = Vec::new();
    if activity.turn > 0 {
        totals.push(format!("turn {}", activity.turn));
    }
    totals.push(format!(
        "{} tool{}",
        activity.tools,
        if activity.tools == 1 { "" } else { "s" }
    ));
    if let Some(elapsed) = run_elapsed {
        totals.push(duration(elapsed));
    }
    vec![
        Line::from(heading("Now", palette)),
        doing,
        spread(
            vec![Span::styled(totals.join(" · "), muted)],
            ui::keycap_hints("Esc interrupt", palette).spans,
            width,
        ),
    ]
}

fn duration(elapsed: Duration) -> String {
    crate::tool_cards::format_duration(u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX))
}

/// Queued rows listed before the rest are counted.
const MAX_QUEUE_ROWS: usize = 5;

/// Messages waiting for the run, in the order it takes them in: steers join at its
/// next step, follow-ups once it ends. Nothing while the queue is empty.
fn queue_lines(
    state: &UiState,
    held: bool,
    bindings: &Bindings,
    width: usize,
    palette: Palette,
) -> Option<Vec<Line<'static>>> {
    let muted = Style::default().fg(palette.muted);
    let queued: Vec<_> = state
        .queue
        .steering
        .iter()
        .map(|message| ("↪ steer    ", message))
        .chain(
            state
                .queue
                .follow_up
                .iter()
                .map(|message| ("⏎ follow-up", message)),
        )
        .collect();
    if queued.is_empty() {
        return None;
    }
    let mut lines = vec![spread(
        vec![
            heading("Queue ", palette),
            Span::styled(queued.len().to_string(), muted),
        ],
        vec![Span::styled(
            if held {
                "held until you answer"
            } else {
                "delivered in order"
            },
            muted,
        )],
        width,
    )];
    let number_width = queued.len().min(MAX_QUEUE_ROWS).to_string().len();
    for (index, (kind, message)) in queued.iter().enumerate().take(MAX_QUEUE_ROWS) {
        let lead = format!("{:>number_width$} {kind} ", index + 1);
        let room = width.saturating_sub(lead.width());
        lines.push(Line::from(vec![
            Span::styled(lead, Style::default().fg(palette.secondary)),
            Span::raw(ui::clip_columns(&first_line(&message.content), room)),
        ]));
    }
    if queued.len() > MAX_QUEUE_ROWS {
        lines.push(Line::styled(
            format!("+{} more · /queue to review", queued.len() - MAX_QUEUE_ROWS),
            muted,
        ));
    }
    // A held queue waits on the decision, which takes the keyboard until answered.
    if held {
        return Some(lines);
    }
    lines.push(Line::styled(
        ui::clip_columns("steers join at the next step; follow-ups wait", width),
        muted,
    ));
    let restore = ui::primary_label(bindings, KeyAction::RestoreQueue);
    let hints = if restore == "Unbound" {
        "/queue manage".to_owned()
    } else {
        format!("{restore} restore newest · /queue manage")
    };
    lines.push(ui::keycap_hints(&hints, palette).style(muted));
    Some(lines)
}

/// `Needs approval`: the call waiting on the user, how long it has waited, and what
/// the permission mode means for it. A call missing from the transcript is named by
/// its tool alone.
fn approval_lines(state: &UiState, width: usize, palette: Palette) -> Option<Vec<Line<'static>>> {
    let pending = state.pending_approval.as_ref()?;
    let muted = Style::default().fg(palette.muted);
    let entry = state
        .transcript
        .tool_entry_for_call(&pending.call_id)
        .and_then(|id| state.transcript.entry_index(id))
        .map(|index| &state.transcript.entries()[index]);
    let tool = entry.and_then(|entry| entry.tool_card());
    let (label, detail) = match (tool, entry.and_then(|entry| entry.process_card())) {
        (Some(card), _) => (card.action_label(), card.display_arguments()),
        // Polling or stopping a background process asks about the process itself.
        (None, Some(card)) => (
            card.action_label().to_owned(),
            card.task_label
                .clone()
                .unwrap_or_else(|| "Background command".to_owned()),
        ),
        (None, None) => (pending.name.clone(), String::new()),
    };
    let label = sanitize_for_terminal(&label);
    let detail = sanitize_for_terminal(&detail.split_whitespace().collect::<Vec<_>>().join(" "));
    // A tool card's clock starts at its call, so it times the wait; a process card's
    // clock is the process's own age.
    let waited = tool
        .and(entry)
        .and_then(|entry| entry.running_elapsed())
        .map(|elapsed| {
            vec![Span::styled(
                duration(elapsed),
                Style::default().fg(palette.warning),
            )]
        })
        .unwrap_or_default();
    let mut lines = vec![
        spread(
            vec![Span::styled(
                "Needs approval",
                Style::default()
                    .fg(palette.warning)
                    .add_modifier(Modifier::BOLD),
            )],
            waited,
            width,
        ),
        Line::from(vec![
            Span::styled(format!(" {label} "), palette.chip(&label)),
            Span::raw(" "),
            Span::raw(ui::clip_columns(
                &detail,
                width.saturating_sub(label.width() + 3),
            )),
        ]),
    ];
    let mut paused = vec![sanitize_for_terminal(&pending.safety)];
    let turn = state.run_progress.map_or(0, |run| run.turn);
    paused.push(if turn > 0 {
        format!("turn {turn} paused")
    } else {
        "run paused".to_owned()
    });
    lines.push(Line::styled(
        ui::clip_columns(&paused.join(" · "), width),
        muted,
    ));
    if let Some(mode) = permission_mode(state) {
        // YOLO skips only the prompts it can grant; some tools ask on every call.
        let note = if mode == "ask" && pending.grants_allowed {
            "permissions ask · YOLO skips prompts like this"
        } else if pending.grants_allowed {
            "permissions YOLO"
        } else {
            "asks on every call"
        };
        lines.push(Line::styled(ui::clip_columns(note, width), muted));
    }
    Some(lines)
}

/// `Context  198.8k / 272.0k 73%` over a bar of the window's fill.
fn context_lines(state: &UiState, width: usize, palette: Palette) -> Vec<Line<'static>> {
    let muted = Style::default().fg(palette.muted);
    let Some(usage) = crate::context_view::usage(state) else {
        let reading = if state.context.loading() {
            "reading…"
        } else {
            "unknown"
        };
        return vec![spread(
            vec![heading("Context", palette)],
            vec![Span::styled(reading, muted)],
            width,
        )];
    };
    let prefix = if usage.approximate { "~" } else { "" };
    let used = crate::context_view::abbreviated(usage.tokens);
    let Some(window) = usage.window else {
        return vec![spread(
            vec![heading("Context", palette)],
            vec![Span::raw(format!("{prefix}{used}"))],
            width,
        )];
    };
    let fill = usage.tokens as f64 / window as f64;
    let pressured = fill >= ui::CONTEXT_WARNING_FILL;
    let level = if pressured {
        Style::default().fg(palette.warning)
    } else {
        muted
    };
    let mut lines = vec![spread(
        vec![heading("Context", palette)],
        vec![
            Span::raw(format!("{prefix}{used}")),
            Span::styled(
                format!(" / {} ", crate::context_view::abbreviated(window)),
                muted,
            ),
            Span::styled(format!("{prefix}{:.0}%", fill * 100.0), level),
        ],
        width,
    )];
    let filled = ((fill * width as f64).round() as usize).min(width);
    lines.push(Line::from(vec![
        Span::styled(
            "━".repeat(filled),
            Style::default().fg(if pressured {
                palette.warning
            } else {
                palette.secondary
            }),
        ),
        Span::styled(
            "━".repeat(width - filled),
            Style::default().fg(palette.panel),
        ),
    ]));
    lines
}

/// The model, its effort, the session's and this turn's cost, and the permission
/// mode, which the approval block shows instead while a decision waits.
fn model_lines(
    state: &UiState,
    focus: Focus,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    let muted = Style::default().fg(palette.muted);
    let mut lines = vec![Line::from(heading("Model", palette))];
    let model = state
        .model_catalog
        .as_ref()
        .and_then(|catalog| catalog.selection.effective_model.as_deref())
        .or(state.model.as_deref());
    let mut identity = Vec::new();
    if let Some(model) = model {
        identity.push(Span::raw(sanitize_for_terminal(model)));
    }
    if let Some(provider) = state.provider.as_deref() {
        if !identity.is_empty() {
            identity.push(Span::raw(" "));
        }
        identity.push(Span::styled(sanitize_for_terminal(provider), muted));
    }
    let effort = state
        .effort
        .as_deref()
        .map(|effort| {
            vec![Span::styled(
                sanitize_for_terminal(effort),
                Style::default()
                    .fg(palette.warning)
                    .add_modifier(Modifier::BOLD),
            )]
        })
        .unwrap_or_default();
    if !identity.is_empty() || !effort.is_empty() {
        lines.push(spread(identity, effort, width));
    }
    let cost = crate::context_view::session_cost(state).map_or_else(
        || vec![Span::styled("cost unknown", muted)],
        |cost| {
            vec![
                Span::styled("cost ", muted),
                Span::raw(cost),
                Span::styled(" session", muted),
            ]
        },
    );
    let turn_cost = crate::context_view::turn_cost(state)
        .map(|cost| vec![Span::raw(cost), Span::styled(" this turn", muted)])
        .unwrap_or_default();
    lines.push(spread(cost, turn_cost, width));
    if focus != Focus::Approval {
        if let Some(mode) = permission_mode(state) {
            lines.push(Line::from(vec![
                Span::styled("permissions ", muted),
                Span::raw(mode),
            ]));
        }
    }
    lines
}

fn permission_mode(state: &UiState) -> Option<&'static str> {
    state
        .permissions
        .snapshot
        .as_ref()
        .map(|permissions| match permissions.mode {
            PermissionMode::Ask => "ask",
            PermissionMode::Yolo => "YOLO",
        })
}

/// Git's changed files with their line counts; the rest are counted on one row.
fn file_lines(
    state: &UiState,
    panel: &SidePanel,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    let muted = Style::default().fg(palette.muted);
    let Some(summary) = &panel.files else {
        let note = if let Some(error) = &state.git_diff.error {
            sanitize_for_terminal(error)
        } else {
            "reading…".to_owned()
        };
        return vec![
            Line::from(heading("Changed files", palette)),
            Line::styled(ui::clip_columns(&note, width), muted),
        ];
    };
    let count = summary.snapshot.files.len();
    // A partial snapshot omits files past Git's limits, so its count is a floor.
    let partial = summary.snapshot.truncated;
    let mut lines = vec![spread(
        vec![heading("Changed files", palette)],
        vec![Span::styled(
            match (count, partial) {
                (0, _) => String::new(),
                (1, false) => "1 file".to_owned(),
                (count, false) => format!("{count} files"),
                (count, true) => format!("{count}+ files"),
            },
            muted,
        )],
        width,
    )];
    if count == 0 {
        lines.push(Line::styled(
            if partial {
                "Changed files omitted · /diff to review"
            } else {
                "No changes"
            },
            muted,
        ));
        return lines;
    }
    for row in &summary.rows {
        let status_style = Style::default().fg(match row.status {
            'A' => palette.success,
            'D' => palette.deletion,
            '?' => palette.muted,
            _ => palette.warning,
        });
        let counts = if row.counted {
            let mut counts = vec![Span::styled(
                format!("+{}", row.additions),
                Style::default().fg(palette.addition),
            )];
            if row.deletions > 0 {
                counts.push(Span::styled(
                    format!(" −{}", row.deletions),
                    Style::default().fg(palette.deletion),
                ));
            }
            counts
        } else {
            Vec::new()
        };
        let counts_width: usize = counts.iter().map(|span| span.content.width()).sum();
        let path_room = width.saturating_sub(2 + counts_width + usize::from(counts_width > 0));
        lines.push(spread(
            vec![
                Span::styled(format!("{} ", row.status), status_style),
                Span::raw(clip_path(&row.path, path_room)),
            ],
            counts,
            width,
        ));
    }
    if count > MAX_FILE_ROWS {
        lines.push(Line::styled(
            format!("+{} more · /diff to review", count - MAX_FILE_ROWS),
            muted,
        ));
    }
    if partial {
        lines.push(Line::styled("partial: some files omitted", muted));
    }
    lines
}

/// Keep a path's end, where its file name is: `…/providers/codex.py`.
fn clip_path(path: &str, width: usize) -> String {
    if path.width() <= width {
        return path.to_owned();
    }
    let mut kept = String::new();
    for character in path.chars().rev() {
        if kept.width() + character.to_string().width() + 1 > width {
            break;
        }
        kept.insert(0, character);
    }
    format!("…{kept}")
}

/// The session's prompts, newest last, with how long each run took and how it
/// ended. Prompts loaded from history have no timing.
fn turn_lines(state: &UiState, width: usize, rows: usize, palette: Palette) -> Vec<Line<'static>> {
    if rows < 2 {
        return Vec::new();
    }
    let muted = Style::default().fg(palette.muted);
    let prompts: Vec<_> = state
        .transcript
        .entries()
        .iter()
        .filter(|entry| entry.role == TranscriptRole::User)
        .collect();
    if prompts.is_empty() {
        return Vec::new();
    }
    let running = matches!(
        state.view_status,
        ViewStatus::Running | ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
    );
    let shown = prompts.len().min(rows - 1);
    let first = prompts.len() - shown;
    let number_width = prompts.len().to_string().len();
    let mut lines = vec![spread(
        vec![heading("Turns", palette)],
        vec![Span::styled(prompts.len().to_string(), muted)],
        width,
    )];
    for (index, prompt) in prompts.iter().enumerate().skip(first) {
        let latest = index + 1 == prompts.len();
        let (duration, outcome) = match prompt.run_result() {
            Some(result) => (
                result
                    .duration_ms
                    .map(crate::tool_cards::format_duration)
                    .unwrap_or_default(),
                match result.outcome {
                    RunOutcome::Completed => {
                        Span::styled("✓", Style::default().fg(palette.success))
                    }
                    RunOutcome::Cancelled => Span::styled("⊘", muted),
                    RunOutcome::Failed => Span::styled("✗", Style::default().fg(palette.error)),
                },
            ),
            None if latest && running => (
                String::new(),
                Span::styled(SPINNER_OUTCOME, Style::default().fg(palette.secondary)),
            ),
            None => (String::new(), Span::raw(" ")),
        };
        let text_style = if latest { Style::default() } else { muted };
        let mut right = Vec::new();
        if !duration.is_empty() {
            right.push(Span::styled(format!("{duration} "), muted));
        }
        right.push(outcome);
        lines.push(spread(
            vec![
                Span::styled(format!("{:>number_width$} ", index + 1), muted),
                Span::styled(first_line(prompt.display_content()), text_style),
            ],
            right,
            width,
        ));
    }
    lines
}

/// The turn list folded to one row while a run or a decision leads the panel.
fn folded_turn_lines(
    state: &UiState,
    focus: Focus,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    let muted = Style::default().fg(palette.muted);
    let prompts = state
        .transcript
        .entries()
        .iter()
        .filter(|entry| entry.role == TranscriptRole::User)
        .count();
    if prompts == 0 {
        return Vec::new();
    }
    vec![spread(
        vec![
            Span::styled("▸ ", muted),
            heading("Turns ", palette),
            Span::styled(prompts.to_string(), muted),
        ],
        vec![Span::styled(
            if focus == Focus::Running {
                "collapsed while running"
            } else {
                ""
            },
            muted,
        )],
        width,
    )]
}

/// `MCP ●●●○ 3/4 connected`, once the servers' status is known.
fn mcp_line(state: &UiState, width: usize, palette: Palette) -> Option<Line<'static>> {
    let servers = &state.mcp.snapshot.as_ref()?.servers;
    if servers.is_empty() {
        return None;
    }
    let connected = servers
        .iter()
        .filter(|server| server.status == McpServerStatus::Connected)
        .count();
    let mut dots = vec![heading("MCP ", palette)];
    for server in servers.iter().take(12) {
        let (dot, color) = match server.status {
            McpServerStatus::Connected => ("●", palette.success),
            McpServerStatus::Disabled => ("○", palette.muted),
            McpServerStatus::Disconnected | McpServerStatus::Unavailable => ("●", palette.error),
        };
        dots.push(Span::styled(dot, Style::default().fg(color)));
    }
    Some(spread(
        dots,
        vec![Span::styled(
            format!("{connected}/{} connected", servers.len()),
            Style::default().fg(palette.muted),
        )],
        width,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::keybindings::Bindings;
    use crate::prompt_editor::PromptEditor;
    use crate::reducer::git_diff::GitDiffFile;
    use crate::startup_logo::LogoChoice;
    use crate::transcript::RunResult;
    use crate::transcript_view::{TranscriptRowCache, TranscriptViewport};
    use crate::ui::{ComposerLayoutCache, ConnectionInfo};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use wisp_protocol::events::{
        ContextAccountingMethod, ContextBudget, ContextEstimate, McpServerSnapshot,
        McpStatusSnapshot, PermissionState, SessionCostSummary, SessionStats, SessionTokenUsage,
    };

    fn budget() -> ContextBudget {
        ContextBudget {
            estimate: ContextEstimate {
                total_tokens: 198_800,
            },
            observed_tokens: Some(198_800),
            observed_is_current: true,
            effective_tokens: None,
            accounting_method: ContextAccountingMethod::ProviderObserved,
            context_window: Some(272_000),
            reserve_tokens: 1_000,
        }
    }

    fn file(path: &str, status: &str, before: &str, after: &str) -> GitDiffFile {
        GitDiffFile {
            path: path.into(),
            status: status.into(),
            before: before.into(),
            after: after.into(),
            notice: String::new(),
        }
    }

    fn changes(files: Vec<GitDiffFile>) -> Arc<GitDiffSnapshot> {
        Arc::new(GitDiffSnapshot {
            generation: 1,
            head: "abc123".into(),
            files,
            truncated: false,
        })
    }

    /// A session two runs in, with everything the panel shows loaded.
    fn session() -> UiState {
        let mut state = UiState::new(
            "openai-codex".into(),
            Some("gpt-6-astra".into()),
            Some("high".into()),
        );
        state.workspace_cwd = Some("/srv/wisp".into());
        state.git_branch = Some("fix/timeouts".into());
        state.context.budget = Some(budget());
        state.context.stats = Some(Box::new(SessionStats {
            session_id: None,
            active_message_count: 4,
            compaction_count: 0,
            usage: SessionTokenUsage {
                input_tokens: 1,
                output_tokens: 1,
                total_tokens: 2,
                cache_read_input_tokens: None,
                cache_write_input_tokens: None,
            },
            context: budget(),
            compaction: None,
            cost: SessionCostSummary {
                known_usd: "1.8412".into(),
                complete: true,
                priced_record_count: 3,
                unpriced_record_count: 0,
            },
        }));
        state.permissions.snapshot = Some(Arc::new(PermissionState {
            mode: PermissionMode::Ask,
            saved_mode: None,
            project_path: None,
            session_only_yolo: false,
        }));
        state.mcp.snapshot = Some(Arc::new(McpStatusSnapshot {
            servers: ["docs", "linear"]
                .into_iter()
                .map(|name| McpServerSnapshot {
                    name: name.into(),
                    status: McpServerStatus::Connected,
                    tool_names: Vec::new(),
                    error: None,
                    launch: None,
                    sign_in: None,
                })
                .collect(),
        }));
        state.git_diff.snapshot = Some(changes(vec![
            file("src/lib.rs", "modified", "a\nb\n", "a\nc\nd\n"),
            file("notes.md", "untracked", "", "new\n"),
        ]));
        for (turn, (prompt, outcome)) in [
            ("why does codex time out", RunOutcome::Completed),
            ("add a read timeout setting", RunOutcome::Cancelled),
        ]
        .into_iter()
        .enumerate()
        {
            let id = state.transcript.append_prompt(prompt.into());
            state
                .transcript
                .complete_message(turn as u64 + 1, "Done.".into());
            state.transcript.attach_run_result(
                id,
                RunResult {
                    duration_ms: Some(252_000),
                    outcome,
                },
            );
        }
        state
    }

    fn screen(state: &UiState, panel: &mut SidePanel, width: u16) -> Vec<String> {
        panel.observe_width(width);
        panel.sync_files(&state.git_diff);
        let mut terminal = Terminal::new(TestBackend::new(width, 40)).unwrap();
        terminal
            .draw(|frame| {
                ui::render_interactive(
                    frame,
                    state,
                    &mut TranscriptViewport::default(),
                    &mut TranscriptRowCache::default(),
                    &mut ComposerLayoutCache::default(),
                    &PromptEditor::default(),
                    &ConnectionInfo {
                        backend_version: "test".into(),
                    },
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
                    Some(panel),
                );
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        (0..buffer.area.height)
            .map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect())
            .collect()
    }

    /// The panel's columns of each row.
    fn panel_text(rows: &[String], width: u16) -> String {
        let start = usize::from(width - PANEL_WIDTH);
        rows.iter()
            .map(|row| {
                row.chars()
                    .skip(start)
                    .collect::<String>()
                    .trim_end()
                    .to_owned()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn wide_terminal_docks_panel() {
        let rows = screen(&session(), &mut SidePanel::default(), 200);
        let panel = panel_text(&rows, 200);
        for expected in [
            "why does codex time out",
            "/srv/wisp · fix/timeouts",
            "198.8k / 272.0k 73%",
            "gpt-6-astra openai-codex",
            "cost $1.84 session",
            "permissions ask",
            "2 files",
            "M src/lib.rs",
            "+2 −1",
            "? notes.md",
            "Turns",
            "4m 12s ✓",
            "4m 12s ⊘",
            "2/2 connected",
            "Ctrl+B hide panel",
        ] {
            assert!(panel.contains(expected), "{expected}\n{panel}");
        }
    }

    #[test]
    fn docked_panel_trims_footer() {
        let rows = screen(&session(), &mut SidePanel::default(), 200);
        let footer = rows.last().unwrap();
        let conversation: String = footer
            .chars()
            .take(usize::from(200 - PANEL_WIDTH))
            .collect();
        assert!(conversation.contains("idle ·"), "{conversation}");
        assert!(!conversation.contains("/srv/wisp"), "{conversation}");
        assert!(!conversation.contains("ctx"), "{conversation}");
    }

    #[test]
    fn undocked_footer_offers_panel() {
        let rows = screen(&session(), &mut SidePanel::default(), 140);
        let footer = rows.last().unwrap();
        assert!(!rows.iter().any(|row| row.contains("Changed files")));
        assert!(footer.contains("/srv/wisp · fix/timeouts"), "{footer}");
        assert!(footer.contains("Ctrl+B panel"), "{footer}");
    }

    #[test]
    fn long_file_lists_point_to_diff() {
        let mut state = session();
        state.git_diff.snapshot = Some(changes(
            (0..10)
                .map(|index| {
                    file(
                        &format!("src/deeply/nested/module_{index}.rs"),
                        "added",
                        "",
                        "x\n",
                    )
                })
                .collect(),
        ));
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(panel.contains("10 files"), "{panel}");
        assert!(panel.contains("A src/deeply/nested/module_7.rs"), "{panel}");
        assert!(!panel.contains("module_8.rs"), "{panel}");
        assert!(panel.contains("+2 more · /diff to review"), "{panel}");
    }

    #[test]
    fn partial_snapshot_says_so() {
        let mut state = session();
        let mut snapshot = changes(vec![file("src/lib.rs", "modified", "a\n", "b\n")]);
        Arc::get_mut(&mut snapshot).unwrap().truncated = true;
        state.git_diff.snapshot = Some(snapshot);
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(panel.contains("1+ files"), "{panel}");
        assert!(panel.contains("partial: some files omitted"), "{panel}");

        let mut empty = changes(Vec::new());
        Arc::get_mut(&mut empty).unwrap().truncated = true;
        state.git_diff.snapshot = Some(empty);
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(!panel.contains("No changes"), "{panel}");
        assert!(panel.contains("Changed files omitted"), "{panel}");
    }

    #[test]
    fn small_costs_keep_precision() {
        let cost = |usd: &str| {
            let mut state = session();
            state.context.stats.as_mut().unwrap().cost.known_usd = usd.into();
            crate::context_view::session_cost(&state).unwrap()
        };
        assert_eq!(cost("0.0012"), "$0.0012");
        assert_eq!(cost("0.25"), "$0.250");
        assert_eq!(cost("0.00001"), "<$0.0001");
        assert_eq!(cost("1.8412"), "$1.84");
    }

    /// The session with a third prompt running a shell call at turn 6.
    fn running() -> UiState {
        let mut state = session();
        let prompt = state.transcript.append_prompt("now push it".into());
        state.run_progress = Some(crate::reducer::RunProgress { prompt, turn: 6 });
        state.view_status = ViewStatus::Running;
        state.transcript.observe_tool_call(push_call());
        state
    }

    fn push_call() -> crate::tool_cards::ToolCallInput {
        crate::tool_cards::ToolCallInput {
            call_id: "push-1".into(),
            name: "bash".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"command": "git push -u origin fix/timeouts"}),
        }
    }

    fn queue(state: &mut UiState, steering: &[&str], follow_up: &[&str]) {
        struct Ids;
        impl crate::reducer::CommandIdSource for Ids {
            fn next_id(&mut self, kind: crate::reducer::CommandKind) -> String {
                format!("{}-1", kind.prefix())
            }
        }
        crate::reducer::reduce(
            state,
            crate::reducer::UiAction::BackendEvent(crate::reducer::BackendEvent::QueueUpdated {
                steering: steering.iter().map(|&text| text.into()).collect(),
                follow_up: follow_up.iter().map(|&text| text.into()).collect(),
            }),
            &mut Ids,
        )
        .unwrap();
    }

    /// The running session waiting to approve its push, with a follow-up queued.
    fn approving() -> UiState {
        let mut state = running();
        state.transcript.observe_approval_requested(push_call());
        state.pending_approval = Some(crate::reducer::PendingApproval {
            call_id: "push-1".into(),
            name: "bash".into(),
            arguments: serde_json::json!({"command": "git push -u origin fix/timeouts"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        });
        state.view_status = ViewStatus::WaitingForApproval;
        queue(&mut state, &[], &["then open the PR"]);
        state
    }

    fn row_of(panel: &str, text: &str) -> usize {
        panel
            .lines()
            .position(|line| line.contains(text))
            .unwrap_or_else(|| panic!("{text}\n{panel}"))
    }

    #[test]
    fn running_leads_with_now() {
        let panel = panel_text(&screen(&running(), &mut SidePanel::default(), 200), 200);
        for expected in [
            "working",
            "Bash  git push -u origin fix/timeouts",
            "turn 6 · 1 tool",
            "Esc interrupt",
            "▸ Turns 3",
            "collapsed while running",
            "permissions ask",
        ] {
            assert!(panel.contains(expected), "{expected}\n{panel}");
        }
        assert!(row_of(&panel, "Now") < row_of(&panel, "Context"), "{panel}");
        // The turn list folds, and an empty queue takes no rows.
        assert!(!panel.contains("4m 12s ✓"), "{panel}");
        assert!(!panel.contains("Queue"), "{panel}");
    }

    #[test]
    fn queue_lists_steers_first() {
        let mut state = running();
        queue(
            &mut state,
            &["also check the docs"],
            &["then draft release notes"],
        );
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        for expected in [
            "Queue 2",
            "delivered in order",
            "steers join at the next step",
            "Alt+Up restore newest · /queue manage",
        ] {
            assert!(panel.contains(expected), "{expected}\n{panel}");
        }
        let steer = row_of(&panel, "1 ↪ steer");
        assert!(
            panel
                .lines()
                .nth(steer)
                .unwrap()
                .contains("also check the docs")
        );
        assert_eq!(row_of(&panel, "2 ⏎ follow-up then draft"), steer + 1);
        assert!(row_of(&panel, "Now") < row_of(&panel, "Queue"), "{panel}");
        assert!(
            row_of(&panel, "Queue") < row_of(&panel, "Context"),
            "{panel}"
        );
    }

    #[test]
    fn approval_leads_with_decision() {
        let state = approving();
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        for expected in [
            "Needs approval",
            "Bash  git push -u origin fix/timeouts",
            "command · turn 6 paused",
            "permissions ask · YOLO skips prompts like this",
            "Queue 1",
            "held until you answer",
            "1 ⏎ follow-up then open the PR",
            "▸ Turns 3",
        ] {
            assert!(panel.contains(expected), "{expected}\n{panel}");
        }
        assert!(
            row_of(&panel, "Needs approval") < row_of(&panel, "Queue"),
            "{panel}"
        );
        assert!(
            row_of(&panel, "Queue") < row_of(&panel, "Context"),
            "{panel}"
        );
        // The run is paused, not running, and the mode is stated once.
        assert!(!panel.contains("Now"), "{panel}");
        assert!(!panel.contains("collapsed while running"), "{panel}");
        assert!(!panel.contains("steers join"), "{panel}");
        assert!(!panel.contains("restore newest"), "{panel}");
        assert_eq!(panel.matches("permissions").count(), 1, "{panel}");
    }

    fn overlay_text(state: &UiState) -> String {
        let mut panel = SidePanel::default();
        panel.observe_width(140);
        panel.toggle();
        let mut terminal = Terminal::new(TestBackend::new(140, 40)).unwrap();
        terminal
            .draw(|frame| {
                let area = overlay_area(frame.area());
                render(
                    frame,
                    area,
                    state,
                    &panel,
                    &Bindings::default(),
                    0,
                    None,
                    Palette::default(),
                );
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        let rows: Vec<String> = (0..buffer.area.height)
            .map(|y| (0..140).map(|x| buffer[(x, y)].symbol()).collect())
            .collect();
        panel_text(&rows, 140)
    }

    #[test]
    fn overlay_follows_state() {
        let panel = overlay_text(&running());
        assert!(row_of(&panel, "Now") < row_of(&panel, "Context"), "{panel}");
        assert!(panel.contains("Ctrl+B close · Esc close"), "{panel}");
        let panel = overlay_text(&approving());
        assert!(
            row_of(&panel, "Needs approval") < row_of(&panel, "Queue"),
            "{panel}"
        );
        assert!(
            row_of(&panel, "Queue") < row_of(&panel, "Context"),
            "{panel}"
        );
    }

    #[test]
    fn stale_run_costs_latest_turn() {
        let priced = |nano_usd| {
            Some(crate::web_search::MessageCost {
                label: String::new(),
                nano_usd: Some(nano_usd),
                lower_bound: false,
            })
        };
        let mut state = session();
        let first = state.transcript.entries()[0].id;
        state.transcript.append_prompt("third".into());
        state
            .transcript
            .complete_message_with_cost(3, "Done.".into(), priced(500_000_000));
        state.transcript.append_prompt("fourth".into());
        state
            .transcript
            .complete_message_with_cost(4, "Done.".into(), priced(20_000_000));
        // A replaced transcript leaves the finished run's prompt id naming an older
        // entry; at idle only the latest prompt's replies count.
        state.run_progress = Some(crate::reducer::RunProgress {
            prompt: first,
            turn: 1,
        });
        assert_eq!(
            crate::context_view::turn_cost(&state).as_deref(),
            Some("$0.020")
        );
        // Compacting starts no run, so the latest turn still counts.
        state.current_command = Some(crate::reducer::ActiveCommand {
            id: "compact-1".into(),
            command_type: crate::reducer::ActiveCommandType::Compact,
        });
        assert_eq!(
            crate::context_view::turn_cost(&state).as_deref(),
            Some("$0.020")
        );
    }

    #[test]
    fn turn_cost_marks_floors() {
        let cost = |nano_usd, lower_bound| crate::web_search::MessageCost {
            label: String::new(),
            nano_usd,
            lower_bound,
        };
        let mut state = session();
        let turn = |state: &UiState| crate::context_view::turn_cost(state);
        assert_eq!(turn(&state), None);
        state.transcript.append_prompt("price it".into());
        state.transcript.complete_message_with_cost(
            3,
            "Reading.".into(),
            Some(cost(Some(210_000_000), false)),
        );
        state.transcript.complete_message_with_cost(
            4,
            "Done.".into(),
            Some(cost(Some(100_000_000), false)),
        );
        assert_eq!(turn(&state).as_deref(), Some("$0.310"));
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(panel.contains("$0.310 this turn"), "{panel}");

        // An unpriced reply or a web search leaves only a floor.
        state
            .transcript
            .complete_message_with_cost(5, "Searched.".into(), Some(cost(None, false)));
        assert_eq!(turn(&state).as_deref(), Some("≥$0.310"));
    }

    #[test]
    fn long_paths_keep_file_name() {
        assert_eq!(
            clip_path("src/wisp/providers/codex.py", 14),
            "…ders/codex.py"
        );
        assert_eq!(clip_path("src/lib.rs", 14), "src/lib.rs");
    }

    #[test]
    fn visibility_reports_once() {
        let mut panel = SidePanel::default();
        panel.observe_width(200);
        assert_eq!(panel.take_visibility_change(), Some(true));
        assert_eq!(panel.take_visibility_change(), None);
        panel.toggle();
        assert_eq!(panel.take_visibility_change(), Some(false));
        // Narrowing leaves the panel hidden until Ctrl+B opens the overlay.
        panel.observe_width(100);
        assert_eq!(panel.take_visibility_change(), None);
        panel.toggle();
        assert!(panel.overlay_open());
        assert_eq!(panel.take_visibility_change(), Some(true));
    }
}
