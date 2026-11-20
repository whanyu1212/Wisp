//! The side panel: live work (plan, running subagents and background processes,
//! approvals, queue, attention, context, and a one-line inventory summary), beside
//! the transcript on a wide terminal or over it as an overlay opened with Ctrl+P.
//!
//! The panel only reads state. The reducer keeps its data loaded while it is on
//! screen (see `UiAction::SetSidePanelVisible`); this module decides where it goes
//! and draws it.

use std::cell::{Cell, RefCell};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::context_split::Split;
use crate::keybindings::{Action as KeyAction, Bindings};
use crate::reducer::git_diff::{GitDiff, GitDiffSnapshot};
use crate::reducer::{BrowserViewport, UiState};
use crate::terminal_images::{CellSize, ImagePreviewMeta, PaintedImage};
use crate::theme::Palette;
use crate::transcript::TranscriptRole;
use crate::ui::{self, sanitize_for_terminal};
use crossterm::event::{KeyCode, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Padding, Paragraph};
use unicode_width::UnicodeWidthStr;
use wisp_protocol::commands::{CodemodePresentation, CodemodeSetting, PermissionMode};
use wisp_protocol::events::{McpServerStatus, SessionBranchSummary};

/// The panel's width, its border included: the overlay's width, and the docked
/// panel's on the narrowest terminal that docks it.
pub(crate) const PANEL_WIDTH: u16 = 44;

/// The widest a docked panel grows on its own, so long paths and names fit without
/// the panel becoming a second transcript. Dragging its border can go wider.
const MAX_PANEL_WIDTH: u16 = 52;

/// Columns from the panel's left edge that start a resize: the edge and the gap
/// beside it, so the press need not land on a one-column line.
const BORDER_HIT_WIDTH: u16 = 2;

/// Two clicks on the border this close together are a double-click.
const DOUBLE_CLICK: Duration = Duration::from_millis(400);

/// What the transcript keeps beside a docked panel: replies wrap at 100 columns,
/// plus the turn rail and the conversation margins.
const MIN_TRANSCRIPT_WIDTH: u16 = 104;

/// Minimum transcript width preserved when the user explicitly drags the docked panel wider.
const MIN_DRAGGED_TRANSCRIPT_WIDTH: u16 = 60;

/// Maximum share of terminal width (in percent) the docked panel may occupy when dragged.
const MAX_DRAGGED_PANEL_PERCENT: u16 = 60;

/// Widest width the docked panel may be dragged to on a terminal `total` columns wide.
pub(crate) fn max_dragged_width(total: u16) -> u16 {
    let by_transcript = total.saturating_sub(MIN_DRAGGED_TRANSCRIPT_WIDTH);
    let by_ratio = (u32::from(total) * u32::from(MAX_DRAGGED_PANEL_PERCENT) / 100) as u16;
    by_transcript.min(by_ratio).max(PANEL_WIDTH)
}

/// Whether a terminal this wide docks the panel beside the transcript.
pub(crate) fn docks(width: u16) -> bool {
    width >= PANEL_WIDTH + MIN_TRANSCRIPT_WIDTH
}

/// Which view the side panel shows.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) enum PanelMode {
    #[default]
    LiveWork,
    Browser,
}

/// Where the panel is and whether the reducer knows it is on screen.
#[derive(Debug, Default)]
pub(crate) struct SidePanel {
    /// Active view shown inside the panel (`LiveWork` vs `Browser`).
    mode: PanelMode,
    /// Whether live interactive browser takeover owns keyboard and viewport routing.
    browser_interacting: bool,
    /// Whether the docked panel already auto-switched to `Browser` on the first frame
    /// of the current browser session, so manual switches back to `Work` are respected.
    auto_switched_browser: bool,
    /// Whether this session has seen any browser viewport state, so manual `/browser`
    /// toggles before the first frame are not reset back to `LiveWork`.
    had_browser_viewport: bool,
    /// Hidden with Ctrl+P on a terminal wide enough to dock it.
    hidden: bool,
    /// Opened with Ctrl+P on a terminal too narrow to dock it.
    overlay: bool,
    /// Temporarily expanded into a large centered overlay (for both `Work` and `Browser`).
    expanded: bool,
    /// Whether closing the overlay hides the panel again, because the background
    /// strip opened a panel the user had hidden.
    rehide: bool,
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
    /// Logical action rows from the last paint, including rows below the viewport.
    painted: RefCell<Painted>,
    scroll: Cell<usize>,
    /// The background process whose peek is open under its row, while the panel has
    /// focus; moving off its row closes it.
    peek: Option<String>,
    /// Whether `x` asked to stop the peeked process and waits for `y`.
    confirm_stop: bool,
    /// Whether the next paint scrolls the peek that just opened into view.
    reveal_peek: Cell<bool>,
    /// Whether focus moved to the background processes since the reducer was last
    /// asked to read them.
    processes_wanted: bool,
}

#[derive(Clone, Debug)]
struct ActionHit {
    line: usize,
    columns: Option<(u16, u16)>,
    section: Section,
    action: PanelAction,
}

#[derive(Debug, Default)]
struct Painted {
    surface: Rect,
    area: Rect,
    total: usize,
    actions: Vec<ActionHit>,
    viewport_image_rect: Option<Rect>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum Disclosure {
    /// The peek of a background process, by its id.
    Process(String),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum PanelAction {
    SwitchMode(PanelMode),
    ToggleExpand,
    Toggle(Disclosure),
    Open(Target),
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

/// A section of the panel that focus can land on.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Section {
    ModeTabs,
    Header,
    Queue,
    Running,
    Attention,
    Context,
    Inventory,
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
    Queue,
    Mcp,
    Skills,
    Context,
    Tree,
    /// The transcript card of a background process, by its id.
    Process(String),
    /// The transcript card of a subagent, by its tool call id.
    Subagent(String),
}

/// What a key in the overlay asks of the app.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum OverlayKey {
    Close,
    /// Close the overlay and open this view.
    Open(Target),
    /// Focus moved; repaint.
    Moved,
    /// Stop the background process with this id; the user confirmed it.
    Stop(String),
    /// Enter interactive browser takeover mode.
    EnterBrowserInteract,
    /// Trigger a manual browser screenshot refresh.
    RefreshBrowser,
    Ignored,
}

impl SidePanel {
    pub(crate) fn docked(&self) -> bool {
        !self.hidden && docks(self.width)
    }

    pub(crate) fn overlay_open(&self) -> bool {
        self.overlay
    }

    pub(crate) fn expanded(&self) -> bool {
        self.expanded
    }

    pub(crate) fn toggle_expanded(&mut self) {
        self.expanded = !self.expanded;
        if self.expanded {
            self.hidden = false;
            self.overlay = true;
            self.rehide = false;
        } else if docks(self.width) {
            self.overlay = false;
        }
    }

    pub(crate) fn mode(&self) -> PanelMode {
        self.mode
    }

    pub(crate) fn browser_interacting(&self) -> bool {
        self.browser_interacting
    }

    pub(crate) fn show_browser(&mut self) {
        self.mode = PanelMode::Browser;
        self.close_peek();
        self.cursor = None;
        self.scroll.set(0);
        if docks(self.width) {
            self.hidden = false;
            if !self.expanded {
                self.overlay = false;
            }
        } else {
            self.overlay = true;
            self.rehide = false;
        }
    }

    pub(crate) fn enter_browser_interact(&mut self) {
        self.show_browser();
        self.browser_interacting = true;
    }

    pub(crate) fn exit_browser_interact(&mut self) -> bool {
        std::mem::take(&mut self.browser_interacting)
    }

    #[cfg(test)]
    pub(crate) fn viewport_image_rect(&self) -> Option<Rect> {
        self.painted.borrow().viewport_image_rect
    }

    pub(crate) fn map_viewport_point(
        &self,
        state: &UiState,
        column: u16,
        row: u16,
    ) -> Option<(u32, u32)> {
        const MAX_BROWSER_COORD: u32 = 16_384;
        if !self.visible() || self.mode != PanelMode::Browser {
            return None;
        }
        let vp = state.browser_viewport.as_ref()?;
        let image_rect = self.painted.borrow().viewport_image_rect?;
        let image_dims = vp.image.as_ref().and_then(|img| img.dimensions);
        let vw = vp.viewport_width.or_else(|| image_dims.map(|(w, _)| w))?;
        let vh = vp.viewport_height.or_else(|| image_dims.map(|(_, h)| h))?;
        browser::map_cell_to_viewport(image_rect, column, row, vw, vh)
            .map(|(x, y)| (x.min(MAX_BROWSER_COORD), y.min(MAX_BROWSER_COORD)))
    }

    /// Auto-switch a docked panel to `Browser` on the first frame of a session,
    /// resetting back to `LiveWork` when the session clears its browser viewport.
    pub(crate) fn observe_browser_viewport(&mut self, viewport: Option<&BrowserViewport>) {
        match viewport {
            None => {
                self.browser_interacting = false;
                if std::mem::take(&mut self.had_browser_viewport) {
                    self.auto_switched_browser = false;
                    self.mode = PanelMode::LiveWork;
                }
            }
            Some(view) => {
                self.had_browser_viewport = true;
                if view.image.is_some() && !self.auto_switched_browser {
                    self.auto_switched_browser = true;
                    if self.docked() {
                        self.mode = PanelMode::Browser;
                    }
                }
            }
        }
    }

    /// `/browser`: focus/show the Browser viewport on wide or narrow terminals, or
    /// toggle a visible docked Browser tab back to LiveWork.
    pub(crate) fn toggle_browser(&mut self) {
        self.browser_interacting = false;
        self.expanded = false;
        if docks(self.width) {
            self.rehide = false;
            self.overlay = false;
            if !self.hidden && self.mode == PanelMode::Browser {
                self.mode = PanelMode::LiveWork;
            } else {
                self.hidden = false;
                self.mode = PanelMode::Browser;
            }
        } else if self.overlay && self.mode == PanelMode::Browser {
            self.close_overlay();
        } else {
            self.mode = PanelMode::Browser;
            self.overlay = true;
            self.rehide = false;
            self.cursor = None;
            *self.painted.borrow_mut() = Painted::default();
        }
        self.scroll.set(0);
        self.close_peek();
    }

    pub(crate) fn visible(&self) -> bool {
        self.docked() || self.overlay_open()
    }

    /// Ctrl+P: hide or show the docked panel, or open or close the overlay. An
    /// overlay opens with focus on the first row it can act on.
    pub(crate) fn toggle(&mut self) {
        self.browser_interacting = false;
        self.expanded = false;
        if docks(self.width) {
            self.rehide = false;
            self.hidden = !self.hidden;
            self.overlay = false;
        } else {
            if self.overlay {
                self.close_overlay();
            } else {
                self.overlay = true;
            }
            self.cursor = None;
            *self.painted.borrow_mut() = Painted::default();
        }
    }

    pub(crate) fn focus(&mut self) {
        self.browser_interacting = false;
        self.hidden = false;
        self.overlay = true;
        self.rehide = false;
        self.cursor = None;
    }

    /// Focuses the panel on its first background process, as the strip under the
    /// composer does. While nothing is listed there, focus falls back to the first
    /// row the panel can act on. A panel the user hid hides again when focus leaves.
    pub(crate) fn focus_background(&mut self) {
        let hidden = self.hidden;
        self.focus();
        self.mode = PanelMode::LiveWork;
        self.browser_interacting = false;
        self.rehide = hidden;
        self.cursor = Some(Cursor {
            section: Section::Running,
            row: 0,
        });
        self.processes_wanted = true;
    }

    /// Whether focus moved to the background processes since this was last asked,
    /// so their list, ended ones included, should be read again.
    pub(crate) fn take_processes_wanted(&mut self) -> bool {
        std::mem::take(&mut self.processes_wanted)
    }

    /// The peek to give the reducer, which reads its output, when the two differ. A
    /// peek whose process the panel no longer lists closes first. The reducer's own
    /// peek is the comparison, since a transcript rebuild closes it there alone.
    pub(crate) fn peek_change(&mut self, state: &UiState) -> Option<Option<String>> {
        if let Some(process_id) = &self.peek {
            let processes = &state.background_processes;
            let listed = processes
                .running()
                .iter()
                .any(|process| process.process_id == *process_id)
                || processes
                    .finished()
                    .iter()
                    .any(|process| process.process_id == *process_id);
            if !listed || !self.overlay {
                self.close_peek();
            }
        }
        let reading = state
            .background_processes
            .peek()
            .map(|peek| peek.process_id.as_str());
        (self.peek.as_deref() != reading).then(|| self.peek.clone())
    }

    fn close_peek(&mut self) {
        self.peek = None;
        self.confirm_stop = false;
    }

    /// The keys of an open peek: Enter opens the process's card, `x` asks to stop it
    /// and `y` confirms, and Esc closes the peek. Any other key cancels a pending
    /// stop. `None` leaves the key to the panel.
    fn peek_key(
        &mut self,
        key: KeyCode,
        focused: Option<&PanelAction>,
        state: &UiState,
    ) -> Option<OverlayKey> {
        let running = |process_id: &str| {
            state
                .background_processes
                .running()
                .iter()
                .any(|process| process.process_id == process_id)
        };
        // The confirmation names the peeked process, so `y` stops that one.
        if std::mem::take(&mut self.confirm_stop) {
            return Some(match (key, self.peek.clone()) {
                (KeyCode::Char('y'), Some(process_id)) if running(&process_id) => {
                    OverlayKey::Stop(process_id)
                }
                _ => OverlayKey::Moved,
            });
        }
        let Some(PanelAction::Toggle(Disclosure::Process(process_id))) = focused else {
            return None;
        };
        let running = running(process_id);
        let open = self.peek.as_ref() == Some(process_id);
        match key {
            KeyCode::Char('x') if running => {
                self.reveal_peek.set(!open);
                self.peek = Some(process_id.clone());
                self.confirm_stop = true;
                Some(OverlayKey::Moved)
            }
            KeyCode::Enter if open => Some(OverlayKey::Open(Target::Process(process_id.clone()))),
            KeyCode::Esc if open => {
                self.close_peek();
                Some(OverlayKey::Moved)
            }
            _ => None,
        }
    }

    /// Keyboard navigation follows the same rows and disclosure controls as clicks.
    pub(crate) fn key(&mut self, key: KeyCode, state: &UiState) -> OverlayKey {
        let focused = self
            .focused(state)
            .and_then(|cursor| self.targets(cursor.section, state).get(cursor.row).cloned());
        if let Some(handled) = self.peek_key(key, focused.as_ref(), state) {
            return handled;
        }
        if key == KeyCode::Esc {
            return OverlayKey::Close;
        }
        if key == KeyCode::Char('f') {
            self.toggle_expanded();
            return OverlayKey::Moved;
        }
        if key == KeyCode::Char('b')
            && (state.browser_viewport.is_some() || self.mode == PanelMode::Browser)
        {
            self.browser_interacting = false;
            self.mode = match self.mode {
                PanelMode::LiveWork => PanelMode::Browser,
                PanelMode::Browser => PanelMode::LiveWork,
            };
            self.cursor = None;
            self.scroll.set(0);
            self.close_peek();
            return OverlayKey::Moved;
        }
        if self.mode == PanelMode::Browser {
            match key {
                KeyCode::Char('i') => return OverlayKey::EnterBrowserInteract,
                KeyCode::Char('r') => return OverlayKey::RefreshBrowser,
                _ => {}
            }
        }
        if matches!(key, KeyCode::PageUp | KeyCode::PageDown) {
            let height = self.painted.borrow().area.height.max(1) as i32;
            self.scroll_by(if key == KeyCode::PageUp {
                -height
            } else {
                height
            });
            return OverlayKey::Moved;
        }
        let sections = self.sections(state);
        let Some(cursor) = self.focused(state) else {
            return OverlayKey::Ignored;
        };
        let targets = self.targets(cursor.section, state);
        let position = sections
            .iter()
            .position(|section| *section == cursor.section)
            .unwrap();
        let moved = match key {
            KeyCode::Tab
                if sections.len() == 1
                    && cursor.section == Section::ModeTabs
                    && targets.len() > 1 =>
            {
                Cursor {
                    row: (cursor.row + 1) % targets.len(),
                    ..cursor
                }
            }
            KeyCode::BackTab
                if sections.len() == 1
                    && cursor.section == Section::ModeTabs
                    && targets.len() > 1 =>
            {
                Cursor {
                    row: (cursor.row + targets.len() - 1) % targets.len(),
                    ..cursor
                }
            }
            KeyCode::Tab => Cursor {
                section: sections[(position + 1) % sections.len()],
                row: 0,
            },
            KeyCode::BackTab => Cursor {
                section: sections[(position + sections.len() - 1) % sections.len()],
                row: 0,
            },
            KeyCode::Up | KeyCode::Left => Cursor {
                row: cursor.row.saturating_sub(1),
                ..cursor
            },
            KeyCode::Down | KeyCode::Right => Cursor {
                row: (cursor.row + 1).min(targets.len() - 1),
                ..cursor
            },
            KeyCode::Home => Cursor { row: 0, ..cursor },
            KeyCode::End => Cursor {
                row: targets.len() - 1,
                ..cursor
            },
            KeyCode::Enter | KeyCode::Char(' ') => {
                return self.activate(targets[cursor.row].clone());
            }
            _ => return OverlayKey::Ignored,
        };
        self.cursor = Some(moved);
        // The peek belongs to the row it opened under.
        if self.peek.is_some() {
            let target = self.targets(moved.section, state).get(moved.row).cloned();
            if target != focused {
                self.close_peek();
            }
        }
        if let Some(hit) = self
            .painted
            .borrow()
            .actions
            .iter()
            .filter(|hit| hit.section == moved.section)
            .nth(moved.row)
        {
            let height = usize::from(self.painted.borrow().area.height);
            if hit.line < self.scroll.get() {
                self.scroll.set(hit.line);
            } else if hit.line >= self.scroll.get() + height {
                self.scroll
                    .set(hit.line.saturating_sub(height.saturating_sub(1)));
            }
        }
        OverlayKey::Moved
    }

    fn sections(&self, _state: &UiState) -> Vec<Section> {
        let mut sections = Vec::new();
        for hit in &self.painted.borrow().actions {
            if matches!(hit.action, PanelAction::ToggleExpand) {
                continue;
            }
            if !sections.contains(&hit.section) {
                sections.push(hit.section);
            }
        }
        sections
    }

    fn targets(&self, section: Section, _state: &UiState) -> Vec<PanelAction> {
        self.painted
            .borrow()
            .actions
            .iter()
            .filter(|hit| {
                hit.section == section && !matches!(hit.action, PanelAction::ToggleExpand)
            })
            .map(|hit| hit.action.clone())
            .collect()
    }

    fn focused(&self, state: &UiState) -> Option<Cursor> {
        // Focus stays on the peeked process while rows move, as when one ends and
        // leaves the running list.
        if let Some(process_id) = &self.peek {
            let peeked = PanelAction::Toggle(Disclosure::Process(process_id.clone()));
            if let Some(row) = self
                .targets(Section::Running, state)
                .iter()
                .position(|target| *target == peeked)
            {
                return Some(Cursor {
                    section: Section::Running,
                    row,
                });
            }
        }
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
            .map_or_else(
                || {
                    if section == Section::ModeTabs && self.mode == PanelMode::Browser {
                        1.min(rows.saturating_sub(1))
                    } else {
                        0
                    }
                },
                |cursor| cursor.row.min(rows - 1),
            );
        Some(Cursor { section, row })
    }

    pub(crate) fn activate(&mut self, action: PanelAction) -> OverlayKey {
        match action {
            PanelAction::SwitchMode(mode) => {
                if mode != PanelMode::Browser {
                    self.browser_interacting = false;
                }
                self.mode = mode;
                self.cursor = None;
                self.scroll.set(0);
                self.close_peek();
                OverlayKey::Moved
            }
            PanelAction::ToggleExpand => {
                self.toggle_expanded();
                OverlayKey::Moved
            }
            PanelAction::Open(target) => OverlayKey::Open(target),
            PanelAction::Toggle(Disclosure::Process(process_id)) => {
                self.browser_interacting = false;
                if self.peek.as_ref() == Some(&process_id) {
                    self.close_peek();
                } else {
                    self.peek = Some(process_id);
                    self.confirm_stop = false;
                    self.reveal_peek.set(true);
                    // The peek's keys need focus, which a click on the docked
                    // panel does not otherwise give it.
                    self.overlay = true;
                }
                // The row stays selected; the next paint rebuilds its hit regions.
                OverlayKey::Moved
            }
        }
    }

    pub(crate) fn contains(&self, column: u16, row: u16) -> bool {
        let area = self.painted.borrow().surface;
        self.visible()
            && column >= area.x
            && column < area.right()
            && row >= area.y
            && row < area.bottom()
    }

    pub(crate) fn click(&mut self, column: u16, row: u16) -> OverlayKey {
        if !self.contains(column, row) {
            return OverlayKey::Ignored;
        }
        let hit = {
            let painted = self.painted.borrow();
            if row < painted.area.y
                || row >= painted.area.bottom()
                || column < painted.area.x
                || column >= painted.area.right()
            {
                return OverlayKey::Ignored;
            }
            let line = usize::from(row - painted.area.y) + self.scroll.get();
            let rel_col = column - painted.area.x;
            painted
                .actions
                .iter()
                .enumerate()
                .find(|(_, hit)| {
                    hit.line == line
                        && hit
                            .columns
                            .is_none_or(|(start, end)| rel_col >= start && rel_col < end)
                })
                .map(|(index, hit)| (index, hit.clone()))
        };
        let Some((hit_index, hit)) = hit else {
            return OverlayKey::Ignored;
        };
        let row = self
            .painted
            .borrow()
            .actions
            .iter()
            .take(hit_index)
            .filter(|other| other.section == hit.section)
            .count();
        self.cursor = Some(Cursor {
            section: hit.section,
            row,
        });
        self.activate(hit.action)
    }

    pub(crate) fn scroll_by(&self, lines: i32) {
        let painted = self.painted.borrow();
        let max = painted
            .total
            .saturating_sub(usize::from(painted.area.height));
        self.scroll.set(
            self.scroll
                .get()
                .saturating_add_signed(lines as isize)
                .min(max),
        );
    }

    pub(crate) fn clear_paint(&self) {
        *self.painted.borrow_mut() = Painted::default();
    }

    /// Whether Ctrl+P would open or close the overlay rather than the docked panel.
    pub(crate) fn toggles_overlay(&self) -> bool {
        !docks(self.width)
    }

    pub(crate) fn close_overlay(&mut self) {
        self.browser_interacting = false;
        self.overlay = false;
        self.expanded = false;
        self.close_peek();
        if std::mem::take(&mut self.rehide) {
            self.hidden = true;
        }
    }

    /// Follow the terminal's width. An overlay open when the terminal grows wide
    /// enough becomes the docked panel, unless the user explicitly expanded it.
    pub(crate) fn observe_width(&mut self, width: u16) {
        if self.overlay && !self.expanded && !docks(self.width) && docks(width) {
            self.overlay = false;
            // A panel the background strip opened over a hidden one stays hidden.
            self.hidden = std::mem::take(&mut self.rehide);
        }
        self.width = width;
        if !self.visible() || self.mode != PanelMode::Browser {
            self.browser_interacting = false;
        }
    }

    /// The docked panel's width on a terminal `total` columns wide, its border included:
    /// the dragged width, kept to between [`PANEL_WIDTH`] and [`max_dragged_width`],
    /// or [`auto_width`] when nothing was dragged.
    pub(crate) fn docked_width(&self, total: u16) -> u16 {
        self.dragged_width.map_or_else(
            || auto_width(total),
            |width| width.min(max_dragged_width(total)).max(PANEL_WIDTH),
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
            self.dragged_width = Some(width.min(max_dragged_width(total)).max(PANEL_WIDTH));
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

/// The panel's area when expanded (`f` / `[Expand]`): a large centered overlay that
/// gives either `Work` or `Browser` generous room on any terminal size.
pub(crate) fn expanded_area(frame: Rect) -> Rect {
    let margin_x = if frame.width >= 20 { 2 } else { 0 };
    let margin_y = if frame.height >= 12 { 1 } else { 0 };
    let max_w = frame.width.saturating_sub(margin_x * 2);
    let max_h = frame.height.saturating_sub(margin_y * 2);
    let width = ((u32::from(frame.width) * 90) / 100) as u16;
    let height = ((u32::from(frame.height) * 88) / 100) as u16;
    let width = width.clamp(PANEL_WIDTH.min(max_w), max_w);
    let height = height.clamp(10.min(max_h), max_h);
    Rect {
        x: frame.x + (frame.width.saturating_sub(width)) / 2,
        y: frame.y + (frame.height.saturating_sub(height)) / 2,
        width,
        height,
    }
}

/// The docked panel's width on a terminal `total` columns wide when its border was
/// not dragged, its border included. The panel and the transcript share the columns
/// beyond the docking threshold evenly, up to [`MAX_PANEL_WIDTH`].
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
}

impl FileSummary {
    fn new(snapshot: Arc<GitDiffSnapshot>) -> Self {
        Self { snapshot }
    }
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

#[cfg(test)]
#[allow(clippy::too_many_arguments)]
pub(crate) fn render(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    panel: &SidePanel,
    bindings: &Bindings,
    palette: Palette,
) {
    render_with_preview(
        frame,
        area,
        state,
        panel,
        bindings,
        palette,
        None,
        CellSize::default(),
        None,
        false,
    );
}

/// Draw the panel into `area`. A docked panel uses the raised surface tier with no
/// divider line (showing a left edge only while dragged); an overlay is boxed so it
/// reads as lying over the transcript.
///
/// The layout leads with live work (pending decision, plan, running subagents and
/// background processes, queue), hides empty sections, states attention items in
/// words, and condenses context and inventories to single lines.
#[allow(clippy::too_many_arguments)]
pub(crate) fn render_with_preview(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    panel: &SidePanel,
    bindings: &Bindings,
    palette: Palette,
    preview: Option<&ImagePreviewMeta>,
    cell_size: CellSize,
    painted_images: Option<&mut Vec<PaintedImage>>,
    ignore_overlay_rect: bool,
) {
    let overlay = !panel.docked() || panel.expanded();
    let surface = if palette.shows_bands() {
        palette.base().bg(palette.surface)
    } else {
        palette.overlay()
    };
    let interacting = panel.mode == PanelMode::Browser && panel.browser_interacting();
    let block = if overlay {
        Block::default()
            .borders(Borders::ALL)
            .border_style(if interacting {
                Style::default().fg(palette.primary)
            } else {
                palette.border()
            })
            .padding(Padding::horizontal(1))
    } else if interacting || panel.resizing() {
        // Highlight the left edge while interacting or dragging without shifting content.
        Block::default()
            .borders(Borders::LEFT)
            .border_style(Style::default().fg(palette.primary))
            .padding(Padding::new(1, 1, 1, 0))
    } else {
        Block::default()
            .borders(Borders::NONE)
            .padding(Padding::new(2, 1, 1, 0))
    };
    let inner = block.inner(area);
    frame.render_widget(block.style(surface), area);
    let width = usize::from(inner.width);
    let height = usize::from(inner.height);
    if width == 0 || height == 0 {
        *panel.painted.borrow_mut() = Painted::default();
        return;
    }

    let show_tabs = state.browser_viewport.is_some() || panel.mode == PanelMode::Browser;
    if panel.mode == PanelMode::Browser {
        let mut rows = PanelRows::default();
        browser::push_mode_tabs(
            &mut rows,
            panel.mode,
            interacting,
            panel.expanded,
            width,
            palette,
        );
        let footer_height = usize::from(panel.overlay_open() || interacting);
        let body = Rect {
            height: inner.height.saturating_sub(footer_height as u16),
            ..inner
        };
        panel.scroll.set(0);
        *panel.painted.borrow_mut() = Painted {
            surface: area,
            area: body,
            total: rows.lines.len(),
            actions: rows.actions,
            viewport_image_rect: None,
        };
        if let Some(cursor) = panel.overlay_open().then(|| panel.focused(state)).flatten() {
            if let Some(hit) = panel
                .painted
                .borrow()
                .actions
                .iter()
                .filter(|hit| hit.section == cursor.section)
                .nth(cursor.row)
                .cloned()
            {
                mark_focus(&mut rows.lines, &hit, width, palette);
            }
        }
        let tab_rect = Rect {
            height: body.height.min(1),
            ..body
        };
        frame.render_widget(Paragraph::new(rows.lines), tab_rect);
        let tab_rows = if body.height > 2 {
            2
        } else {
            1.min(body.height)
        };
        let browser_body = Rect {
            y: body.y + tab_rows,
            height: body.height.saturating_sub(tab_rows),
            ..body
        };
        let viewport_image_rect = browser::render_browser_body(
            frame,
            browser_body,
            state,
            surface,
            palette,
            preview,
            cell_size,
            painted_images,
            ignore_overlay_rect,
        );
        panel.painted.borrow_mut().viewport_image_rect = viewport_image_rect;
        if footer_height > 0 {
            let footer = Rect {
                y: body.bottom(),
                height: footer_height as u16,
                ..inner
            };
            let hints = if interacting {
                "Esc exit · Click/Scroll · Type · Alt+← back · Ctrl+R reload"
            } else if panel.expanded {
                "Esc close · Tab tabs · b work · i interact · f restore · r refresh"
            } else {
                "Esc · Tab · b work · i interact · f size · r refresh"
            };
            frame.render_widget(Paragraph::new(ui::keycap_hints(hints, palette)), footer);
        }
        return;
    }

    let focus = Focus::of(state);
    let mut rows = PanelRows::default();
    if show_tabs {
        browser::push_mode_tabs(&mut rows, panel.mode, false, panel.expanded, width, palette);
    }
    let (header, branch_row) = header_lines(state, bindings, width, palette);
    rows.section(Section::Header, header);
    if let Some(row) = branch_row {
        rows.action(row, PanelAction::Open(Target::Tree));
    }
    if focus == Focus::Approval {
        if let Some(lines) = approval_lines(state, width, palette) {
            rows.section(Section::Queue, lines);
        }
    }
    if let Some(plan) = plan::plan_lines(state, width, palette) {
        rows.section(Section::Running, plan);
    }
    // With focus, as `/ps` and the background strip open it, every running/finished
    // item is listed so each can be selected; the docked panel alone shows the first few.
    let shown = if panel.overlay_open() {
        usize::MAX
    } else {
        running::MAX_RUNNING_ROWS
    };
    let mut peek_span = None;
    if let Some(running) = running::running_lines(state, panel, shown, width, palette) {
        rows.section(Section::Running, running.lines);
        let start = rows.current.map_or(0, |(start, _)| start);
        peek_span = running
            .peek
            .map(|(first, last)| (start + first, start + last));
        for (line, action) in running.actions {
            rows.action(line, action);
        }
    }
    if focus != Focus::Idle {
        if let Some(lines) = queue_lines(state, focus == Focus::Approval, bindings, width, palette)
        {
            let count =
                (state.queued_steering() + state.queued_follow_ups()).min(MAX_QUEUE_ROWS + 1);
            rows.section(Section::Queue, lines);
            for index in 1..=count {
                rows.action(index, PanelAction::Open(Target::Queue));
            }
        }
    }
    reading::attention_rows(&mut rows, state, panel, width, palette);
    reading::context_line(&mut rows, state, width, palette);
    reading::inventory_summary(&mut rows, state, panel, width, palette);

    // The bottom row stays anchored while the panel scrolls. A focused panel
    // names its keys there; otherwise the row appears only to show the scroll
    // position, since the main footer carries the panel's keys.
    let footer_height = usize::from(panel.overlay_open() || rows.lines.len() > height);
    let body = Rect {
        height: inner.height.saturating_sub(footer_height as u16),
        ..inner
    };
    let max_scroll = rows.lines.len().saturating_sub(usize::from(body.height));
    let mut offset = panel.scroll.get().min(max_scroll);
    // A peek that just opened scrolls into view, its process row first if it must.
    if let Some((first, last)) = peek_span.filter(|_| panel.reveal_peek.take()) {
        let height = usize::from(body.height).max(1);
        if last >= offset + height {
            offset = (last + 1 - height).min(first);
        } else if first < offset {
            offset = first;
        }
    }
    panel.scroll.set(offset);
    *panel.painted.borrow_mut() = Painted {
        surface: area,
        area: body,
        total: rows.lines.len(),
        actions: rows.actions,
        viewport_image_rect: None,
    };
    if let Some(cursor) = panel.overlay_open().then(|| panel.focused(state)).flatten() {
        if let Some(hit) = panel
            .painted
            .borrow()
            .actions
            .iter()
            .filter(|hit| hit.section == cursor.section)
            .nth(cursor.row)
            .cloned()
        {
            mark_focus(&mut rows.lines, &hit, width, palette);
        }
    }
    let visible: Vec<_> = rows
        .lines
        .into_iter()
        .skip(offset)
        .take(usize::from(body.height))
        .collect();
    frame.render_widget(Paragraph::new(visible), body);
    let muted = Style::default().fg(palette.muted);
    let footer = Rect {
        y: body.bottom(),
        height: footer_height as u16,
        ..inner
    };
    let hint = if panel.overlay_open() {
        let hint_text = if panel.expanded {
            if show_tabs {
                "Esc close · Tab sections · b browser · f restore · Enter"
            } else {
                "Esc close · Tab sections · f restore · Enter"
            }
        } else if show_tabs {
            "Esc close · Tab sections · b browser · Enter"
        } else {
            "Esc close · Tab sections · Enter"
        };
        ui::keycap_hints(hint_text, palette).spans
    } else {
        Vec::new()
    };
    let more = if max_scroll > 0 {
        format!(" {}/{}", offset + 1, max_scroll + 1)
    } else {
        String::new()
    };
    frame.render_widget(
        Paragraph::new(spread(hint, vec![Span::styled(more, muted)], width)),
        footer,
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

/// Highlight the focused row across the panel's `width`, or a single column range
/// when a line holds several inline links (such as the inventory summary).
fn mark_focus(lines: &mut [Line<'static>], hit: &ActionHit, width: usize, palette: Palette) {
    let Some(line) = lines.get_mut(hit.line) else {
        return;
    };
    let Some((start, end)) = hit.columns else {
        // Pad the row so the highlight spans the panel, as a list selection does.
        let padding = width.saturating_sub(line.width());
        line.spans.push(Span::raw(" ".repeat(padding)));
        *line = palette.selected_line(std::mem::take(line));
        return;
    };
    let start = usize::from(start);
    let end = usize::from(end);
    let mut col = 0usize;
    let selection = palette.selection();
    for span in &mut line.spans {
        let span_end = col + span.content.width();
        if col >= start && span_end <= end {
            span.style = palette.selected_row(span.style).patch(selection);
        }
        col = span_end;
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
/// Returns the header lines and the row index of the branch link when present.
fn header_lines(
    state: &UiState,
    bindings: &Bindings,
    width: usize,
    palette: Palette,
) -> (Vec<Line<'static>>, Option<usize>) {
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
    // Run state stays in the footer and status line, which show with or without the panel.
    let mut lines = vec![Line::styled(
        ui::clip_columns(&title, width),
        Style::default().add_modifier(Modifier::BOLD),
    )];
    let workspace = ui::footer_workspace_parts(state);
    if !workspace.is_empty() {
        lines.push(Line::styled(
            ui::clip_columns(&workspace.join(" · "), width),
            Style::default().fg(palette.muted),
        ));
    }
    lines.extend(codemode_line(state, width, palette));
    let mut branch_row = None;
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
        branch_row = Some(lines.len());
        lines.push(spread(
            vec![
                Span::styled("⑂ ", Style::default().fg(palette.muted)),
                Span::styled(branch, Style::default().fg(palette.muted)),
            ],
            vec![Span::styled(key, palette.keycap())],
            width,
        ));
    }
    (lines, branch_row)
}

/// Whether the next prompt offers codemode, and which setting decides it. The
/// automatic setting has no row while it leaves codemode off, which is the usual case
/// when no MCP tool is one a script may call.
fn codemode_line(state: &UiState, width: usize, palette: Palette) -> Option<Line<'static>> {
    let snapshot = state.codemode.snapshot.as_deref()?;
    let setting = crate::discovery_view::codemode_label(snapshot.setting);
    let (text, color) = if snapshot.presentation == CodemodePresentation::CodemodeOnly {
        if snapshot.effective_presentation == CodemodePresentation::CodemodeOnly {
            (format!("codemode only · {setting}"), palette.muted)
        } else {
            (
                "codemode only · using direct tools".to_owned(),
                palette.warning,
            )
        }
    } else if !snapshot.available && snapshot.setting == CodemodeSetting::Always {
        (
            format!("codemode {setting} · needs wisp-ai[codemode]"),
            palette.warning,
        )
    } else if snapshot.offered {
        (format!("codemode on · {setting}"), palette.muted)
    } else if snapshot.setting == CodemodeSetting::Automatic {
        return None;
    } else {
        (format!("codemode off · {setting}"), palette.muted)
    };
    Some(Line::styled(
        ui::clip_columns(&text, width),
        Style::default().fg(color),
    ))
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
        let mut room = width.saturating_sub(lead.width());
        let mut row = vec![Span::styled(lead, Style::default().fg(palette.secondary))];
        // The image note comes first so a long message cannot clip it away.
        if let Some(note) = message.image_note() {
            let note = format!("{note} ");
            room = room.saturating_sub(note.width());
            row.push(Span::styled(note, muted));
        }
        row.push(Span::raw(ui::clip_columns(
            &first_line(&message.content),
            room,
        )));
        lines.push(Line::from(row));
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

fn permission_mode(state: &UiState) -> Option<&'static str> {
    state
        .permissions
        .snapshot
        .as_ref()
        .map(|permissions| match permissions.mode {
            PermissionMode::Ask => "ask",
            PermissionMode::Auto => "auto (edits)",
            PermissionMode::AutoCommands => "auto (commands)",
            PermissionMode::Yolo => "YOLO",
        })
}

mod browser;
mod peek;
mod plan;
mod reading;
mod running;

#[derive(Default)]
struct PanelRows {
    lines: Vec<Line<'static>>,
    actions: Vec<ActionHit>,
    current: Option<(usize, Section)>,
}

impl PanelRows {
    fn section(&mut self, kind: Section, lines: Vec<Line<'static>>) {
        if !self.lines.is_empty() {
            self.lines.push(Line::default());
        }
        self.current = Some((self.lines.len(), kind));
        self.lines.extend(lines);
    }

    fn action(&mut self, row: usize, action: PanelAction) {
        let (start, kind) = self.current.expect("section before action");
        if start + row < self.lines.len() {
            self.actions.push(ActionHit {
                line: start + row,
                columns: None,
                section: kind,
                action,
            });
        }
    }

    fn action_range(&mut self, row: usize, start_col: u16, end_col: u16, action: PanelAction) {
        let (start, kind) = self.current.expect("section before action");
        if start + row < self.lines.len() {
            self.actions.push(ActionHit {
                line: start + row,
                columns: Some((start_col, end_col)),
                section: kind,
                action,
            });
        }
    }

    fn push(&mut self, line: Line<'static>, action: Option<PanelAction>) {
        if let Some(action) = action {
            let (_, kind) = self.current.expect("section before row");
            self.actions.push(ActionHit {
                line: self.lines.len(),
                columns: None,
                section: kind,
                action,
            });
        }
        self.lines.push(line);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::keybindings::Bindings;
    use crate::prompt_editor::PromptEditor;
    use crate::reducer::ViewStatus;
    use crate::reducer::git_diff::GitDiffFile;
    use crate::startup_logo::LogoChoice;
    use crate::transcript::{RunOutcome, RunResult};
    use crate::transcript_view::{TranscriptRowCache, TranscriptViewport};
    use crate::ui::{ComposerLayoutCache, ConnectionInfo};
    use crossterm::event::MouseButton;
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use wisp_protocol::events::{
        ContextAccountingMethod, ContextBreakdown, ContextBudget, ContextEstimate, ContextItem,
        ContextSource, ContextSourceItem, ContextSourceUsage, ContextTurnGrowth, McpServerSnapshot,
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
            used_skill_names: Vec::new(),
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
                    read_only_tool_names: Vec::new(),
                    read_only_hint_tool_names: Vec::new(),
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
                    crate::ui::Welcome {
                        logo: LogoChoice::AdalBraille,
                        ..crate::ui::Welcome::default()
                    },
                    None,
                    ui::Motion::default(),
                    ui::StatusTimers::default(),
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
            "198.8K · 73% context",
            "/context",
            "2 MCP · 2 changed",
        ] {
            assert!(panel.contains(expected), "{expected}\n{panel}");
        }
        // Static inventories, turn lists, and composer/footer facts stay out of the panel.
        for moved in [
            "Permissions",
            "Ctrl+P panel",
            "add a read timeout setting",
            "MCP servers",
            "Changed files",
            "Recent turns",
            "Breakdown",
            "No changes",
            "reading…",
        ] {
            assert!(!panel.contains(moved), "{moved}\n{panel}");
        }
        assert!(
            rows.iter().any(|row| row.contains("Ctrl+P panel")),
            "{rows:#?}"
        );
    }

    #[test]
    fn docked_panel_leaves_yolo_to_main_column() {
        let mut state = session();
        state.permissions.snapshot = Some(Arc::new(wisp_protocol::events::PermissionState {
            mode: PermissionMode::Yolo,
            saved_mode: Some(PermissionMode::Yolo),
            project_path: Some("/srv/wisp".into()),
            session_only_yolo: false,
        }));
        let mut compacting = state.clone();
        compacting.view_status = ViewStatus::Running;
        compacting.interaction_status = crate::reducer::InteractionStatus::Compacting;
        // A framed composer names it; a short terminal's footer does, also while
        // the status line names a compaction the composer cannot frame.
        for (state, height) in [(&state, 40), (&state, 12), (&compacting, 40)] {
            let rows = screen_of_height(state, &mut SidePanel::default(), 200, height);
            let panel = panel_text(&rows, 200);
            assert!(!panel.contains("YOLO"), "{panel}");
            assert!(
                rows.iter().any(|row| row.contains("YOLO")),
                "{height}\n{rows:#?}"
            );
        }
    }

    fn reading_session() -> UiState {
        let mut state = with_split(session());
        let mut catalog = crate::commands::tests::skills();
        catalog.entries = ["wisp-development", "playwright", "frontend-design"]
            .into_iter()
            .map(|name| wisp_protocol::events::SkillCatalogEntry {
                name: name.into(),
                description: format!("Instructions for {name}"),
                source: wisp_protocol::events::SkillSource::PackageWisp,
            })
            .collect();
        catalog.project_trusted = true;
        catalog.diagnostics.clear();
        state.skills.snapshot = Some(Arc::new(catalog));
        state.context.stats.as_mut().unwrap().used_skill_names = vec!["wisp-development".into()];
        state
    }

    #[test]
    fn empty_sections_disappear() {
        let mut lone = UiState::new("openai-codex".into(), None, None);
        lone.transcript
            .append_prompt("why does codex time out".into());
        lone.git_diff.snapshot = Some(changes(Vec::new()));
        let docked = panel_text(&screen(&lone, &mut SidePanel::default(), 200), 200);
        let focused = overlay_text(&lone);
        for text in [&docked, &focused] {
            assert!(text.contains("why does codex time out"), "{text}");
            for absent in [
                "Plan",
                "Running",
                "Queue",
                "Attention",
                "context",
                "MCP",
                "skills",
                "changed",
                "No changes",
                "reading…",
                "Recent turns",
            ] {
                assert!(!text.contains(absent), "{absent}\n{text}");
            }
        }
    }

    #[test]
    fn running_leads_with_plan_and_running_items() {
        let mut state = running();
        state.transcript.complete_message(
            2,
            "Plan:\n- [x] Inspect side panel\n- [x] Update layout\n- [ ] Verify tests".into(),
        );
        // Active subagent with a nested tool call.
        let subagent_call = crate::tool_cards::ToolCallInput {
            call_id: "sub-1".into(),
            name: "spawn_agent".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"task": "audit side panel tests"}),
        };
        state.transcript.observe_tool_call(subagent_call);
        assert!(state.transcript.observe_script_call("sub-1", |script| {
            script.call_started(
                "nested-1",
                "read",
                serde_json::json!({"path": "side_panel.rs"}),
            )
        }));
        // Running background process with retained output.
        backend(
            &mut state,
            crate::reducer::BackendEvent::from_projection_value(&serde_json::json!({
                "type": "rpc.state", "command_id": "get_state-1",
                "timestamp": "2026-11-15T09:00:00Z",
                "state": {
                    "provider": "fake", "auto_compaction_enabled": true,
                    "steering_mode": "one_at_a_time", "follow_up_mode": "one_at_a_time",
                    "pending_steering_count": 0, "pending_follow_up_count": 0,
                    "background_processes": [
                        {"process_id": "server", "command": "cargo watch -x test", "started_at_ms": 0}
                    ]
                }
            }))
            .unwrap(),
        );

        let docked = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        let focused = overlay_text(&state);
        for panel in [&docked, &focused] {
            for expected in [
                "Plan",
                "2 of 3",
                "✓ Inspect side panel",
                "○ Verify tests",
                "Running",
                "● audit side panel tests",
                "read",
                "1 ● cargo watch -x test",
            ] {
                assert!(panel.contains(expected), "{expected}\n{panel}");
            }
            assert!(row_of(panel, "Plan") < row_of(panel, "Running"), "{panel}");
            assert!(
                row_of(panel, "Running") < row_of(panel, "context"),
                "{panel}"
            );
        }

        // First row in Running is the background process (so focus_background lands on it);
        // Down moves to the subagent and Enter opens its transcript card.
        let mut panel = overlay(&state);
        focus_section(&mut panel, &state, Section::Running);
        panel.key(KeyCode::Down, &state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Subagent("sub-1".into()))
        );
    }

    fn set_linear(state: &mut UiState, status: McpServerStatus) {
        let snapshot = Arc::make_mut(state.mcp.snapshot.as_mut().unwrap());
        snapshot.servers[1].status = status;
    }

    #[test]
    fn attention_states_status_in_words() {
        let mut state = reading_session();
        let healthy = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(!healthy.contains("Attention"), "{healthy}");
        assert!(!healthy.contains("Disconnected"), "{healthy}");

        set_linear(&mut state, McpServerStatus::Disconnected);
        let disconnected = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(disconnected.contains("Attention"), "{disconnected}");
        assert!(disconnected.contains("linear"), "{disconnected}");
        assert!(
            disconnected.contains("Disconnected · /mcp"),
            "{disconnected}"
        );

        set_linear(&mut state, McpServerStatus::Connecting);
        let connecting = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(connecting.contains("Connecting · /mcp"), "{connecting}");

        set_linear(&mut state, McpServerStatus::Disabled);
        let disabled = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(!disabled.contains("Attention"), "{disabled}");
    }

    #[test]
    fn context_line_turns_amber_and_red() {
        let palette = Palette::default();
        let color_for = |tokens: u64| {
            let mut state = session();
            let budget = state.context.budget.as_mut().unwrap();
            budget.observed_tokens = Some(tokens);
            budget.context_window = Some(100_000);
            let mut panel = SidePanel::default();
            panel.observe_width(200);
            let mut terminal = Terminal::new(TestBackend::new(52, 20)).unwrap();
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        frame.area(),
                        &state,
                        &panel,
                        &Bindings::default(),
                        palette,
                    );
                })
                .unwrap();
            let buffer = terminal.backend().buffer();
            let y = (0..20)
                .find(|&y| {
                    (0..52)
                        .map(|x| buffer[(x, y)].symbol())
                        .collect::<String>()
                        .contains("context")
                })
                .expect("context line");
            buffer[(2, y)].fg
        };
        assert_eq!(color_for(14_000), palette.muted);
        assert_eq!(color_for(75_000), palette.warning);
        assert_eq!(color_for(92_000), palette.error);
    }

    #[test]
    fn inventory_summary_links_open_dialogs() {
        let state = reading_session();
        let text = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(text.contains("2 MCP · 3 skills · 2 changed"), "{text}");

        // Keyboard navigation reaches each part on the single summary line.
        let mut panel = overlay(&state);
        focus_section(&mut panel, &state, Section::Inventory);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Mcp)
        );
        panel.key(KeyCode::Right, &state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Skills)
        );
        panel.key(KeyCode::Right, &state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Diff(None))
        );
    }

    #[test]
    fn presentation_row_shows_fallback() {
        use wisp_protocol::events::CodemodeState;
        for (effective, expected) in [
            (
                CodemodePresentation::CodemodeOnly,
                "codemode only · automatic",
            ),
            (
                CodemodePresentation::DirectAndCodemode,
                "codemode only · using direct tools",
            ),
        ] {
            let mut state = UiState::unconfigured();
            state.codemode.snapshot = Some(Arc::new(CodemodeState {
                setting: CodemodeSetting::Automatic,
                presentation: CodemodePresentation::CodemodeOnly,
                effective_presentation: effective,
                available: true,
                offered: effective == CodemodePresentation::CodemodeOnly,
                env_setting: None,
                mcp_tools: 0,
                scriptable_mcp_tools: 0,
            }));
            let line = codemode_line(&state, 44, Palette::default()).unwrap();
            assert_eq!(
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>(),
                expected
            );
        }
    }

    #[test]
    fn codemode_row_names_setting() {
        use wisp_protocol::events::CodemodeState;
        let cases = [
            (
                CodemodeSetting::Automatic,
                true,
                true,
                Some("codemode on · automatic"),
            ),
            (CodemodeSetting::Automatic, true, false, None),
            (
                CodemodeSetting::Always,
                true,
                true,
                Some("codemode on · always"),
            ),
            (
                CodemodeSetting::Never,
                true,
                false,
                Some("codemode off · never"),
            ),
            (
                CodemodeSetting::Always,
                false,
                false,
                Some("codemode always · needs wisp-ai[codemode]"),
            ),
        ];
        for (setting, available, offered, expected) in cases {
            let mut state = UiState::unconfigured();
            assert!(codemode_line(&state, 44, Palette::default()).is_none());
            state.codemode.snapshot = Some(Arc::new(CodemodeState {
                setting,
                presentation: Default::default(),
                effective_presentation: Default::default(),
                available,
                offered,
                env_setting: None,
                mcp_tools: 0,
                scriptable_mcp_tools: 0,
            }));
            let text = codemode_line(&state, 44, Palette::default()).map(|line| {
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>()
            });
            assert_eq!(text.as_deref(), expected, "{setting:?}");
        }
    }

    #[test]
    fn small_viewports_render_without_panicking() {
        let state = reading_session();
        let mut panel = overlay(&state);
        for (width, height) in [(1, 1), (24, 8), (80, 24), (148, 40), (200, 48)] {
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            panel.observe_width(width);
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        frame.area(),
                        &state,
                        &panel,
                        &Bindings::default(),
                        Palette::default(),
                    )
                })
                .unwrap();
        }
    }

    #[test]
    fn reading_render_snapshot() {
        let state = reading_session();
        let mut panel = SidePanel::default();
        panel.observe_width(200);
        panel.sync_files(&state.git_diff);
        let mut terminal = Terminal::new(TestBackend::new(52, 40)).unwrap();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    frame.area(),
                    &state,
                    &panel,
                    &Bindings::default(),
                    Palette::default(),
                )
            })
            .unwrap();
        // Optional visual QA capture of the actual Ratatui cells, never a hand-built mock.
        if let Ok(path) = std::env::var("WISP_TEST_SIDEBAR_SNAPSHOT") {
            let buffer = terminal.backend().buffer();
            let cells: Vec<_> = buffer.content.iter().map(|cell| serde_json::json!({"text":cell.symbol(), "fg":format!("{:?}", cell.fg), "bg":format!("{:?}", cell.bg), "bold":cell.modifier.contains(Modifier::BOLD)})).collect();
            std::fs::write(
                path,
                serde_json::to_string(&serde_json::json!({"width":52,"height":40,"cells":cells}))
                    .unwrap(),
            )
            .unwrap();
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
        assert!(conversation.contains("idle │"), "{conversation}");
        assert!(!conversation.contains("/srv/wisp"), "{conversation}");
        assert!(!conversation.contains("ctx"), "{conversation}");
    }

    #[test]
    fn docked_panel_grows_capped() {
        for (total, panel) in [
            (148, 44),
            (160, 50),
            (161, 50),
            (180, 52),
            (200, 52),
            (240, 52),
        ] {
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
        // Never below the narrowest width, and clamped by the standardized drag ceiling.
        panel.continue_resize(DRAG, 190, now);
        assert_eq!(panel.docked_width(200), PANEL_WIDTH);
        panel.continue_resize(DRAG, 10, now);
        assert_eq!(panel.docked_width(200), max_dragged_width(200));
        panel.continue_resize(DRAG, 110, now);
        assert!(panel.continue_resize(RELEASE, 110, now));
        assert!(!panel.resizing());
        // A terminal that shrinks squeezes the width, and growing back restores it.
        assert_eq!(panel.docked_width(140), max_dragged_width(140));
        assert_eq!(panel.docked_width(200), 90);
        // With no resize in progress, drags belong to someone else.
        assert!(!panel.continue_resize(DRAG, 120, now));
    }

    #[test]
    fn expanded_area_and_toggle_apply_to_both_modes() {
        let expanded = expanded_area(Rect::new(0, 0, 160, 40));
        assert!(expanded.width >= 140 && expanded.width < 160);
        assert!(expanded.height >= 34 && expanded.height < 40);

        let mut panel = SidePanel::default();
        panel.observe_width(160);
        assert!(!panel.expanded());
        panel.toggle_expanded();
        assert!(panel.expanded());
        assert!(panel.overlay_open());
        panel.toggle_expanded();
        assert!(!panel.expanded());
        assert!(!panel.overlay_open());
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
        let edge_cell = |panel: &SidePanel| {
            let mut terminal = Terminal::new(TestBackend::new(200, 20)).unwrap();
            terminal
                .draw(|frame| {
                    let area = panel.split(frame.area()).1;
                    let bindings = Bindings::default();
                    render(frame, area, &session(), panel, &bindings, palette);
                })
                .unwrap();
            let cell = &terminal.backend().buffer()[(200 - panel.docked_width(200), 5)];
            (cell.symbol().to_owned(), cell.fg, cell.bg)
        };
        let mut panel = SidePanel::default();
        panel.observe_width(200);
        // At rest the docked panel uses the surface tier with no vertical divider line.
        let (sym, _, bg) = edge_cell(&panel);
        assert_eq!(sym, " ");
        assert_eq!(bg, palette.surface);
        // While dragging, the left edge highlights in the primary colour.
        panel.press_border(Instant::now(), 0);
        let (sym, fg, _) = edge_cell(&panel);
        assert_eq!(sym, "│");
        assert_eq!(fg, palette.primary);
    }

    #[test]
    fn undocked_footer_offers_panel() {
        let rows = screen(&session(), &mut SidePanel::default(), 140);
        let footer = rows.last().unwrap();
        assert!(!rows.iter().any(|row| row.contains("2 MCP · 2 changed")));
        assert!(footer.contains("/srv/wisp · fix/timeouts"), "{footer}");
        assert!(footer.contains("Ctrl+P panel"), "{footer}");
    }

    #[test]
    fn partial_snapshot_says_so() {
        let mut state = session();
        let mut snapshot = changes(vec![file("src/lib.rs", "modified", "a\n", "b\n")]);
        Arc::get_mut(&mut snapshot).unwrap().truncated = true;
        state.git_diff.snapshot = Some(snapshot);
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(panel.contains("1+ changed"), "{panel}");

        let mut empty = changes(Vec::new());
        Arc::get_mut(&mut empty).unwrap().truncated = true;
        state.git_diff.snapshot = Some(empty);
        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(!panel.contains("No changes"), "{panel}");
        assert!(panel.contains("0+ changed"), "{panel}");
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
        assert!(!panel_text(&rows, 200).contains("Ctrl+T tree"));
        assert!(
            rows.iter().any(|row| row.contains("Ctrl+T tree")),
            "{rows:#?}"
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
        state.run_progress = Some(crate::reducer::RunProgress {
            prompt,
            turn: 6,
            completed_output_tokens: 0,
            streaming_bytes: 0,
        });
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
                follow_up_image_counts: Vec::new(),
                steering_image_counts: Vec::new(),
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
    fn run_state_stays_in_footer() {
        let rows = screen(&session(), &mut SidePanel::default(), 200);
        let panel = panel_text(&rows, 200);
        let start = usize::from(200 - auto_width(200));
        let column: Vec<String> = rows
            .iter()
            .map(|row| row.chars().take(start).collect())
            .collect();
        assert!(!panel.contains("idle"), "{panel}");
        assert!(column.last().unwrap().contains("idle"), "{column:#?}");
    }

    /// Whether the panel has a `Now` section heading.
    fn has_now(panel: &str) -> bool {
        panel.lines().any(|line| line.trim() == "Now")
    }

    #[test]
    fn running_leaves_activity_to_status_line() {
        let rows = screen(&running(), &mut SidePanel::default(), 200);
        let panel = panel_text(&rows, 200);
        // The status line above the composer carries live run activity; the panel does not.
        assert!(!has_now(&panel), "{panel}");
        assert!(!panel.contains("Bash git push"), "{panel}");
        assert!(!panel.contains("turn 6"), "{panel}");
        assert!(
            rows.iter()
                .any(|row| row.contains("⠋ Bash git push -u origin fix/timeouts")),
            "{rows:#?}"
        );
        // An empty queue takes no rows.
        assert!(!panel.contains("Queue"), "{panel}");
    }

    #[test]
    fn background_lists_running() {
        let mut state = session();
        let process = |id: &str, command: &str| serde_json::json!({"process_id": id, "command": command, "started_at_ms": 0});
        let processes = vec![
            process("server", "npm run dev\u{1b}]0;owned\u{7}"),
            process("ci", "gh pr checks 1 --watch"),
        ];
        backend(
            &mut state,
            crate::reducer::BackendEvent::from_projection_value(&serde_json::json!({
                "type": "rpc.state", "command_id": "get_state-1",
                "timestamp": "2026-11-13T09:00:00Z",
                "state": {
                    "provider": "fake", "auto_compaction_enabled": true,
                    "steering_mode": "one_at_a_time", "follow_up_mode": "one_at_a_time",
                    "pending_steering_count": 0, "pending_follow_up_count": 0,
                    "background_processes": processes
                }
            }))
            .unwrap(),
        );

        let panel = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);

        assert!(panel.contains("Running"), "{panel}");
        assert!(panel.contains("1 ● npm run dev"), "{panel}");
        assert!(panel.contains("2 ● gh pr checks 1 --watch"), "{panel}");
        assert!(
            !panel.contains('\u{1b}') && !panel.contains('\u{7}'),
            "{panel:?}"
        );

        // Enter on a row opens its peek.
        let mut panel = overlay(&state);
        focus_section(&mut panel, &state, Section::Running);
        assert_eq!(panel.key(KeyCode::Enter, &state), OverlayKey::Moved);
        assert_eq!(panel.peek.as_deref(), Some("server"));
    }

    #[test]
    fn docked_lists_only_failed_exits() {
        let mut state = session();
        for (process_id, exit_code) in [("lint", 0), ("tests", 1)] {
            backend(
                &mut state,
                crate::reducer::BackendEvent::from_projection_value(&serde_json::json!({
                    "type": "process.ended", "timestamp": "2026-11-13T09:00:00Z",
                    "process_id": process_id, "command": format!("make {process_id}"),
                    "state": "completed", "exit_code": exit_code,
                    "duration_ms": 1_000, "notify": true,
                }))
                .unwrap(),
            );
        }

        // Docked, the clean exit is only counted.
        let docked = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(docked.contains("Running"), "{docked}");
        assert!(docked.contains("2 ended"), "{docked}");
        assert!(docked.contains("✗ make tests"), "{docked}");
        assert!(!docked.contains("make lint"), "{docked}");
        assert!(docked.contains("+1 more · /ps"), "{docked}");

        // Opened over the conversation, as `/ps` opens it, both are listed with [done].
        let focused = overlay_text(&state);
        assert!(focused.contains("✓ make lint"), "{focused}");
        assert!(focused.contains("[done]"), "{focused}");
        assert!(focused.contains("✗ make tests"), "{focused}");
        assert!(!focused.contains("more · /ps"), "{focused}");
    }

    #[test]
    fn idle_panel_has_no_background() {
        let panel = panel_text(&screen(&session(), &mut SidePanel::default(), 200), 200);
        assert!(!panel.contains("Running"), "{panel}");
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
        assert!(
            row_of(&panel, "Queue") < row_of(&panel, "context"),
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
        ] {
            assert!(panel.contains(expected), "{expected}\n{panel}");
        }
        assert!(
            row_of(&panel, "Needs approval") < row_of(&panel, "Queue"),
            "{panel}"
        );
        assert!(
            row_of(&panel, "Queue") < row_of(&panel, "context"),
            "{panel}"
        );
        // The run is paused, not running, and the mode is stated once.
        assert!(!panel.contains("Now"), "{panel}");
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
        assert!(!has_now(&panel), "{panel}");
        assert!(panel.contains("context"), "{panel}");
        assert!(
            panel.contains("Esc close · Tab sections · Enter"),
            "{panel}"
        );
        let panel = overlay_text(&approving());
        assert!(
            row_of(&panel, "Needs approval") < row_of(&panel, "Queue"),
            "{panel}"
        );
        assert!(
            row_of(&panel, "Queue") < row_of(&panel, "context"),
            "{panel}"
        );
    }

    /// The overlay open on `state` at a normal width, its files counted.
    fn overlay(state: &UiState) -> SidePanel {
        let mut panel = SidePanel::default();
        panel.observe_width(140);
        panel.toggle();
        panel.sync_files(&state.git_diff);
        paint_overlay(state, &panel);
        panel
    }

    fn paint_overlay(state: &UiState, panel: &SidePanel) {
        let mut terminal = Terminal::new(TestBackend::new(140, 40)).unwrap();
        terminal
            .draw(|frame| {
                render(
                    frame,
                    overlay_area(frame.area()),
                    state,
                    panel,
                    &Bindings::default(),
                    Palette::default(),
                )
            })
            .unwrap();
    }

    fn focus_section(panel: &mut SidePanel, state: &UiState, section: Section) {
        for _ in 0..8 {
            if panel
                .focused(state)
                .is_some_and(|cursor| cursor.section == section)
            {
                return;
            }
            panel.key(KeyCode::Tab, state);
        }
        panic!("missing section {section:?}");
    }

    #[test]
    fn tab_cycles_sections() {
        let mut state = running();
        state.context.stats.as_mut().unwrap().branch = SessionBranchSummary {
            branches: 2,
            ..Default::default()
        };
        queue(&mut state, &["steer"], &["later"]);
        let mut panel = overlay(&state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Tree)
        );
        for section in [
            Section::Queue,
            Section::Context,
            Section::Inventory,
            Section::Header,
        ] {
            panel.key(KeyCode::Tab, &state);
            assert_eq!(panel.focused(&state).unwrap().section, section);
        }
        panel.key(KeyCode::BackTab, &state);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Mcp)
        );
        assert_eq!(panel.key(KeyCode::Char('x'), &state), OverlayKey::Ignored);
        assert_eq!(panel.key(KeyCode::Esc, &state), OverlayKey::Close);
    }

    #[test]
    fn focus_lists_every_process() {
        let mut state = session();
        let processes: Vec<_> = (1..=8)
            .map(|n| serde_json::json!({"process_id": format!("p{n}"), "command": format!("job {n}"), "started_at_ms": 0}))
            .collect();
        backend(
            &mut state,
            crate::reducer::BackendEvent::from_projection_value(&serde_json::json!({
                "type": "rpc.state", "command_id": "get_state-1",
                "timestamp": "2026-11-15T09:00:00Z",
                "state": {
                    "provider": "fake", "auto_compaction_enabled": true,
                    "steering_mode": "one_at_a_time", "follow_up_mode": "one_at_a_time",
                    "pending_steering_count": 0, "pending_follow_up_count": 0,
                    "background_processes": processes
                }
            }))
            .unwrap(),
        );

        // The docked panel shows five and points at /ps for the rest.
        let docked = panel_text(&screen(&state, &mut SidePanel::default(), 200), 200);
        assert!(docked.contains("5 ● job 5"), "{docked}");
        assert!(!docked.contains("job 6"), "{docked}");
        assert!(docked.contains("+3 more · /ps"), "{docked}");

        // Focused, every process is listed and the last one can be selected.
        let mut panel = SidePanel::default();
        panel.observe_width(140);
        panel.focus_background();
        paint_overlay(&state, &panel);
        panel.key(KeyCode::End, &state);
        panel.key(KeyCode::Enter, &state);
        assert_eq!(panel.peek.as_deref(), Some("p8"));
    }

    #[test]
    fn background_focus_skips_queue() {
        let mut state = running();
        queue(&mut state, &["steer"], &[]);
        backend(
            &mut state,
            crate::reducer::BackendEvent::from_projection_value(&serde_json::json!({
                "type": "rpc.state", "command_id": "get_state-1",
                "timestamp": "2026-11-15T09:00:00Z",
                "state": {
                    "provider": "fake", "auto_compaction_enabled": true,
                    "steering_mode": "one_at_a_time", "follow_up_mode": "one_at_a_time",
                    "pending_steering_count": 1, "pending_follow_up_count": 0,
                    "background_processes": [
                        {"process_id": "server", "command": "npm run dev", "started_at_ms": 0}
                    ]
                }
            }))
            .unwrap(),
        );
        let mut panel = SidePanel::default();
        panel.observe_width(140);
        panel.focus_background();
        paint_overlay(&state, &panel);

        // Focus starts on the running process.
        assert_eq!(panel.focused(&state).unwrap().section, Section::Running);
        panel.key(KeyCode::Enter, &state);
        assert_eq!(panel.peek.as_deref(), Some("server"));
    }

    #[test]
    fn focus_skips_empty_sections() {
        let mut state = running();
        queue(&mut state, &["steer"], &[]);
        let mut panel = overlay(&state);
        assert_eq!(panel.focused(&state).unwrap().section, Section::Queue);
        queue(&mut state, &[], &[]);
        paint_overlay(&state, &panel);
        assert_eq!(panel.focused(&state).unwrap().section, Section::Context);
        focus_section(&mut panel, &state, Section::Inventory);
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
        focus_section(&mut panel, &state, Section::Queue);
        assert_eq!(
            panel.key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Queue)
        );
    }

    #[test]
    fn overlay_marks_focus() {
        let state = session();
        let selection = Palette::default().selection().bg;
        let panel = overlay(&state);
        assert_eq!(row_background(&panel, &state, 140, "context"), selection);
        let mut docked = SidePanel::default();
        docked.observe_width(200);
        docked.sync_files(&state.git_diff);
        assert_ne!(row_background(&docked, &state, 200, "context"), selection);
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
            completed_output_tokens: 0,
            streaming_bytes: 0,
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
        state.run_progress = Some(crate::reducer::RunProgress {
            prompt,
            turn: 3,
            completed_output_tokens: 0,
            streaming_bytes: 0,
        });
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
        assert!(
            !panel.contains("this turn"),
            "cost details stay behind Breakdown"
        );

        // An unpriced reply or a web search leaves only a floor.
        state
            .transcript
            .complete_message_with_cost(5, "Searched.".into(), Some(cost(None, false)));
        assert_eq!(turn(&state).as_deref(), Some("≥$0.310"));
    }

    #[test]
    fn visibility_reports_once() {
        let mut panel = SidePanel::default();
        panel.observe_width(200);
        assert_eq!(panel.take_visibility_change(), Some(true));
        assert_eq!(panel.take_visibility_change(), None);
        panel.toggle();
        assert_eq!(panel.take_visibility_change(), Some(false));
        // Narrowing leaves the panel hidden until Ctrl+P opens the overlay.
        panel.observe_width(100);
        assert_eq!(panel.take_visibility_change(), None);
        panel.toggle();
        assert!(panel.overlay_open());
        assert_eq!(panel.take_visibility_change(), Some(true));
    }
}
