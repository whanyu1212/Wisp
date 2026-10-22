//! The side panel: what the session is, what it costs, and what it changed, beside
//! the transcript on a wide terminal or over it as an overlay opened with Ctrl+B.
//!
//! The panel only reads state. The reducer keeps its data loaded while it is on
//! screen (see `UiAction::SetSidePanelVisible`); this module decides where it goes
//! and draws it.

use std::cell::Cell;
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::context_split::{self, SPARK_TURNS, Split, abbreviated};
use crate::git_diff_view::EditCard;
use crate::keybindings::{Action as KeyAction, Bindings};
use crate::reducer::git_diff::{GitDiff, GitDiffEdit, GitDiffSnapshot};
use crate::reducer::{UiState, ViewStatus};
use crate::theme::Palette;
use crate::transcript::{RunOutcome, TranscriptRole};
use crate::ui::{self, sanitize_for_terminal};
use crossterm::event::{KeyCode, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Padding, Paragraph};
use unicode_width::UnicodeWidthStr;
use wisp_protocol::commands::PermissionMode;
use wisp_protocol::events::{
    ContextBreakdown, ContextSource, McpServerStatus, SessionBranchSummary,
};

/// The panel's width, its border included: the overlay's width, and the docked
/// panel's on the narrowest terminal that docks it.
pub(crate) const PANEL_WIDTH: u16 = 56;

/// The widest a docked panel grows on its own, so long paths and names fit without
/// the panel becoming a second transcript. Dragging its border can go wider.
const MAX_PANEL_WIDTH: u16 = 76;

/// Columns from the panel's left edge that start a resize: the border and the gap
/// beside it, so the press need not land on a one-column line.
const BORDER_HIT_WIDTH: u16 = 2;

/// Two clicks on the border this close together are a double-click.
const DOUBLE_CLICK: Duration = Duration::from_millis(400);

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
    /// The width the user dragged the docked panel to, for this process only; `None`
    /// follows [`auto_width`]. Clamped to the terminal at layout, so it returns when a
    /// terminal that shrank grows back.
    dragged_width: Option<u16>,
    resize: Option<Resize>,
    /// When the last click on the border that resized nothing was released.
    last_border_click: Option<Instant>,
    files: Option<FileSummary>,
    /// The overlay's keyboard focus, when it has moved off the first row it can act
    /// on. Clamped to what the panel shows, since queues drain and files refresh.
    cursor: Option<Cursor>,
    /// How many of each section's focusable rows the last frame drew, by
    /// `Section::index`: a short panel cuts off its last sections, and focus must
    /// not land where the user cannot see it. `None` before the first frame.
    drawn: Cell<Option<[usize; 3]>>,
}

/// A drag of the docked panel's border in progress.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Resize {
    /// How far right of the border the press landed, kept while dragging so the
    /// border does not jump to the pointer.
    grab: u16,
    /// Whether the pointer has moved since the press: a press and release in place
    /// is a click, which can make a double-click.
    moved: bool,
}

/// A section of the overlay that focus can land on. Each opens the view it
/// summarizes; the turn list has nowhere to go yet, so focus skips it.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Section {
    Queue,
    Files,
    Mcp,
}

impl Section {
    fn index(self) -> usize {
        self as usize
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Cursor {
    section: Section,
    row: usize,
}

/// What Enter on a focused row opens.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum Target {
    /// `/diff`, at this file when one is named.
    Diff(Option<String>),
    /// The card of the session's latest edit or write call on a file, or `/diff` at
    /// the file when that card is not in the transcript.
    Edit(EditCard),
    Queue,
    Mcp,
}

/// What a key in the overlay asks of the app.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum OverlayKey {
    Close,
    /// Close the overlay and open this view.
    Open(Target),
    /// Focus moved; repaint.
    Moved,
    Ignored,
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

    /// Ctrl+B: hide or show the docked panel, or open or close the overlay. An
    /// overlay opens with focus on the first row it can act on.
    pub(crate) fn toggle(&mut self) {
        if docks(self.width) {
            self.hidden = !self.hidden;
        } else {
            self.overlay = !self.overlay;
            self.cursor = None;
            self.drawn.set(None);
        }
    }

    /// A key while the overlay is open: Tab and Shift+Tab move between the sections
    /// that open a view, arrows move within one, Enter opens what is focused, and Esc
    /// closes the overlay. The docked panel takes no keys, so typing stays with the
    /// composer.
    pub(crate) fn key(&mut self, key: KeyCode, state: &UiState) -> OverlayKey {
        if key == KeyCode::Esc {
            return OverlayKey::Close;
        }
        let sections = self.sections(state);
        let Some(cursor) = self.focused(state) else {
            return OverlayKey::Ignored;
        };
        let rows = self.targets(cursor.section, state).len();
        let position = sections
            .iter()
            .position(|section| *section == cursor.section)
            .expect("a focused section is listed");
        let moved = match key {
            KeyCode::Tab => Cursor {
                section: sections[(position + 1) % sections.len()],
                row: 0,
            },
            KeyCode::BackTab => Cursor {
                section: sections[(position + sections.len() - 1) % sections.len()],
                row: 0,
            },
            KeyCode::Up => Cursor {
                row: cursor.row.saturating_sub(1),
                ..cursor
            },
            KeyCode::Down => Cursor {
                row: (cursor.row + 1).min(rows - 1),
                ..cursor
            },
            KeyCode::Home => Cursor { row: 0, ..cursor },
            KeyCode::End => Cursor {
                row: rows - 1,
                ..cursor
            },
            KeyCode::Enter => {
                let target = self.targets(cursor.section, state).swap_remove(cursor.row);
                return OverlayKey::Open(target);
            }
            _ => return OverlayKey::Ignored,
        };
        self.cursor = Some(moved);
        OverlayKey::Moved
    }

    /// The sections focus can land on, in the order the panel draws them.
    fn sections(&self, state: &UiState) -> Vec<Section> {
        [Section::Queue, Section::Files, Section::Mcp]
            .into_iter()
            .filter(|section| !self.targets(*section, state).is_empty())
            .collect()
    }

    /// What each of a section's focusable rows opens, one per row the last frame
    /// drew.
    fn targets(&self, section: Section, state: &UiState) -> Vec<Target> {
        let mut targets = self.section_targets(section, state);
        if let Some(drawn) = self.drawn.get() {
            targets.truncate(drawn[section.index()]);
        }
        targets
    }

    /// What each of a section's focusable rows opens, one per row it draws when the
    /// panel has room for all of them.
    fn section_targets(&self, section: Section, state: &UiState) -> Vec<Target> {
        match section {
            // The queue is drawn only while a run is going or waits on a decision.
            Section::Queue if Focus::of(state) == Focus::Idle => Vec::new(),
            Section::Queue => {
                let queued = state.queued_steering() + state.queued_follow_ups();
                // The listed rows, and the row counting the rest.
                let rows = queued.min(MAX_QUEUE_ROWS) + usize::from(queued > MAX_QUEUE_ROWS);
                vec![Target::Queue; rows]
            }
            Section::Files => {
                let Some(summary) = &self.files else {
                    return Vec::new();
                };
                let count = summary.snapshot.files.len();
                let mut targets: Vec<_> = summary
                    .rows
                    .iter()
                    .map(|row| match &row.edit {
                        Some(edit) => Target::Edit(EditCard::of(edit, &row.git_path)),
                        None => Target::Diff(Some(row.git_path.clone())),
                    })
                    .collect();
                // The row counting the rest, or the one saying a partial snapshot
                // omitted every file, opens /diff where it starts.
                if count > MAX_FILE_ROWS || (count == 0 && summary.snapshot.truncated) {
                    targets.push(Target::Diff(None));
                }
                targets
            }
            Section::Mcp => state
                .mcp
                .snapshot
                .as_ref()
                .filter(|snapshot| !snapshot.servers.is_empty())
                .map(|_| vec![Target::Mcp])
                .unwrap_or_default(),
        }
    }

    /// The focused row: the cursor clamped to what the panel shows now, or the first
    /// row of the first section before focus has moved.
    fn focused(&self, state: &UiState) -> Option<Cursor> {
        let sections = self.sections(state);
        let section = self
            .cursor
            .map(|cursor| cursor.section)
            .filter(|section| sections.contains(section))
            .or_else(|| sections.first().copied())?;
        let rows = self.targets(section, state).len();
        let row = self
            .cursor
            .filter(|cursor| cursor.section == section)
            .map_or(0, |cursor| cursor.row.min(rows - 1));
        Some(Cursor { section, row })
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

    /// The docked panel's width on a terminal `total` columns wide, its border included:
    /// the dragged width, kept to between [`PANEL_WIDTH`] and what leaves the transcript
    /// its minimum, or [`auto_width`] when nothing was dragged.
    pub(crate) fn docked_width(&self, total: u16) -> u16 {
        self.dragged_width.map_or_else(
            || auto_width(total),
            |width| {
                width
                    .min(total.saturating_sub(MIN_TRANSCRIPT_WIDTH))
                    .max(PANEL_WIDTH)
            },
        )
    }

    /// Split a frame into the conversation and the docked panel at its right.
    pub(crate) fn split(&self, area: Rect) -> (Rect, Rect) {
        let width = self.docked_width(area.width).min(area.width);
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

    /// Whether a drag of the border is in progress, which highlights it.
    pub(crate) fn resizing(&self) -> bool {
        self.resize.is_some()
    }

    /// A left press on the docked panel's border at `now`, `grab` columns right of the
    /// border line: start a resize, or, as the second click of a double-click, return
    /// the panel to [`auto_width`] instead.
    pub(crate) fn press_border(&mut self, now: Instant, grab: u16) {
        let double_click = self
            .last_border_click
            .take()
            .is_some_and(|click| now.saturating_duration_since(click) <= DOUBLE_CLICK);
        if double_click {
            self.dragged_width = None;
            self.resize = None;
        } else {
            self.resize = Some(Resize { grab, moved: false });
        }
    }

    /// A press anywhere ends a resize whose release was lost, so the next drag selects
    /// text instead of moving the border. Says whether one ended, which repaints the
    /// border.
    pub(crate) fn cancel_resize(&mut self) -> bool {
        self.resize.take().is_some()
    }

    /// A press off the border: the next border click starts a new double-click.
    pub(crate) fn forget_border_click(&mut self) {
        self.last_border_click = None;
    }

    /// Follow a drag or release while a resize is in progress, and say whether it was
    /// one. Both move the border with the pointer at `column`, keeping where it was
    /// grabbed: a fast drag or sparse motion reports can leave the release as the only
    /// event at the final column. A release ends the resize, at `now`, and counts as a
    /// click only when the border never moved.
    pub(crate) fn continue_resize(
        &mut self,
        kind: MouseEventKind,
        column: u16,
        now: Instant,
    ) -> bool {
        let Some(resize) = self.resize else {
            return false;
        };
        // The terminal shrank below docking mid-drag: drags do nothing, and the
        // release still ends the resize.
        let docked = self.docked();
        let total = self.width;
        let held_at = total.saturating_sub(self.docked_width(total)) + resize.grab;
        let moved = resize.moved || (docked && column != held_at);
        if moved && docked {
            let width = total.saturating_sub(column.saturating_sub(resize.grab));
            self.dragged_width = Some(
                width
                    .min(total.saturating_sub(MIN_TRANSCRIPT_WIDTH))
                    .max(PANEL_WIDTH),
            );
        }
        if matches!(kind, MouseEventKind::Up(_)) {
            self.last_border_click = (!moved).then_some(now);
            self.resize = None;
        } else {
            self.resize = Some(Resize { moved, ..resize });
        }
        true
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

/// The docked panel's width on a terminal `total` columns wide when its border was
/// not dragged, its border included. The panel and the transcript share the columns
/// beyond the docking threshold evenly, up to [`MAX_PANEL_WIDTH`]: 160 columns give
/// 56, 180 give 66, and 200 or more give 76.
pub(crate) fn auto_width(total: u16) -> u16 {
    let spare = total.saturating_sub(PANEL_WIDTH + MIN_TRANSCRIPT_WIDTH);
    (PANEL_WIDTH + spare / 2).min(MAX_PANEL_WIDTH)
}

/// The cells of a docked panel at `panel` where a left press starts a resize.
pub(crate) fn border_hit_area(panel: Rect) -> Rect {
    Rect {
        width: BORDER_HIT_WIDTH.min(panel.width),
        ..panel
    }
}

#[derive(Debug)]
struct FileSummary {
    snapshot: Arc<GitDiffSnapshot>,
    /// The listed files only: the rest are counted, not diffed, so a large
    /// working tree cannot stall a frame.
    rows: Vec<FileRow>,
    /// How many of all the snapshot's files a Wisp edit in this session changed.
    by_wisp: usize,
}

#[derive(Debug)]
struct FileRow {
    status: char,
    /// The path as drawn, made safe for the terminal.
    path: String,
    /// The path as Git reported it, which `/diff` looks the file up by.
    git_path: String,
    additions: usize,
    deletions: usize,
    /// Whether line counts are known: an untracked file, or one Git could not read,
    /// shows none.
    counted: bool,
    /// The session's latest edit or write call on the file; `None` when the file
    /// changed only outside this session.
    edit: Option<GitDiffEdit>,
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
                    git_path: file.path.clone(),
                    additions,
                    deletions,
                    counted,
                    edit: file.latest_edit().cloned(),
                }
            })
            .collect();
        let by_wisp = snapshot
            .files
            .iter()
            .filter(|file| file.edited_by_wisp())
            .count();
        Self {
            snapshot,
            rows,
            by_wisp,
        }
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
        // A drag of the border shows it held: the terminal reports no hover to
        // highlight it before the press.
        let border = if panel.resizing() {
            palette.primary
        } else {
            palette.rail()
        };
        Block::default()
            .borders(Borders::LEFT)
            .border_style(Style::default().fg(border))
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
    let tree_key = ui::primary_label(bindings, KeyAction::SessionTree);
    let hint = if overlay {
        "Esc close · Tab sections · Enter open".to_owned()
    } else {
        format!("{toggle_key} hide panel")
    };
    // The docked panel names the session tree's key at its right, as the mock does.
    let tree_hint = (!overlay && tree_key != "Unbound").then(|| format!("{tree_key} session tree"));
    let focus = Focus::of(state);
    let mut sections = vec![header_lines(state, bindings, width, palette)];
    // Where each focusable section sits in `sections`, and the row its focusable
    // rows start at.
    let mut focusable: Vec<(Section, usize, usize)> = Vec::new();
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
            if let Some(queue) = queue_lines(state, false, bindings, width, palette) {
                focusable.push((Section::Queue, sections.len(), 1));
                sections.push(queue);
            }
        }
        Focus::Approval => {
            sections.extend(approval_lines(state, width, palette));
            if let Some(queue) = queue_lines(state, true, bindings, width, palette) {
                focusable.push((Section::Queue, sections.len(), 1));
                sections.push(queue);
            }
        }
    }
    let context_detail = match focus {
        Focus::Idle if overlay => ContextDetail::Overlay,
        Focus::Idle => ContextDetail::Full,
        Focus::Running | Focus::Approval => ContextDetail::Compact,
    };
    let context_index = sections.len();
    sections.extend([
        context_lines(state, context_detail, width, palette),
        model_lines(state, focus, width, palette),
    ]);
    focusable.push((Section::Files, sections.len(), 1));
    sections.push(file_lines(state, panel, overlay, width, palette));
    let mcp = mcp_line(state, width, palette);
    let fixed = |sections: &[Vec<Line<'static>>]| {
        sections.iter().map(|lines| lines.len() + 1).sum::<usize>()
            + mcp.as_ref().map_or(0, |_| 2)
            + 1
    };
    if context_detail != ContextDetail::Compact && fixed(&sections) + 1 > height {
        // Too short for the split's rows: keep the changed files and MCP in view.
        sections[context_index] = context_lines(state, ContextDetail::Compact, width, palette);
    }
    let turns = if focus == Focus::Idle {
        // The turn list takes what the other sections leave, keeping its newest turns.
        turn_lines(
            state,
            width,
            height.saturating_sub(fixed(&sections) + 1),
            palette,
        )
    } else {
        folded_turn_lines(state, focus, width, palette)
    };
    if !turns.is_empty() {
        sections.push(turns);
    }
    if let Some(mcp) = mcp {
        focusable.push((Section::Mcp, sections.len(), 0));
        sections.push(vec![mcp]);
    }

    let mut lines = Vec::new();
    let mut starts = Vec::with_capacity(sections.len());
    for section in sections {
        if !lines.is_empty() {
            lines.push(Line::default());
        }
        starts.push(lines.len());
        lines.extend(section);
    }
    lines.truncate(height.saturating_sub(1));

    // Record what fits before marking focus, so the mark and the keys agree on it.
    let mut drawn = [0; 3];
    for &(section, index, first_row) in &focusable {
        let rows = panel.section_targets(section, state).len();
        drawn[section.index()] = rows.min(lines.len().saturating_sub(starts[index] + first_row));
    }
    panel.drawn.set(Some(drawn));
    // Only the overlay takes keys, so only it marks a focused row.
    if let Some(cursor) = overlay.then(|| panel.focused(state)).flatten() {
        if let Some(&(_, index, first_row)) = focusable
            .iter()
            .find(|(section, ..)| *section == cursor.section)
        {
            mark_focus(
                &mut lines[starts[index]..],
                first_row + cursor.row,
                width,
                palette,
            );
        }
    }
    let hint_row = Rect {
        y: inner.bottom() - 1,
        height: 1,
        ..inner
    };
    frame.render_widget(Paragraph::new(lines), inner);
    let hint_line = match tree_hint {
        Some(tree_hint) => spread(
            ui::keycap_hints(&hint, palette).spans,
            ui::keycap_hints(&tree_hint, palette).spans,
            width,
        ),
        None => ui::keycap_hints(&hint, palette),
    };
    frame.render_widget(
        Paragraph::new(hint_line).style(Style::default().fg(palette.muted)),
        hint_row,
    );
}

/// A row with `left` at the start and `right` against the end; the left side is
/// clipped to leave room for the right.
pub(crate) fn spread(
    left: Vec<Span<'static>>,
    right: Vec<Span<'static>>,
    width: usize,
) -> Line<'static> {
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

/// `spans` cut to `width` columns, clipping the span that crosses it.
pub(crate) fn clip_spans(spans: Vec<Span<'static>>, width: usize) -> Vec<Span<'static>> {
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

/// Highlight the focused row across the panel's `width`, and its section's heading,
/// the first span of the section's first line.
fn mark_focus(lines: &mut [Line<'static>], row: usize, width: usize, palette: Palette) {
    if let Some(heading) = lines.first_mut().and_then(|line| line.spans.first_mut()) {
        heading.style = heading.style.fg(palette.primary);
    }
    if let Some(line) = lines.get_mut(row) {
        // Pad the row so the highlight spans the panel, as a list selection does.
        let padding = width.saturating_sub(line.width());
        line.spans.push(Span::raw(" ".repeat(padding)));
        *line = std::mem::take(line).patch_style(palette.selection());
    }
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
fn header_lines(
    state: &UiState,
    bindings: &Bindings,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
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
    if let Some(branch) = state
        .context
        .stats
        .as_ref()
        .and_then(|stats| branch_text(&stats.branch))
    {
        let key = match ui::primary_label(bindings, KeyAction::SessionTree).as_str() {
            "Unbound" => "/tree".to_owned(),
            key => key.to_owned(),
        };
        lines.push(spread(
            vec![
                Span::styled("⑂ ", Style::default().fg(palette.muted)),
                Span::styled(branch, Style::default().fg(palette.muted)),
            ],
            vec![Span::styled(key, palette.keycap())],
            width,
        ));
    }
    lines
}

/// Where the active path sits in the session tree: `branched from turn 4 · 2
/// siblings`, or `root · 2 branches` on the original line. A session that never
/// branched has no line.
fn branch_text(branch: &SessionBranchSummary) -> Option<String> {
    let plural = |count: u64, one: &str, many: &str| {
        format!("{count} {}", if count == 1 { one } else { many })
    };
    match branch.branched_from_turn {
        Some(0) => Some(format!(
            "branched from the start · {}",
            plural(branch.siblings, "sibling", "siblings")
        )),
        Some(turn) => Some(format!(
            "branched from turn {turn} · {}",
            plural(branch.siblings, "sibling", "siblings")
        )),
        None if branch.branches > 0 => Some(format!(
            "root · {}",
            plural(branch.branches, "branch", "branches")
        )),
        None => None,
    }
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
                Span::styled(item.label.clone(), palette.chip(&item.label)),
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
            Span::styled(label.clone(), palette.chip(&label)),
            Span::raw(" "),
            Span::raw(ui::clip_columns(
                &detail,
                width.saturating_sub(label.width() + 1),
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

/// How much of the context split the panel shows.
#[derive(Clone, Copy, PartialEq, Eq)]
enum ContextDetail {
    /// Every row, the sparkline, and the footnote: the docked panel at idle.
    Full,
    /// Every row without the sparkline or footnote: the Ctrl+B overlay.
    Overlay,
    /// The bar and one line about the turn: while a run goes, or when the panel is
    /// too short for the rows.
    Compact,
}

/// `Context  198.8k / 272.0k 73%` over the window split by source, with the
/// compaction reserve dashed after its marker, then as much of the split as
/// `detail` allows.
fn context_lines(
    state: &UiState,
    detail: ContextDetail,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    let muted = Style::default().fg(palette.muted);
    let Some(split) = Split::of(state) else {
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
    let prefix = if split.approximate { "~" } else { "" };
    let used = abbreviated(split.total);
    let Some(window) = split.window else {
        return vec![spread(
            vec![heading("Context", palette)],
            vec![Span::raw(format!("{prefix}{used}"))],
            width,
        )];
    };
    let fill = split.total as f64 / window as f64;
    let level = if fill >= ui::CONTEXT_WARNING_FILL {
        Style::default().fg(palette.warning)
    } else {
        muted
    };
    let mut lines = vec![
        spread(
            vec![heading("Context", palette)],
            vec![
                Span::raw(format!("{prefix}{used}")),
                Span::styled(format!(" / {} ", abbreviated(window)), muted),
                Span::styled(format!("{prefix}{:.0}%", fill * 100.0), level),
            ],
            width,
        ),
        context_split::bar(&split, width, palette),
    ];
    let Some(breakdown) = split.breakdown else {
        return lines;
    };
    if detail == ContextDetail::Compact {
        lines.extend(compact_turn_line(state, &split, width, palette));
        return lines;
    }
    lines.extend(source_rows(&split, breakdown, width, palette));
    let threshold = threshold_label(&split, palette);
    if detail == ContextDetail::Full && !breakdown.turns.is_empty() {
        lines.push(spread(sparkline(breakdown, palette), threshold, width));
    } else if !threshold.is_empty() {
        lines.push(spread(Vec::new(), threshold, width));
    }
    lines.extend(finished_turn_line(&split, breakdown, width, palette));
    if detail == ContextDetail::Full {
        let note = if breakdown.scaled {
            "~ estimated split, scaled to provider total"
        } else {
            "~ estimated split"
        };
        lines.push(spread(
            vec![Span::styled(note, Style::default().fg(palette.rail()))],
            vec![Span::styled("/context", muted)],
            width,
        ));
    }
    lines
}

/// A row per source with its share, the Messages parts under it, then the
/// compaction reserve and free space.
fn source_rows(
    split: &Split<'_>,
    breakdown: &ContextBreakdown,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    let muted = Style::default().fg(palette.muted);
    let dim = Style::default().fg(palette.rail());
    let percent = |tokens: u64| {
        split
            .percent(tokens)
            .map_or_else(String::new, |percent| format!("{percent:.1}%"))
    };
    let row = |swatch: Span<'static>, label: Vec<Span<'static>>, tokens: String, share: String| {
        let mut left = vec![swatch, Span::raw(" ")];
        left.extend(label);
        spread(
            left,
            vec![
                Span::styled(format!("{tokens:>8}"), dim),
                Span::styled(format!("{share:>7}"), muted),
            ],
            width,
        )
    };
    let mut lines = Vec::new();
    for (usage, (source, tokens)) in breakdown.sources.iter().zip(split.sources()) {
        let mut label = vec![Span::styled(context_split::source_label(source), muted)];
        if source == ContextSource::McpTools && !usage.children.is_empty() {
            let servers = usage.children.len();
            label.push(Span::styled(
                format!(" {servers} server{}", if servers == 1 { "" } else { "s" }),
                dim,
            ));
        }
        lines.push(row(
            Span::styled(
                "■",
                Style::default().fg(context_split::source_color(source, palette)),
            ),
            label,
            context_split::estimated(tokens),
            percent(tokens),
        ));
        if source == ContextSource::Messages && !usage.children.is_empty() {
            let mut parts: Vec<_> = usage.children.iter().collect();
            parts.sort_by(|a, b| b.tokens.cmp(&a.tokens));
            let text = parts
                .iter()
                .map(|part| {
                    format!(
                        "{} {}",
                        sanitize_for_terminal(&part.label),
                        abbreviated(part.tokens)
                    )
                })
                .collect::<Vec<_>>()
                .join(" · ");
            lines.push(Line::styled(
                ui::clip_columns(&format!("  {text}"), width),
                dim,
            ));
        }
    }
    if let Some(free) = split.free() {
        lines.push(row(
            Span::styled(
                context_split::RESERVE_CELL,
                Style::default().fg(palette.rail()),
            ),
            vec![Span::styled("Compaction reserve", dim)],
            abbreviated(split.reserve),
            percent(split.reserve),
        ));
        lines.push(row(
            Span::styled("■", Style::default().fg(palette.panel)),
            vec![Span::styled("Free space", dim)],
            split.free_label(free),
            percent(free),
        ));
    }
    lines
}

/// `▏compacts at 85%`, or nothing when the reserve leaves no threshold.
fn threshold_label(split: &Split<'_>, palette: Palette) -> Vec<Span<'static>> {
    split
        .threshold()
        .and_then(|threshold| split.percent(threshold))
        .map(|percent| {
            vec![
                Span::styled("▏", Style::default().fg(palette.error)),
                Span::styled(
                    format!("compacts at {percent:.0}%"),
                    Style::default().fg(palette.rail()),
                ),
            ]
        })
        .unwrap_or_default()
}

/// `per turn ▂▅▁█▃▄`: the newest turns' growth, the latest in the warning colour.
fn sparkline(breakdown: &ContextBreakdown, palette: Palette) -> Vec<Span<'static>> {
    let turns = &breakdown.turns[breakdown.turns.len().saturating_sub(SPARK_TURNS)..];
    let tokens: Vec<u64> = turns.iter().map(|turn| turn.tokens).collect();
    let blocks = context_split::spark(&tokens);
    let mut spans = vec![Span::styled(
        "per turn ",
        Style::default().fg(palette.muted),
    )];
    for (index, block) in blocks.iter().enumerate() {
        let color = if index + 1 == blocks.len() {
            palette.warning
        } else {
            palette.secondary
        };
        spans.push(Span::styled(*block, Style::default().fg(color)));
    }
    spans
}

/// `turn 6 +38.2k · largest: gh pr checks output 21.4k`, clipping the label
/// rather than the size.
fn finished_turn_line(
    split: &Split<'_>,
    breakdown: &ContextBreakdown,
    width: usize,
    palette: Palette,
) -> Option<Line<'static>> {
    let (turn, growth) = split.finished_turn()?;
    let dim = Style::default().fg(palette.rail());
    let mut spans = turn_growth(turn, growth, palette);
    // Until a finished run's statistics arrive, the largest items predate it.
    if let Some(largest) = breakdown.largest.first().filter(|_| split.growth == 0) {
        let size = format!(" {}", abbreviated(largest.tokens));
        let used: usize = spans.iter().map(|span| span.content.width()).sum();
        let label = format!(" · largest: {}", sanitize_for_terminal(&largest.label));
        let room = width.saturating_sub(used + size.width());
        spans.push(Span::styled(ui::clip_columns(&label, room), dim));
        spans.push(Span::styled(size, dim));
    }
    Some(Line::from(clip_spans(spans, width)))
}

/// The compact layout's line: what the run in progress has added, or the newest
/// turn at idle. A compaction is not a turn, so it shows neither.
fn compact_turn_line(
    state: &UiState,
    split: &Split<'_>,
    width: usize,
    palette: Palette,
) -> Option<Line<'static>> {
    let compacting = state.context.compaction.is_some()
        || state.current_command.as_ref().is_some_and(|command| {
            command.command_type == crate::reducer::ActiveCommandType::Compact
        });
    if compacting {
        return None;
    }
    if !ui::run_in_status_line(state) {
        let breakdown = split.breakdown?;
        return finished_turn_line(split, breakdown, width, palette);
    }
    let (turn, growth) = split.running_turn()?;
    let mut spans = turn_growth(turn, growth, palette);
    spans.push(Span::styled(" so far", Style::default().fg(palette.muted)));
    Some(Line::from(clip_spans(spans, width)))
}

fn turn_growth(turn: u64, growth: u64, palette: Palette) -> Vec<Span<'static>> {
    vec![
        Span::styled(format!("turn {turn} "), Style::default().fg(palette.muted)),
        Span::styled(
            format!("+{}", abbreviated(growth)),
            Style::default().fg(palette.warning),
        ),
    ]
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
    overlay: bool,
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
    let by_wisp = summary.by_wisp;
    let trailer = if by_wisp > 0 {
        let others = count - by_wisp;
        vec![
            Span::styled("◆", Style::default().fg(palette.primary)),
            Span::styled(
                format!(
                    " {by_wisp} by Wisp · {others}{} other",
                    if partial { "+" } else { "" }
                ),
                muted,
            ),
        ]
    } else {
        vec![Span::styled(
            match (count, partial) {
                (0, _) => String::new(),
                (1, false) => "1 file".to_owned(),
                (count, false) => format!("{count} files"),
                (count, true) => format!("{count}+ files"),
            },
            muted,
        )]
    };
    let mut lines = vec![spread(
        vec![heading("Changed files", palette)],
        trailer,
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
        // Once Wisp edited anything, every row gets the ◆ column, and a file it did
        // not edit reads as muted with no counts: it changed outside this session.
        let external = by_wisp > 0 && row.edit.is_none();
        let counts = if external {
            vec![Span::styled("not from Wisp", muted)]
        } else if row.counted {
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
        let mark_width = if by_wisp > 0 { 2 } else { 0 };
        let path_room =
            width.saturating_sub(mark_width + 2 + counts_width + usize::from(counts_width > 0));
        let mut path = Vec::new();
        if by_wisp > 0 {
            path.push(match row.edit {
                Some(_) => Span::styled("◆ ", Style::default().fg(palette.primary)),
                None => Span::raw("  "),
            });
        }
        if external {
            path.push(Span::styled(format!("{} ", row.status), muted));
            path.push(Span::styled(clip_path(&row.path, path_room), muted));
        } else {
            path.push(Span::styled(format!("{} ", row.status), status_style));
            path.push(Span::raw(clip_path(&row.path, path_room)));
        }
        lines.push(spread(path, counts, width));
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
    // Only the overlay takes keys, so only it offers the jump.
    if overlay && by_wisp > 0 {
        lines.push(spread(
            vec![Span::styled("Enter jumps to the edit", muted)],
            vec![Span::styled("/diff", palette.keycap())],
            width,
        ));
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
    use crossterm::event::MouseButton;
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use wisp_protocol::events::{
        ContextAccountingMethod, ContextBreakdown, ContextBudget, ContextEstimate, ContextItem,
        ContextSourceItem, ContextSourceUsage, ContextTurnGrowth, McpServerSnapshot,
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
            ..Default::default()
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
            context_breakdown: None,
            branch: Default::default(),
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
        screen_of_height(state, panel, width, 40)
    }

    fn screen_of_height(
        state: &UiState,
        panel: &mut SidePanel,
        width: u16,
        height: u16,
    ) -> Vec<String> {
        panel.observe_width(width);
        panel.sync_files(&state.git_diff);
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
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
        let start = usize::from(width - auto_width(width));
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
            .take(usize::from(200 - auto_width(200)))
            .collect();
        assert!(conversation.contains("idle ·"), "{conversation}");
        assert!(!conversation.contains("/srv/wisp"), "{conversation}");
        assert!(!conversation.contains("ctx"), "{conversation}");
    }

    #[test]
    fn docked_panel_grows_capped() {
        for (total, panel) in [(160, 56), (161, 56), (180, 66), (200, 76), (240, 76)] {
            assert_eq!(auto_width(total), panel, "{total}");
            let (conversation, docked) = SidePanel::default().split(Rect::new(0, 0, total, 10));
            assert_eq!((conversation.width, docked.width), (total - panel, panel));
        }
        // The overlay on a terminal too narrow to dock keeps the narrowest width.
        assert_eq!(overlay_area(Rect::new(0, 0, 150, 10)).width, PANEL_WIDTH);
    }

    const DRAG: MouseEventKind = MouseEventKind::Drag(MouseButton::Left);
    const RELEASE: MouseEventKind = MouseEventKind::Up(MouseButton::Left);

    #[test]
    fn dragged_width_clamps() {
        let now = Instant::now();
        let mut panel = SidePanel::default();
        panel.observe_width(200);
        panel.press_border(now, 0);
        assert!(panel.resizing());
        // The border follows the pointer: column 110 leaves 90 columns.
        assert!(panel.continue_resize(DRAG, 110, now));
        assert_eq!(panel.docked_width(200), 90);
        // Never below the narrowest width, and the transcript keeps its minimum.
        panel.continue_resize(DRAG, 190, now);
        assert_eq!(panel.docked_width(200), PANEL_WIDTH);
        panel.continue_resize(DRAG, 10, now);
        assert_eq!(panel.docked_width(200), 200 - MIN_TRANSCRIPT_WIDTH);
        panel.continue_resize(DRAG, 110, now);
        assert!(panel.continue_resize(RELEASE, 110, now));
        assert!(!panel.resizing());
        // A terminal that shrinks squeezes the width, and growing back restores it.
        assert_eq!(panel.docked_width(180), 76);
        assert_eq!(panel.docked_width(200), 90);
        // With no resize in progress, drags belong to someone else.
        assert!(!panel.continue_resize(DRAG, 120, now));
    }

    #[test]
    fn double_click_resets_width() {
        let start = Instant::now();
        let at = |millis| start + Duration::from_millis(millis);
        let mut panel = SidePanel::default();
        panel.observe_width(200);
        panel.press_border(at(0), 0);
        panel.continue_resize(DRAG, 110, at(0));
        panel.continue_resize(RELEASE, 110, at(0));
        // A drag's release is not a click, so a quick press after it resizes again.
        panel.press_border(at(100), 0);
        assert!(panel.resizing());
        panel.continue_resize(RELEASE, 110, at(150));
        // A slow second click is two clicks.
        panel.press_border(at(1_000), 0);
        assert!(panel.resizing());
        panel.continue_resize(RELEASE, 110, at(1_000));
        assert_eq!(panel.docked_width(200), 90);
        panel.press_border(at(1_200), 0);
        assert!(!panel.resizing());
        assert_eq!(panel.docked_width(200), auto_width(200));

        // A press elsewhere between two border clicks breaks the double-click.
        panel.press_border(at(2_000), 0);
        panel.continue_resize(DRAG, 110, at(2_000));
        panel.continue_resize(RELEASE, 110, at(2_000));
        panel.press_border(at(2_100), 0);
        panel.continue_resize(RELEASE, 110, at(2_100));
        panel.forget_border_click();
        panel.press_border(at(2_200), 0);
        assert!(panel.resizing());
        assert_eq!(panel.docked_width(200), 90);
    }

    #[test]
    fn drag_keeps_grab_offset() {
        let now = Instant::now();
        let mut panel = SidePanel::default();
        panel.observe_width(200);
        // Pressed one column right of the border at 124: moving down only keeps the width.
        panel.press_border(now, 1);
        panel.continue_resize(DRAG, 125, now);
        assert_eq!(panel.docked_width(200), 76);
        panel.continue_resize(DRAG, 111, now);
        assert_eq!(panel.docked_width(200), 90);
    }

    #[test]
    fn release_moves_border() {
        let start = Instant::now();
        let mut panel = SidePanel::default();
        panel.observe_width(200);
        // No drag reported between the press at the border and the release at 110.
        panel.press_border(start, 0);
        panel.continue_resize(RELEASE, 110, start);
        assert_eq!(panel.docked_width(200), 90);
        // That release moved the border, so it is not the first click of a double-click.
        panel.press_border(start + Duration::from_millis(100), 0);
        assert!(panel.resizing());
        assert_eq!(panel.docked_width(200), 90);
    }

    #[test]
    fn held_border_highlights() {
        let palette = Palette::default();
        let border = |panel: &SidePanel| {
            let mut terminal = Terminal::new(TestBackend::new(200, 20)).unwrap();
            terminal
                .draw(|frame| {
                    let area = panel.split(frame.area()).1;
                    let bindings = Bindings::default();
                    render(frame, area, &session(), panel, &bindings, 0, None, palette);
                })
                .unwrap();
            terminal.backend().buffer()[(200 - panel.docked_width(200), 5)].fg
        };
        let mut panel = SidePanel::default();
        panel.observe_width(200);
        assert_eq!(border(&panel), palette.rail());
        panel.press_border(Instant::now(), 0);
        assert_eq!(border(&panel), palette.primary);
    }

    #[test]
    fn wide_panel_fits_paths() {
        let path = "src/wisp/agent/harness/boundaries/overflow_recovery.rs";
        let mut state = session();
        state.git_diff.snapshot = Some(changes(vec![file(path, "added", "", "x\n")]));
        let narrow = panel_text(&screen(&state, &mut SidePanel::default(), 160), 160);
        assert!(!narrow.contains(path), "{narrow}");
        let wide = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(wide.contains(&format!("A {path}")), "{wide}");
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
    fn tree_line_follows_branch() {
        let line = |branch: SessionBranchSummary| {
            let mut state = session();
            state.context.stats.as_mut().unwrap().branch = branch;
            let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
            panel
                .lines()
                .find_map(|line| line.find('⑂').map(|start| line[start..].to_owned()))
        };
        // A root session that never branched has no line.
        assert_eq!(line(SessionBranchSummary::default()), None);
        let root = line(SessionBranchSummary {
            branches: 2,
            ..Default::default()
        })
        .unwrap();
        assert!(root.starts_with("⑂ root · 2 branches"), "{root}");
        assert!(root.ends_with("Ctrl+T"), "{root}");
        let branched = line(SessionBranchSummary {
            branched_from_turn: Some(4),
            siblings: 1,
            branches: 0,
        })
        .unwrap();
        assert!(
            branched.starts_with("⑂ branched from turn 4 · 1 sibling "),
            "{branched}"
        );
        let sibling = line(SessionBranchSummary {
            branched_from_turn: Some(4),
            siblings: 2,
            branches: 0,
        })
        .unwrap();
        assert!(
            sibling.starts_with("⑂ branched from turn 4 · 2 siblings"),
            "{sibling}"
        );
        let rows = screen(&session(), &mut SidePanel::default(), 200);
        let footer = panel_text(&rows, 200);
        assert!(footer.contains("Ctrl+T session tree"), "{footer}");
    }

    #[test]
    fn wisp_edits_get_marks() {
        let mut state = session();
        let mut edited = file("src/lib.rs", "modified", "a\n", "b\n");
        edited.edits = vec![crate::reducer::git_diff::GitDiffEdit {
            call_id: "edit-1".into(),
            entry_id: String::new(),
            tool: "edit".into(),
        }];
        state.git_diff.snapshot = Some(changes(vec![
            edited,
            file("src/other.rs", "modified", "a\n", "c\n"),
        ]));
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(panel.contains("◆ 1 by Wisp · 1 other"), "{panel}");
        assert!(panel.contains("◆ M src/lib.rs"), "{panel}");
        assert!(panel.contains("  M src/other.rs"), "{panel}");
        assert!(panel.contains("not from Wisp"), "{panel}");
        assert!(
            !panel.contains("Enter jumps"),
            "the docked panel takes no keys"
        );
        assert!(overlay_text(&state).contains("Enter jumps to the edit"));
        // Enter on the ◆ file jumps to its edit; on the other, it opens /diff.
        let mut panel = overlay(&state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Edit(EditCard {
                call_id: "edit-1".into(),
                entry_id: String::new(),
                path: "src/lib.rs".into(),
            }))
        );
        panel.key(KeyCode::Down, &state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Diff(Some("src/other.rs".into())))
        );
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

    struct Ids;
    impl crate::reducer::CommandIdSource for Ids {
        fn next_id(&mut self, kind: crate::reducer::CommandKind) -> String {
            format!("{}-1", kind.prefix())
        }
    }

    fn backend(state: &mut UiState, event: crate::reducer::BackendEvent) {
        crate::reducer::reduce(
            state,
            crate::reducer::UiAction::BackendEvent(event),
            &mut Ids,
        )
        .unwrap();
    }

    fn queue(state: &mut UiState, steering: &[&str], follow_up: &[&str]) {
        backend(
            state,
            crate::reducer::BackendEvent::QueueUpdated {
                steering: steering.iter().map(|&text| text.into()).collect(),
                follow_up: follow_up.iter().map(|&text| text.into()).collect(),
            },
        );
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

    /// The mocks' split: 198.8k of 272.0k, compacting at 85%.
    fn breakdown() -> ContextBreakdown {
        let item = |label: &str, tokens: u64, count: Option<u64>| ContextSourceItem {
            label: label.into(),
            tokens,
            count,
        };
        let source = |source, tokens, count, children| ContextSourceUsage {
            source,
            tokens,
            count,
            children,
        };
        ContextBreakdown {
            sources: vec![
                source(
                    ContextSource::Messages,
                    152_600,
                    Some(55),
                    vec![
                        item("user", 16_400, Some(8)),
                        item("assistant", 41_000, Some(6)),
                        item("tool output", 95_200, Some(41)),
                    ],
                ),
                source(ContextSource::BuiltInTools, 9_800, Some(12), Vec::new()),
                source(
                    ContextSource::McpTools,
                    14_100,
                    Some(38),
                    vec![
                        item("github", 7_900, Some(21)),
                        item("linear", 3_600, Some(11)),
                        item("deepwiki", 1_400, Some(3)),
                        item("docs-langchain", 1_200, Some(3)),
                    ],
                ),
                source(ContextSource::SystemPrompt, 6_200, None, Vec::new()),
                source(ContextSource::Skills, 2_400, None, Vec::new()),
                source(
                    ContextSource::ProjectInstructions,
                    8_700,
                    Some(2),
                    vec![
                        item("AGENTS.md", 7_900, None),
                        item("rust/AGENTS.md", 800, None),
                    ],
                ),
                source(ContextSource::ProjectContext, 5_000, None, Vec::new()),
            ],
            scaled: true,
            turns: [22_100, 41_300, 12_000, 55_600, 29_600, 38_200]
                .into_iter()
                .zip(1..)
                .map(|(tokens, turn)| ContextTurnGrowth { turn, tokens })
                .collect(),
            largest: vec![ContextItem {
                label: "gh pr checks 766 output".into(),
                tokens: 21_400,
                turn: 6,
            }],
        }
    }

    /// `state` with the mocks' split and a reserve that compacts at 85%.
    fn with_split(mut state: UiState) -> UiState {
        let mut budget = budget();
        budget.reserve_tokens = 40_800;
        state.context.budget = Some(budget.clone());
        let stats = state.context.stats.as_mut().unwrap();
        stats.context = budget;
        stats.context_breakdown = Some(breakdown());
        state
    }

    fn row_of(panel: &str, text: &str) -> usize {
        panel
            .lines()
            .position(|line| line.contains(text))
            .unwrap_or_else(|| panic!("{text}\n{panel}"))
    }

    #[test]
    fn idle_context_split_rows() {
        // The narrowest docked panel, so the bar is 52 columns.
        let panel = panel_text(
            &screen(&with_split(session()), &mut SidePanel::default(), 160),
            160,
        );
        let bar = panel.lines().nth(row_of(&panel, "Context") + 1).unwrap();
        let bar = bar.trim_start_matches(['│', ' ']);
        // 85% of the 52-column bar: the marker, then the reserve's dashed rule.
        assert_eq!(bar.chars().position(|c| c == '┃'), Some(44), "{panel}");
        assert!(bar.ends_with("┃╍╍╍╍╍╍╍"), "{panel}");
        let mut previous = row_of(&panel, "Context");
        for expected in [
            "■ Messages                            ~152.6k  56.1%",
            "tool output 95.2k · assistant 41.0k · user 16.4k",
            "■ MCP tools 4 servers                  ~14.1k   5.2%",
            "■ Project context                       ~5.0k   1.8%",
            "╍ Compaction reserve                    40.8k  15.0%",
            // An exact total leaves free space exact.
            "■ Free space                            32.4k  11.9%",
            "per turn ▄▆▂█▅▆                     ▏compacts at 85%",
            "turn 6 +38.2k · largest: gh pr checks 766 out… 21.4k",
            "~ estimated split, scaled to provider total /context",
        ] {
            let row = row_of(&panel, expected);
            assert!(row > previous, "{expected}\n{panel}");
            previous = row;
        }
    }

    #[test]
    fn overlay_context_drops_sparkline() {
        let panel = overlay_text(&with_split(session()));
        assert!(panel.contains("■ Skills"), "{panel}");
        assert!(panel.contains("turn 6 +38.2k"), "{panel}");
        assert!(!panel.contains("per turn"), "{panel}");
        assert!(!panel.contains("/context"), "{panel}");
    }

    #[test]
    fn running_context_is_compact() {
        let mut state = with_split(running());
        let budget = state.context.budget.as_mut().unwrap();
        budget.observed_tokens = Some(210_000);
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(panel.contains("210.0k / 272.0k 77%"), "{panel}");
        assert!(panel.contains("turn 7 +11.2k so far"), "{panel}");
        assert!(!panel.contains("Messages"), "{panel}");
    }

    #[test]
    fn short_panel_compacts_context() {
        let rows = screen_of_height(&with_split(session()), &mut SidePanel::default(), 200, 30);
        let panel = panel_text(&rows, 200);
        // Too short for every source row: the bar and the newest turn keep the
        // changed files in view.
        assert!(!panel.contains("■ Messages"), "{panel}");
        assert!(panel.contains("turn 6 +38.2k"), "{panel}");
        assert!(panel.contains("M src/lib.rs"), "{panel}");
    }

    #[test]
    fn compaction_drops_stale_split() {
        let mut state = with_split(running());
        // A compaction during the run: the next request is far smaller.
        state.context.budget.as_mut().unwrap().observed_tokens = Some(60_000);
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(panel.contains("60.0k / 272.0k 22%"), "{panel}");
        assert!(!panel.contains("so far"), "{panel}");
    }

    #[test]
    fn finished_run_awaits_stats() {
        let mut state = with_split(session());
        state.context.budget.as_mut().unwrap().observed_tokens = Some(210_000);
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(panel.contains("turn 7 +11.2k"), "{panel}");
        assert!(!panel.contains("largest"), "{panel}");
    }

    #[test]
    fn compaction_is_not_a_turn() {
        let mut state = with_split(running());
        state.context.compaction = Some(wisp_protocol::events::CompactionReason::Manual);
        let split = Split::of(&state).unwrap();
        assert!(compact_turn_line(&state, &split, 52, Palette::default()).is_none());
    }

    #[test]
    fn context_without_split() {
        let mut state = with_split(session());
        state.context.stats.as_mut().unwrap().context_breakdown = None;
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 160), 160);
        // Older statistics have no split: one run, the reserve after its marker.
        let bar = panel.lines().nth(row_of(&panel, "Context") + 1).unwrap();
        let bar = bar.trim_start_matches(['│', ' ']);
        assert!(bar.starts_with('━') && bar.ends_with("┃╍╍╍╍╍╍╍"), "{panel}");
        assert!(!panel.contains("Messages"), "{panel}");
    }

    #[test]
    fn running_leads_with_now() {
        let panel = panel_text(&screen(&running(), &mut SidePanel::default(), 200), 200);
        for expected in [
            "working",
            "Bash git push -u origin fix/timeouts",
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
            "Bash git push -u origin fix/timeouts",
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
        panel.sync_files(&state.git_diff);
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
        assert!(
            panel.contains("Esc close · Tab sections · Enter open"),
            "{panel}"
        );
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

    /// The overlay open on `state` at a normal width, its files counted.
    fn overlay(state: &UiState) -> SidePanel {
        let mut panel = SidePanel::default();
        panel.observe_width(140);
        panel.toggle();
        panel.sync_files(&state.git_diff);
        panel
    }

    #[test]
    fn tab_cycles_sections() {
        let mut state = running();
        queue(&mut state, &["steer"], &["later"]);
        let mut panel = overlay(&state);
        let mut press = |key| panel.key(key, &state);
        // Focus starts on the queue, the first section that opens a view.
        assert_eq!(press(KeyCode::Enter), OverlayKey::Open(Target::Queue));
        assert_eq!(press(KeyCode::Tab), OverlayKey::Moved);
        assert_eq!(
            press(KeyCode::Enter),
            OverlayKey::Open(Target::Diff(Some("src/lib.rs".into())))
        );
        press(KeyCode::Down);
        press(KeyCode::Down);
        assert_eq!(
            press(KeyCode::Enter),
            OverlayKey::Open(Target::Diff(Some("notes.md".into())))
        );
        press(KeyCode::Tab);
        assert_eq!(press(KeyCode::Enter), OverlayKey::Open(Target::Mcp));
        // Tab wraps, and Shift+Tab goes back.
        press(KeyCode::Tab);
        assert_eq!(press(KeyCode::Enter), OverlayKey::Open(Target::Queue));
        press(KeyCode::BackTab);
        assert_eq!(press(KeyCode::Enter), OverlayKey::Open(Target::Mcp));
        assert_eq!(press(KeyCode::Char('x')), OverlayKey::Ignored);
        assert_eq!(press(KeyCode::Esc), OverlayKey::Close);
    }

    #[test]
    fn focus_skips_empty_sections() {
        // At idle the queue is not drawn, so focus starts on the changed files.
        let mut state = session();
        queue(&mut state, &["steer"], &[]);
        let mut panel = overlay(&state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Diff(Some("src/lib.rs".into())))
        );
        // Focus left on a section that empties moves to the first one still drawn.
        panel.key(KeyCode::Tab, &state);
        assert_eq!(panel.focused(&state).unwrap().section, Section::Mcp);
        state.mcp.snapshot = None;
        assert_eq!(panel.focused(&state).unwrap().section, Section::Files);
        // The row counting the rest of a long list opens /diff where it starts.
        state.git_diff.snapshot = Some(changes(
            (0..10)
                .map(|index| file(&format!("f{index}.rs"), "added", "", "x\n"))
                .collect(),
        ));
        let mut panel = overlay(&state);
        panel.key(KeyCode::End, &state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Diff(None))
        );
    }

    /// The background of the middle of the panel's row that contains `text`.
    fn row_background(
        panel: &SidePanel,
        state: &UiState,
        width: u16,
        text: &str,
    ) -> Option<ratatui::style::Color> {
        let mut terminal = Terminal::new(TestBackend::new(width, 40)).unwrap();
        terminal
            .draw(|frame| {
                let area = if panel.docked() {
                    panel.split(frame.area()).1
                } else {
                    overlay_area(frame.area())
                };
                render(
                    frame,
                    area,
                    state,
                    panel,
                    &Bindings::default(),
                    0,
                    None,
                    Palette::default(),
                );
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        let y = (0..buffer.area.height)
            .find(|&y| {
                (0..width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
                    .contains(text)
            })
            .unwrap_or_else(|| panic!("{text}"));
        buffer[(width - panel.docked_width(width) / 2, y)]
            .style()
            .bg
    }

    #[test]
    fn overflow_rows_take_focus() {
        let selection = Palette::default().selection().bg;
        let mut state = running();
        let follow_ups: Vec<String> = (1..=6).map(|index| format!("later {index}")).collect();
        let follow_ups: Vec<&str> = follow_ups.iter().map(String::as_str).collect();
        queue(&mut state, &[], &follow_ups);
        state.git_diff.snapshot = Some(changes(
            (0..10)
                .map(|index| file(&format!("f{index}.rs"), "added", "", "x\n"))
                .collect(),
        ));
        let mut panel = overlay(&state);
        // The queue's row counting the rest is the last one focus reaches.
        panel.key(KeyCode::End, &state);
        assert_eq!(
            row_background(&panel, &state, 140, "+1 more · /queue"),
            selection
        );
        assert_ne!(row_background(&panel, &state, 140, "later 5"), selection);
        panel.key(KeyCode::Up, &state);
        assert_eq!(row_background(&panel, &state, 140, "later 5"), selection);
        // So is the file list's.
        panel.key(KeyCode::Tab, &state);
        panel.key(KeyCode::End, &state);
        assert_eq!(
            row_background(&panel, &state, 140, "+2 more · /diff"),
            selection
        );
        // The panel is too short for the MCP line, so Tab skips it.
        assert_eq!(panel.drawn.get().unwrap()[Section::Mcp.index()], 0);
        panel.key(KeyCode::Tab, &state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Queue)
        );
    }

    #[test]
    fn diff_gets_git_path() {
        let mut state = session();
        state.git_diff.snapshot = Some(changes(vec![file("a\tb.rs", "modified", "a\n", "b\n")]));
        let mut panel = overlay(&state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Diff(Some("a\tb.rs".into())))
        );
    }

    #[test]
    fn overlay_marks_focus() {
        let state = session();
        let selection = Palette::default().selection().bg;
        let panel = overlay(&state);
        assert_eq!(
            row_background(&panel, &state, 140, "M src/lib.rs"),
            selection
        );
        assert_ne!(row_background(&panel, &state, 140, "? notes.md"), selection);
        // The MCP line takes focus as a whole.
        let mut panel = panel;
        panel.key(KeyCode::Tab, &state);
        assert_eq!(
            row_background(&panel, &state, 140, "2/2 connected"),
            selection
        );
        // The docked panel takes no keys, so it marks nothing.
        let mut docked = SidePanel::default();
        docked.observe_width(200);
        docked.sync_files(&state.git_diff);
        assert_ne!(
            row_background(&docked, &state, 200, "M src/lib.rs"),
            selection
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
    fn steered_run_keeps_total() {
        let priced = |nano_usd| {
            Some(crate::web_search::MessageCost {
                label: String::new(),
                nano_usd: Some(nano_usd),
                lower_bound: false,
            })
        };
        let mut state = session();
        let prompt = state.transcript.append_prompt("push it".into());
        state.run_progress = Some(crate::reducer::RunProgress { prompt, turn: 3 });
        state
            .transcript
            .complete_message_with_cost(3, "Pushing.".into(), priced(210_000_000));
        // A steer the run took in reads as a prompt of its own.
        state.transcript.append_prompt("also open the PR".into());
        state
            .transcript
            .complete_message_with_cost(4, "Opened.".into(), priced(100_000_000));
        backend(
            &mut state,
            crate::reducer::BackendEvent::RunCompleted(crate::transcript::RunTotals {
                outcome: "completed".into(),
                duration_ms: Some(9_000),
                tool_calls: 2,
                ..Default::default()
            }),
        );
        let cost = |state: &UiState| crate::context_view::turn_cost(state);
        assert_eq!(cost(&state).as_deref(), Some("$0.310"));
        // A history sync replaces the transcript; the run keeps its total.
        state.transcript = Default::default();
        assert_eq!(cost(&state).as_deref(), Some("$0.310"));
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
