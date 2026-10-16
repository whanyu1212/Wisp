//! `/mcp`: browse MCP servers and add, remove, enable, disable, or reconnect them.
//!
//! Browsing happens in the centered overlay. Adding, enabling, and removing open
//! a panel docked above the composer. Like a pending approval, its choices have no
//! default: Enter acts only on a highlighted choice that has been painted, and
//! number keys act only once the panel has been painted, so a key pressed before
//! the panel appeared cannot confirm it.
//!
//! A pasted snippet is sent to the backend for parsing and not kept. Environment
//! and header values the user types are held as [`SecretText`], rendered only as
//! bullets, and sent once in a secret command.
//!
//! `l` signs in to an HTTP server with OAuth: the backend opens the sign-in page
//! in the browser, and the list shows its URL until the browser returns. Esc
//! cancels a sign-in in progress instead of closing the view.

use crate::{
    discovery_view::{DiscoveryAction, ReportView},
    reducer::{McpChange, SecretText, UiAction, UiState},
    session_picker::terminal_row,
    theme::Palette,
    ui::{choice_line, sanitize_for_terminal},
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Borders, Padding, Paragraph},
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::{
    McpServerLaunch, McpServerPreview, McpServerSnapshot, McpServerStatus, McpSignIn,
};

/// Largest environment value the backend accepts.
pub(crate) const MCP_ENV_VALUE_MAX_BYTES: usize = 16_384;
/// Largest header value the backend accepts, counted in bytes so it also fits
/// its character limit.
const MCP_HEADER_VALUE_MAX_BYTES: usize = 8_192;
/// Largest snippet the backend accepts, counted in bytes so it also fits its
/// character limit.
pub(crate) const MCP_SNIPPET_MAX_BYTES: usize = 65_536;
/// Shown instead of a confirmation's details when they do not all fit.
const ENLARGE_HINT: &str = "Make the terminal larger to see everything before confirming.";

#[derive(Default)]
pub(crate) struct McpView {
    selected: Option<String>,
    /// The server painted as selected; `e` and `d` act only on it.
    rendered: Option<String>,
    detail: ReportView,
    flow: Option<Flow>,
    notice: Option<String>,
}

/// A choice with no default, armed only once painted.
#[derive(Default)]
struct Pick {
    selected: Option<usize>,
    painted: bool,
    rendered: Option<usize>,
}

impl Pick {
    fn step(&mut self, code: KeyCode, count: usize) {
        self.selected = Some(match (self.selected, code) {
            (None, KeyCode::Down) => 0,
            (None, _) => count - 1,
            (Some(index), KeyCode::Down) => (index + 1) % count,
            (Some(index), _) => (index + count - 1) % count,
        });
    }

    /// The choice a key confirms, if the panel showing it has been painted.
    fn chosen(&self, code: KeyCode, count: usize) -> Option<usize> {
        if !self.painted {
            return None;
        }
        match code {
            KeyCode::Enter => self.selected.filter(|index| self.rendered == Some(*index)),
            KeyCode::Char(key) => key
                .to_digit(10)
                .and_then(|digit| usize::try_from(digit).ok())
                .filter(|digit| (1..=count).contains(digit))
                .map(|digit| digit - 1),
            _ => None,
        }
    }

    fn invalidate(&mut self) {
        self.painted = false;
        self.rendered = None;
    }

    fn mark_painted(&mut self, visible: bool) {
        self.painted = visible;
        self.rendered = if visible { self.selected } else { None };
    }
}

enum Flow {
    /// Waiting for the user to paste a snippet.
    Paste,
    /// The snippet was sent to the backend to be parsed.
    Previewing,
    Env(EnvStep),
    Confirm(Confirm),
}

/// Asking, one at a time, for the values a snippet's server needs: how to
/// provide each environment variable, then each HTTP header's value. The
/// snippet's own values are never used: they are usually placeholders.
struct EnvStep {
    server: McpServerPreview,
    replace: bool,
    answers: Vec<(String, EnvAnswer)>,
    /// Header values entered so far, in `launch.header_names` order.
    headers: Vec<(String, SecretText)>,
    pick: Pick,
    /// The value being typed, once the user chose to enter one. A header value
    /// is always typed.
    input: Option<String>,
}

/// What the current step asks for.
enum Ask<'a> {
    Env(&'a str),
    Header(&'a str),
}

enum EnvAnswer {
    Forward,
    Value(SecretText),
}

impl EnvStep {
    fn new(server: McpServerPreview, replace: bool) -> Self {
        let mut step = Self {
            server,
            replace,
            answers: Vec::new(),
            headers: Vec::new(),
            pick: Pick::default(),
            input: None,
        };
        step.begin_header();
        step
    }

    fn current(&self) -> Ask<'_> {
        let launch = &self.server.launch;
        match launch.env_names.get(self.answers.len()) {
            Some(name) => Ask::Env(name),
            None => Ask::Header(&launch.header_names[self.headers.len()]),
        }
    }

    /// The question being asked, counting from one, and how many there are.
    fn position(&self) -> (usize, usize) {
        let launch = &self.server.launch;
        (
            self.answers.len() + self.headers.len() + 1,
            launch.env_names.len() + launch.header_names.len(),
        )
    }

    fn finished(&self) -> bool {
        let launch = &self.server.launch;
        self.answers.len() == launch.env_names.len()
            && self.headers.len() == launch.header_names.len()
    }

    /// Start typing when the next question is a header's value.
    fn begin_header(&mut self) {
        if !self.finished() && matches!(self.current(), Ask::Header(_)) {
            self.input = Some(String::new());
        }
    }
}

struct Confirm {
    kind: ConfirmKind,
    pick: Pick,
}

enum ConfirmKind {
    Add {
        name: String,
        launch: McpServerLaunch,
        env: Vec<(String, SecretText)>,
        headers: Vec<(String, SecretText)>,
        replace: bool,
    },
    Enable {
        name: String,
        launch: McpServerLaunch,
    },
    Remove {
        name: String,
    },
}

impl ConfirmKind {
    fn title(&self) -> String {
        match self {
            Self::Add { name, replace, .. } if *replace => format!("Replace MCP server {name}"),
            Self::Add { name, .. } => format!("Add MCP server {name}"),
            Self::Enable { name, .. } => format!("Enable MCP server {name}"),
            Self::Remove { name } => format!("Remove MCP server {name}"),
        }
    }

    fn choices(&self) -> [&'static str; 2] {
        match self {
            Self::Add { .. } => ["Save and connect", "Cancel"],
            Self::Enable { .. } => ["Enable and connect", "Cancel"],
            Self::Remove { .. } => ["Remove", "Keep"],
        }
    }

    fn footnote(&self) -> Option<&'static str> {
        match self {
            Self::Add { launch, .. } | Self::Enable { launch, .. } if launch.command.is_some() => {
                Some("Wisp runs this command every time it starts.")
            }
            Self::Remove { .. } => Some("Its settings are deleted and the server stops."),
            _ => None,
        }
    }

    fn launch(&self) -> Option<&McpServerLaunch> {
        match self {
            Self::Add { launch, .. } | Self::Enable { launch, .. } => Some(launch),
            Self::Remove { .. } => None,
        }
    }

    /// The change confirming sends; it consumes the entered values.
    fn into_change(self) -> McpChange {
        match self {
            Self::Add {
                name,
                launch,
                env,
                headers,
                replace,
            } => McpChange::Add {
                name,
                launch,
                env,
                headers,
                replace,
            },
            Self::Enable { name, launch } => McpChange::SetEnabled {
                name,
                enabled: true,
                expected: Some(launch),
            },
            Self::Remove { name } => McpChange::Remove(name),
        }
    }
}

impl McpView {
    /// Whether the view shows a docked panel instead of the server list.
    pub fn docked(&self) -> bool {
        self.flow.is_some()
    }

    /// Whether `key` would act on a painted choice.
    pub fn key_activates(&self, key: KeyEvent) -> bool {
        match &self.flow {
            // Digits are text while a value is typed; only Enter submits it.
            Some(Flow::Env(EnvStep { input: Some(_), .. })) => key.code == KeyCode::Enter,
            Some(Flow::Env(_) | Flow::Confirm(_)) => {
                matches!(key.code, KeyCode::Enter | KeyCode::Char('1' | '2'))
            }
            _ => matches!(key.code, KeyCode::Char('d' | 'e' | 'l' | 'o')),
        }
    }

    pub fn invalidate(&mut self) {
        self.rendered = None;
        match &mut self.flow {
            Some(Flow::Env(step)) => step.pick.invalidate(),
            Some(Flow::Confirm(confirm)) => confirm.pick.invalidate(),
            _ => {}
        }
    }

    /// Move past a finished snippet preview. Called before the view is measured
    /// and drawn, so the panel's height matches the step it shows.
    pub fn advance(&mut self, state: &UiState) {
        if !matches!(self.flow, Some(Flow::Previewing)) || state.mcp_preview.loading() {
            return;
        }
        if let Some(error) = &state.mcp_preview.error {
            self.notice = Some(error.clone());
            self.flow = Some(Flow::Paste);
            return;
        }
        let Some(servers) = state.mcp_preview.snapshot.as_deref() else {
            return;
        };
        let [server] = servers.as_slice() else {
            self.notice = Some(format!(
                "This snippet has {} servers. Paste one at a time, or run `wisp mcp import` \
                 to add several.",
                servers.len()
            ));
            self.flow = Some(Flow::Paste);
            return;
        };
        let replace = servers_of(state)
            .iter()
            .any(|known| known.name == server.name);
        let step = EnvStep::new(server.clone(), replace);
        self.notice = None;
        self.flow = Some(if step.finished() {
            Flow::Confirm(confirm_add(step))
        } else {
            Flow::Env(step)
        });
    }

    pub fn handle_key(&mut self, key: KeyEvent, state: &UiState) -> DiscoveryAction {
        self.advance(state);
        if key.code == KeyCode::Char('c') && key.modifiers == KeyModifiers::CONTROL {
            return DiscoveryAction::Close;
        }
        let Some(flow) = self.flow.take() else {
            return self.browse_key(key, state);
        };
        let (flow, action) = self.flow_key(flow, key);
        self.flow = flow;
        action
    }

    /// Show why the last request was not sent. A snippet that was not sent for
    /// reading returns to the paste step instead of waiting for a reply.
    pub fn request_rejected(&mut self, notice: &str) {
        if matches!(self.flow, Some(Flow::Previewing)) {
            self.flow = Some(Flow::Paste);
        }
        self.notice = Some(notice.to_owned());
    }

    /// Take a pasted snippet, or paste into the value being typed.
    pub fn handle_paste(&mut self, text: &str, state: &UiState) -> DiscoveryAction {
        match &mut self.flow {
            Some(Flow::Paste) => {
                if text.trim().is_empty() {
                    return DiscoveryAction::None;
                }
                // Its reply would be taken for this snippet's.
                if state.mcp_preview.loading() {
                    self.notice = Some("Still reading the previous snippet; paste again.".into());
                    return DiscoveryAction::None;
                }
                let Some(snippet) = SecretText::new(text.to_owned(), MCP_SNIPPET_MAX_BYTES) else {
                    self.notice = Some("The snippet is too large to read.".into());
                    return DiscoveryAction::None;
                };
                self.notice = None;
                self.flow = Some(Flow::Previewing);
                DiscoveryAction::Refresh(UiAction::PreviewMcpSnippet(snippet))
            }
            Some(Flow::Env(step @ EnvStep { input: Some(_), .. })) => {
                let limit = if matches!(step.current(), Ask::Header(_)) {
                    MCP_HEADER_VALUE_MAX_BYTES
                } else {
                    MCP_ENV_VALUE_MAX_BYTES
                };
                let Some(value) = step.input.as_mut() else {
                    return DiscoveryAction::None;
                };
                // A copied token often ends with a newline; keep the value on one line.
                for character in text.chars().filter(|character| !character.is_control()) {
                    if value.len() + character.len_utf8() > limit {
                        break;
                    }
                    value.push(character);
                }
                DiscoveryAction::None
            }
            _ => DiscoveryAction::None,
        }
    }

    fn browse_key(&mut self, key: KeyEvent, state: &UiState) -> DiscoveryAction {
        if key.code == KeyCode::Esc {
            // Esc first stops waiting for a sign-in; once that was asked for, it
            // closes the view while the cancel finishes.
            if state
                .mcp_sign_in
                .as_ref()
                .is_some_and(|sign_in| !sign_in.cancel_requested)
            {
                return DiscoveryAction::Refresh(UiAction::CancelMcpSignIn);
            }
            return DiscoveryAction::Close;
        }
        if key.modifiers != KeyModifiers::NONE {
            return DiscoveryAction::None;
        }
        let servers = servers_of(state);
        match key.code {
            KeyCode::Char('r') => DiscoveryAction::Refresh(UiAction::LoadMcpStatus),
            KeyCode::Char('c') => {
                DiscoveryAction::Refresh(UiAction::ChangeMcpServers(McpChange::Reconnect))
            }
            KeyCode::Char('a') => {
                if self.ready_for_change(state) {
                    self.notice = None;
                    self.flow = Some(Flow::Paste);
                }
                DiscoveryAction::None
            }
            KeyCode::Char(key @ ('l' | 'o')) => {
                let Some(server) = self
                    .rendered
                    .as_deref()
                    .and_then(|name| servers.iter().find(|server| server.name == name))
                else {
                    return DiscoveryAction::None;
                };
                let change = match (key, server.sign_in) {
                    ('l', Some(_)) => McpChange::Login(server.name.clone()),
                    // A disabled or failed server may still have a saved sign-in.
                    ('o', Some(_)) => McpChange::Logout(server.name.clone()),
                    ('l', None) => {
                        let has_url = server
                            .launch
                            .as_ref()
                            .is_some_and(|launch| launch.url.is_some());
                        self.notice = Some(if has_url {
                            "This server sends its own Authorization header, so it does not sign in."
                                .into()
                        } else {
                            "Only servers added by URL sign in. To use a hosted server's OAuth, \
                             remove this one and add the server by its URL."
                                .into()
                        });
                        return DiscoveryAction::None;
                    }
                    _ => {
                        self.notice = Some(format!("{} does not sign in.", server.name));
                        return DiscoveryAction::None;
                    }
                };
                if !self.ready_for_change(state) {
                    return DiscoveryAction::None;
                }
                self.notice = None;
                DiscoveryAction::Refresh(UiAction::ChangeMcpServers(change))
            }
            KeyCode::Char(key @ ('d' | 'e')) => {
                let Some(server) = self
                    .rendered
                    .as_deref()
                    .and_then(|name| servers.iter().find(|server| server.name == name))
                else {
                    return DiscoveryAction::None;
                };
                let name = server.name.clone();
                let disabling = key == 'e' && server.status != McpServerStatus::Disabled;
                // A change made without a confirmation reports busy itself.
                if !disabling && !self.ready_for_change(state) {
                    return DiscoveryAction::None;
                }
                if key == 'd' {
                    self.open_confirm(ConfirmKind::Remove { name });
                    return DiscoveryAction::None;
                }
                if disabling {
                    return DiscoveryAction::Refresh(UiAction::ChangeMcpServers(
                        McpChange::SetEnabled {
                            name,
                            enabled: false,
                            expected: None,
                        },
                    ));
                }
                match server.launch.clone() {
                    Some(launch) => self.open_confirm(ConfirmKind::Enable { name, launch }),
                    None => {
                        self.notice =
                            Some("Refresh with r to see what this server runs first.".into());
                    }
                }
                DiscoveryAction::None
            }
            KeyCode::Up | KeyCode::Down => {
                let names: Vec<&str> = servers.iter().map(|server| server.name.as_str()).collect();
                if names.is_empty() {
                    return DiscoveryAction::None;
                }
                let current = self
                    .selected
                    .as_deref()
                    .and_then(|name| names.iter().position(|known| *known == name))
                    .unwrap_or(0);
                let next = if key.code == KeyCode::Down {
                    (current + 1).min(names.len() - 1)
                } else {
                    current.saturating_sub(1)
                };
                self.selected = Some(names[next].to_owned());
                // `e` and `d` must wait for the new selection to be painted.
                self.rendered = None;
                self.detail = ReportView::default();
                DiscoveryAction::None
            }
            code => {
                self.detail.scroll(code);
                DiscoveryAction::None
            }
        }
    }

    /// Whether a change can start now, so a confirmation is not built (and
    /// values typed) only to be refused. Sets a notice when it cannot.
    fn ready_for_change(&mut self, state: &UiState) -> bool {
        let reason = if state.mcp.loading() {
            "Wait for the current MCP update to finish."
        } else if !state.can_select_model() {
            "Finish the current operation before changing MCP servers."
        } else {
            return true;
        };
        self.notice = Some(reason.into());
        false
    }

    fn open_confirm(&mut self, kind: ConfirmKind) {
        self.notice = None;
        self.flow = Some(Flow::Confirm(Confirm {
            kind,
            pick: Pick::default(),
        }));
    }

    fn flow_key(&mut self, flow: Flow, key: KeyEvent) -> (Option<Flow>, DiscoveryAction) {
        match flow {
            Flow::Paste | Flow::Previewing => {
                if key.code == KeyCode::Esc {
                    self.notice = None;
                    return (None, DiscoveryAction::None);
                }
                (Some(flow), DiscoveryAction::None)
            }
            Flow::Env(step) => self.env_key(step, key),
            Flow::Confirm(mut confirm) => {
                if key.code == KeyCode::Esc {
                    return (None, DiscoveryAction::None);
                }
                if matches!(key.code, KeyCode::Up | KeyCode::Down) {
                    confirm.pick.step(key.code, 2);
                    return (Some(Flow::Confirm(confirm)), DiscoveryAction::None);
                }
                match confirm.pick.chosen(key.code, 2) {
                    Some(0) => {
                        if let ConfirmKind::Add { name, .. } = &confirm.kind {
                            self.selected = Some(name.clone());
                        }
                        let change = confirm.kind.into_change();
                        (
                            None,
                            DiscoveryAction::Refresh(UiAction::ChangeMcpServers(change)),
                        )
                    }
                    Some(_) => (None, DiscoveryAction::None),
                    None => (Some(Flow::Confirm(confirm)), DiscoveryAction::None),
                }
            }
        }
    }

    fn env_key(&mut self, mut step: EnvStep, key: KeyEvent) -> (Option<Flow>, DiscoveryAction) {
        let header = matches!(step.current(), Ask::Header(_));
        if let Some(value) = &mut step.input {
            match key.code {
                // A header has no other way to be provided, so Esc stops adding.
                KeyCode::Esc if header => return (None, DiscoveryAction::None),
                KeyCode::Esc => step.input = None,
                KeyCode::Backspace => {
                    value.pop();
                }
                KeyCode::Enter => {
                    let mut typed = step.input.take().unwrap_or_default();
                    if header {
                        // HTTP refuses surrounding spaces, and a copied token
                        // often has one; an empty value can never be sent.
                        typed = typed.trim_matches([' ', '\t']).to_owned();
                    }
                    let limit = if header {
                        MCP_HEADER_VALUE_MAX_BYTES
                    } else {
                        MCP_ENV_VALUE_MAX_BYTES
                    };
                    let secret = if header && typed.is_empty() {
                        None
                    } else {
                        SecretText::env_value(typed, limit)
                    };
                    let name = match step.current() {
                        Ask::Env(name) | Ask::Header(name) => name.to_owned(),
                    };
                    match secret {
                        Some(secret) if header => step.headers.push((name, secret)),
                        Some(secret) => {
                            step.answers.push((name, EnvAnswer::Value(secret)));
                            step.pick = Pick::default();
                        }
                        None => step.input = Some(String::new()),
                    }
                    step.begin_header();
                }
                KeyCode::Char(character)
                    if matches!(key.modifiers, KeyModifiers::NONE | KeyModifiers::SHIFT)
                        && value.len() + character.len_utf8()
                            <= if header {
                                MCP_HEADER_VALUE_MAX_BYTES
                            } else {
                                MCP_ENV_VALUE_MAX_BYTES
                            } =>
                {
                    value.push(character);
                }
                _ => {}
            }
            return (Some(Self::next_env_flow(step)), DiscoveryAction::None);
        }
        if key.code == KeyCode::Esc {
            return (None, DiscoveryAction::None);
        }
        if matches!(key.code, KeyCode::Up | KeyCode::Down) {
            step.pick.step(key.code, 2);
            return (Some(Flow::Env(step)), DiscoveryAction::None);
        }
        match step.pick.chosen(key.code, 2) {
            Some(0) => {
                if let Ask::Env(name) = step.current() {
                    let name = name.to_owned();
                    step.answers.push((name, EnvAnswer::Forward));
                }
                step.pick = Pick::default();
                step.begin_header();
            }
            Some(_) => step.input = Some(String::new()),
            None => {}
        }
        (Some(Self::next_env_flow(step)), DiscoveryAction::None)
    }

    fn next_env_flow(step: EnvStep) -> Flow {
        if step.finished() {
            Flow::Confirm(confirm_add(step))
        } else {
            Flow::Env(step)
        }
    }

    /// Rows the docked panel needs when it is `width` columns wide, borders
    /// included. A confirmation asks for every wrapped detail row, so a long
    /// command is shown whole whenever the terminal has room.
    pub fn panel_height(&self, width: u16) -> u16 {
        match &self.flow {
            None => 0,
            Some(Flow::Paste | Flow::Previewing | Flow::Env(_)) => 6,
            Some(Flow::Confirm(confirm)) => {
                let rows = confirm_details(&confirm.kind, panel_width(width)).len();
                let footnote = if confirm.kind.footnote().is_some() {
                    2
                } else {
                    0
                };
                // Borders, details and a gap, two choices, then the footnote.
                u16::try_from(2 + rows + usize::from(rows > 0) + 2 + footnote).unwrap_or(u16::MAX)
            }
        }
    }

    pub fn render(&mut self, frame: &mut Frame<'_>, area: Rect, state: &UiState, palette: Palette) {
        let notice = self.notice.as_deref();
        match &mut self.flow {
            None => self.render_browse(frame, area, state, palette),
            Some(Flow::Paste) => render_paste(frame, area, false, notice, palette),
            Some(Flow::Previewing) => render_paste(frame, area, true, notice, palette),
            Some(Flow::Env(step)) => render_env(frame, area, step, palette),
            Some(Flow::Confirm(confirm)) => render_confirm(frame, area, confirm, palette),
        }
    }

    fn render_browse(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        state: &UiState,
        palette: Palette,
    ) {
        let hint = match &state.mcp_sign_in {
            Some(sign_in) if !sign_in.cancel_requested => " Esc cancel sign-in ",
            Some(_) => " Esc close ",
            None => [
                " ↑↓ select · a add · e enable/disable · d remove · l sign in · o sign out · c reconnect · r refresh · Esc close ",
                " a add · e on/off · d remove · l/o sign in/out · c reconnect · Esc close ",
                " a e d l o c r · Esc ",
            ]
            .into_iter()
            // Borders and title corners take the rest of the row.
            .find(|hint| hint.width() + 4 <= usize::from(area.width))
            .unwrap_or(" Esc "),
        };
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(palette.border())
            .title(" MCP servers ")
            .title_bottom(Line::from(hint).style(Style::default().fg(palette.muted)));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        self.rendered = None;
        if inner.width == 0 || inner.height == 0 {
            return;
        }
        let width = usize::from(inner.width);
        let mut status: Vec<Line<'static>> = Vec::new();
        if state.mcp.loading() {
            status.push(Line::styled(
                terminal_row("Updating MCP servers…", width),
                Style::default().fg(palette.muted),
            ));
        }
        if let Some(error) = &state.mcp.error {
            status.push(Line::styled(
                terminal_row(&format!("Error: {error}"), width),
                Style::default().fg(palette.warning),
            ));
        }
        if let Some(notice) = &self.notice {
            status.push(Line::styled(
                terminal_row(notice, width),
                Style::default().fg(palette.warning),
            ));
        }
        if let Some(sign_in) = &state.mcp_sign_in {
            status.push(sign_in_status(sign_in, width, palette));
        }
        let Some(snapshot) = state.mcp.snapshot.as_deref() else {
            status.push(Line::raw(terminal_row("MCP status unavailable.", width)));
            frame.render_widget(Paragraph::new(status), inner);
            return;
        };
        let servers = &snapshot.servers;
        if servers.is_empty() {
            status.push(Line::raw(terminal_row(
                "No MCP servers configured. Press a to add one.",
                width,
            )));
            frame.render_widget(Paragraph::new(status), inner);
            return;
        }
        let selected = self
            .selected
            .as_deref()
            .and_then(|name| servers.iter().position(|server| server.name == name))
            .unwrap_or(0);
        self.selected = Some(servers[selected].name.clone());

        let available = usize::from(inner.height).saturating_sub(status.len());
        let list_rows = servers.len().min((available / 2).max(1)).min(available);
        let [header, list, detail] = Layout::vertical([
            Constraint::Length(u16::try_from(status.len()).unwrap_or(u16::MAX)),
            Constraint::Length(u16::try_from(list_rows).unwrap_or(u16::MAX)),
            Constraint::Min(0),
        ])
        .areas(inner);
        frame.render_widget(Paragraph::new(status), header);

        // Keep the selected row in view.
        let first = selected.saturating_sub(list_rows.saturating_sub(1));
        let rows: Vec<Line<'static>> = servers
            .iter()
            .enumerate()
            .skip(first)
            .take(list_rows)
            .map(|(index, server)| server_row(server, index == selected, width, palette))
            .collect();
        frame.render_widget(Paragraph::new(rows), list);
        if list.height > 0 {
            self.rendered = self.selected.clone();
        }
        if detail.height > 1 {
            let [_, body] =
                Layout::vertical([Constraint::Length(1), Constraint::Min(0)]).areas(detail);
            // While a sign-in waits, its page's address takes the details, where a
            // long one wraps whole and scrolls instead of crowding out the list.
            let rows = match &state.mcp_sign_in {
                Some(sign_in) => sign_in_detail(sign_in),
                None => server_detail(&servers[selected]),
            };
            self.detail.render(frame, body, &rows);
        }
    }
}

/// The status row of a sign-in in progress.
fn sign_in_status(
    sign_in: &crate::reducer::McpSignInState,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let text = if sign_in.cancel_requested {
        format!("Cancelling sign-in to {}…", sign_in.name)
    } else {
        format!("Signing in to {}: finish in your browser.", sign_in.name)
    };
    Line::styled(
        terminal_row(&text, width),
        Style::default().fg(palette.primary),
    )
}

/// What a sign-in in progress waits for. The sign-in page's address is shown
/// whole, so it can be copied when no browser opened; the browser must run on
/// this machine, because the page returns to a local address.
fn sign_in_detail(sign_in: &crate::reducer::McpSignInState) -> Vec<String> {
    match &sign_in.url {
        Some(url) => vec![
            "If no browser opened, open this address in a browser on this machine:".into(),
            url.clone(),
        ],
        None => vec!["Finding the sign-in page…".into()],
    }
}

fn servers_of(state: &UiState) -> &[McpServerSnapshot] {
    state
        .mcp
        .snapshot
        .as_deref()
        .map_or(&[][..], |snapshot| snapshot.servers.as_slice())
}

/// Build the add confirmation from a finished environment step: forwarded
/// variables join `env_from`, and entered values travel separately.
fn confirm_add(step: EnvStep) -> Confirm {
    let EnvStep {
        server,
        replace,
        answers,
        headers,
        ..
    } = step;
    let mut launch = server.launch;
    let mut env = Vec::new();
    let mut env_names = Vec::new();
    for (name, answer) in answers {
        match answer {
            EnvAnswer::Forward => {
                if !launch.env_from.contains(&name) {
                    launch.env_from.push(name);
                }
            }
            EnvAnswer::Value(value) => {
                env_names.push(name.clone());
                env.push((name, value));
            }
        }
    }
    launch.env_names = env_names;
    launch.header_names = headers.iter().map(|(name, _)| name.clone()).collect();
    Confirm {
        kind: ConfirmKind::Add {
            name: server.name,
            launch,
            env,
            headers,
            replace,
        },
        pick: Pick::default(),
    }
}

fn server_row(
    server: &McpServerSnapshot,
    selected: bool,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let state = match server.status {
        McpServerStatus::Connected | McpServerStatus::Disconnected => format!(
            "{} · {} tools",
            server.status.as_str(),
            server.tool_names.len()
        ),
        status => status.as_str().to_owned(),
    };
    let state = match server.sign_in {
        Some(McpSignIn::Required) => format!("{state} · sign in required"),
        Some(McpSignIn::SignedIn) => format!("{state} · signed in"),
        _ => state,
    };
    let text = terminal_row(
        &format!("{}  {state}", server.name),
        width.saturating_sub(2),
    );
    let line = Line::from(vec![
        Span::raw(if selected { "› " } else { "  " }),
        Span::raw(text),
    ]);
    if selected {
        line.style(palette.selection())
    } else {
        line
    }
}

/// What a server runs, how its environment is set, its error, and its tools.
fn server_detail(server: &McpServerSnapshot) -> Vec<String> {
    let mut rows = server.launch.as_ref().map(launch_rows).unwrap_or_default();
    if let Some(error) = &server.error {
        rows.push(error.clone());
    }
    match server.sign_in {
        Some(McpSignIn::Required) => rows.push("Press l to sign in.".into()),
        Some(McpSignIn::SignedIn) => rows.push("Signed in; press o to sign out.".into()),
        Some(McpSignIn::Available) => {
            rows.push("Press l to sign in, if the server asks for it.".into());
        }
        None => {}
    }
    if !server.tool_names.is_empty() {
        rows.push("tools:".into());
        rows.extend(server.tool_names.iter().map(|name| format!("  {name}")));
    }
    rows
}

fn launch_rows(launch: &McpServerLaunch) -> Vec<String> {
    let mut rows = Vec::new();
    match (&launch.command, &launch.url) {
        (Some(command), _) => rows.push(format!("runs: {}", shell_display(command, &launch.args))),
        (None, Some(url)) => rows.push(format!("connects to: {url}")),
        (None, None) => {}
    }
    if !launch.env_names.is_empty() {
        rows.push(format!(
            "sets: {} (values hidden)",
            launch.env_names.join(", ")
        ));
    }
    if !launch.env_from.is_empty() {
        rows.push(format!("forwards: {}", launch.env_from.join(", ")));
    }
    if !launch.header_names.is_empty() {
        rows.push(format!(
            "sends headers: {} (values hidden)",
            launch.header_names.join(", ")
        ));
    }
    let host = launch.url.as_deref().map(url_host).unwrap_or_default();
    for header in &launch.headers_from {
        rows.push(header_forward_row(&header.header, &header.variable, host));
    }
    rows
}

/// One forwarded header: which variable's value leaves the machine, and for
/// which host. Both are named on one row so neither can be missed.
pub(crate) fn header_forward_row(header: &str, variable: &str, host: &str) -> String {
    format!("sends ${variable} to {host} in header {header}")
}

/// The host (and port) part of an absolute URL.
pub(crate) fn url_host(url: &str) -> &str {
    let rest = url.split_once("://").map_or(url, |(_, rest)| rest);
    rest.split(['/', '?', '#']).next().unwrap_or(rest)
}

/// Split a `headers_from` value, `${VAR}` or `Scheme ${VAR}`, into its scheme
/// and variable, or None when it has any other shape.
pub(crate) fn parse_header_template(template: &str) -> Option<(Option<&str>, &str)> {
    let (scheme, reference) = match template.split_once(' ') {
        Some((scheme, reference)) => (Some(scheme), reference),
        None => (None, template),
    };
    if let Some(scheme) = scheme {
        if scheme.is_empty()
            || scheme.len() > 20
            || !scheme.chars().all(|c| c.is_ascii_alphabetic())
        {
            return None;
        }
    }
    let variable = reference.strip_prefix("${")?.strip_suffix('}')?;
    let mut characters = variable.chars();
    let valid = characters
        .next()
        .is_some_and(|first| first.is_ascii_alphabetic() || first == '_')
        && characters.all(|c| c.is_ascii_alphanumeric() || c == '_');
    valid.then_some((scheme, variable))
}

/// Quote a command line the way a POSIX shell would read it back.
pub(crate) fn shell_display(command: &str, args: &[String]) -> String {
    std::iter::once(command)
        .chain(args.iter().map(String::as_str))
        .map(|word| {
            let plain = !word.is_empty()
                && word.chars().all(|character| {
                    character.is_ascii_alphanumeric() || "@%_+=:,./-".contains(character)
                });
            if plain {
                word.to_owned()
            } else {
                format!("'{}'", word.replace('\'', r"'\''"))
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Columns inside a docked panel's borders and padding.
fn panel_width(width: u16) -> usize {
    usize::from(width.saturating_sub(4)).max(1)
}

/// The confirmation's detail rows at `width`, every one wrapped in full: a
/// confirmation must never hide part of what it confirms.
fn confirm_details(kind: &ConfirmKind, width: usize) -> Vec<String> {
    let mut rows: Vec<String> = kind
        .launch()
        .map(launch_rows)
        .unwrap_or_default()
        .iter()
        .flat_map(|row| wrapped(row, width))
        .collect();
    if matches!(kind, ConfirmKind::Add { replace: true, .. }) {
        rows.extend(wrapped("Replaces the server saved under this name.", width));
    }
    rows
}

/// Wrap sanitized text over as many rows as it needs.
fn wrapped(content: &str, width: usize) -> Vec<String> {
    let safe = sanitize_for_terminal(content).replace('\n', " ");
    let mut rows = vec![String::new()];
    let mut columns = 0;
    for grapheme in safe.graphemes(true) {
        let next = grapheme.width();
        if columns + next > width && columns > 0 {
            rows.push(String::new());
            columns = 0;
        }
        rows.last_mut().expect("one row").push_str(grapheme);
        columns += next;
    }
    rows
}

/// Whether a docked panel's title is shown without being cut.
fn title_fits(title: &str, area: Rect) -> bool {
    sanitize_for_terminal(title).width() <= usize::from(area.width.saturating_sub(6))
}

fn docked_block(
    title: &str,
    area: Rect,
    hint: &str,
    warning: bool,
    palette: Palette,
) -> Block<'static> {
    let muted = Style::default().fg(palette.muted);
    let mut block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .padding(Padding::horizontal(1))
        .style(palette.composer())
        .border_style(if warning {
            Style::default().fg(palette.warning)
        } else {
            palette.border()
        })
        .title(
            Line::from(format!(
                " {} ",
                terminal_row(title, usize::from(area.width.saturating_sub(6)))
            ))
            .style(Style::default().add_modifier(Modifier::BOLD)),
        );
    if hint.width() + 2 <= usize::from(area.width) {
        block = block.title_bottom(Line::from(hint.to_owned()).style(muted).right_aligned());
    }
    block
}

fn render_paste(
    frame: &mut Frame<'_>,
    area: Rect,
    reading: bool,
    notice: Option<&str>,
    palette: Palette,
) {
    let block = docked_block("Add MCP server", area, " Esc cancel ", false, palette);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let width = usize::from(inner.width);
    let muted = Style::default().fg(palette.muted);
    let mut lines = vec![
        Line::raw(terminal_row(
            if reading {
                "Reading the snippet…"
            } else {
                "Paste the server's config snippet."
            },
            width,
        )),
        Line::styled(
            terminal_row(
                r#"For example {"mcpServers": {"name": {"command": …}}}"#,
                width,
            ),
            muted,
        ),
    ];
    if let Some(notice) = notice {
        lines.push(Line::default());
        lines.push(Line::styled(
            terminal_row(notice, width),
            Style::default().fg(palette.warning),
        ));
    }
    frame.render_widget(Paragraph::new(lines), inner);
}

fn render_env(frame: &mut Frame<'_>, area: Rect, step: &mut EnvStep, palette: Palette) {
    let (position, total) = step.position();
    let (name, header) = match step.current() {
        Ask::Env(name) => (name.to_owned(), false),
        Ask::Header(name) => (name.to_owned(), true),
    };
    let asked = if header {
        format!("header {name}")
    } else {
        name.clone()
    };
    let title = format!("{} needs {asked} ({position} of {total})", step.server.name);
    let hint = if header {
        " Enter save · Esc cancel "
    } else if step.input.is_some() {
        " Enter save · Esc back "
    } else {
        " ↑↓ select · Enter choose · Esc cancel "
    };
    let block = docked_block(&title, area, hint, false, palette);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let width = usize::from(inner.width);
    let muted = Style::default().fg(palette.muted);
    // The input or the choices come first, so they stay usable in a small
    // terminal; explanations follow.
    if let Some(value) = &step.input {
        step.pick.mark_painted(false);
        const PROMPT: &str = "› ";
        let count = value.chars().count();
        let room = width.saturating_sub(PROMPT.width() + 1);
        let masked = if count > room {
            format!("…{}", "•".repeat(room.saturating_sub(1)))
        } else {
            "•".repeat(count)
        };
        let cursor_x = inner.x + u16::try_from(PROMPT.width() + masked.width()).unwrap_or(0);
        let lines = vec![
            Line::from(vec![
                Span::styled(PROMPT, Style::default().fg(palette.primary)),
                Span::raw(masked),
            ]),
            Line::default(),
            Line::styled(terminal_row(&format!("Value for {asked}."), width), muted),
            Line::styled(
                terminal_row(
                    "It stays hidden and is saved to your Wisp settings file.",
                    width,
                ),
                muted,
            ),
        ];
        frame.render_widget(Paragraph::new(lines), inner);
        if inner.height >= 1 && inner.width > 0 {
            frame.set_cursor_position((cursor_x.min(inner.right().saturating_sub(1)), inner.y));
        }
        return;
    }
    let choices = [
        format!("Forward ${name} from Wisp's environment"),
        "Enter a value (hidden)".to_owned(),
    ];
    let mut lines: Vec<Line<'static>> = choices
        .iter()
        .enumerate()
        .map(|(index, label)| {
            choice_line(
                &(index + 1).to_string(),
                label,
                step.pick.selected == Some(index),
                width,
                palette,
            )
        })
        .collect();
    lines.push(Line::default());
    lines.push(Line::styled(
        terminal_row(&format!("How should Wisp provide {name}?"), width),
        muted,
    ));
    frame.render_widget(Paragraph::new(lines), inner);
    // Both choices must be on screen. This step only chooses how a value is
    // provided; the final confirmation shows the whole result.
    step.pick
        .mark_painted(inner.height >= 2 && inner.width >= 12);
}

fn render_confirm(frame: &mut Frame<'_>, area: Rect, confirm: &mut Confirm, palette: Palette) {
    let hint = if panel_width(area.width) >= 40 {
        " ↑↓ select · Enter confirm · Esc cancel "
    } else {
        " ↑↓ Enter · Esc "
    };
    let title = confirm.kind.title();
    let block = docked_block(&title, area, hint, true, palette);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let width = usize::from(inner.width);
    let height = usize::from(inner.height);
    let details = confirm_details(&confirm.kind, width);
    let choices = confirm.kind.choices();
    // The choices always stay visible. The details need their own rows plus a
    // gap; when they cannot all be shown, the panel says so instead of showing
    // part of them, and the choices stay unarmed.
    let detail_rows = details.len() + usize::from(!details.is_empty());
    let complete = detail_rows + choices.len() <= height && title_fits(&title, area);
    let mut lines: Vec<Line<'static>> = Vec::new();
    if complete {
        lines.extend(details.into_iter().map(Line::raw));
        if detail_rows > 0 {
            lines.push(Line::default());
        }
    } else {
        let room = height.saturating_sub(choices.len());
        lines.extend(
            wrapped(ENLARGE_HINT, width)
                .into_iter()
                .take(room)
                .map(|row| Line::styled(row, Style::default().fg(palette.warning))),
        );
    }
    lines.extend(choices.iter().enumerate().map(|(index, label)| {
        choice_line(
            &(index + 1).to_string(),
            label,
            confirm.pick.selected == Some(index),
            width,
            palette,
        )
    }));
    if let Some(footnote) = confirm
        .kind
        .footnote()
        .filter(|_| complete && lines.len() + 2 <= height)
    {
        lines.push(Line::default());
        lines.push(Line::styled(
            terminal_row(footnote, width),
            Style::default().fg(palette.muted),
        ));
    }
    frame.render_widget(Paragraph::new(lines), inner);
    confirm.pick.mark_painted(complete && inner.width >= 12);
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend, buffer::Buffer};
    use std::sync::Arc;
    use wisp_protocol::events::McpStatusSnapshot;

    const SECRET: &str = "s3cret-sentinel";

    fn launch(command: &str, args: &[&str]) -> McpServerLaunch {
        McpServerLaunch {
            command: Some(command.into()),
            args: args.iter().map(|arg| (*arg).to_owned()).collect(),
            url: None,
            ..Default::default()
        }
    }

    fn server(name: &str, status: McpServerStatus, launch: McpServerLaunch) -> McpServerSnapshot {
        let tools = if status == McpServerStatus::Connected {
            vec![format!("mcp__{name}__search")]
        } else {
            vec![]
        };
        McpServerSnapshot {
            name: name.into(),
            status,
            tool_names: tools,
            error: (status == McpServerStatus::Unavailable).then(|| "startup failed".into()),
            launch: Some(launch),
            sign_in: None,
        }
    }

    fn state(servers: Vec<McpServerSnapshot>) -> UiState {
        let mut state = UiState::unconfigured();
        state.input_ready = true;
        state.mcp.snapshot = Some(Arc::new(McpStatusSnapshot { servers }));
        state
    }

    fn sample_state() -> UiState {
        let mut github = launch("npx", &["-y", "@modelcontextprotocol/server-github"]);
        github.env_names = vec!["GITHUB_TOKEN".into()];
        github.env_from = vec!["HOME_DIR".into()];
        state(vec![
            server("github", McpServerStatus::Connected, github),
            server(
                "archive",
                McpServerStatus::Disabled,
                launch("uvx", &["archive"]),
            ),
        ])
    }

    fn palettes() -> [(&'static str, Palette); 2] {
        [
            ("colour", Palette::default()),
            (
                "mono",
                crate::theme::resolve("glass").unwrap().palette(true),
            ),
        ]
    }

    fn draw_buffer(
        view: &mut McpView,
        state: &UiState,
        width: u16,
        height: u16,
        palette: Palette,
    ) -> Buffer {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| view.render(frame, frame.area(), state, palette))
            .unwrap();
        terminal.backend().buffer().clone()
    }

    fn text(buffer: &Buffer) -> String {
        let area = buffer.area;
        (0..area.height)
            .map(|y| {
                (0..area.width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn draw(view: &mut McpView, state: &UiState, width: u16, height: u16) -> String {
        text(&draw_buffer(view, state, width, height, Palette::default()))
    }

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn press(view: &mut McpView, state: &UiState, code: KeyCode) -> DiscoveryAction {
        view.handle_key(key(code), state)
    }

    fn change(action: DiscoveryAction) -> Option<McpChange> {
        match action {
            DiscoveryAction::Refresh(UiAction::ChangeMcpServers(change)) => Some(change),
            _ => None,
        }
    }

    #[test]
    fn browse_shows_status_launch_and_tools_at_each_size_in_colour_and_mono() {
        let state = sample_state();
        for (name, palette) in palettes() {
            let mut view = McpView::default();
            let wide = text(&draw_buffer(&mut view, &state, 100, 30, palette));
            assert!(
                wide.contains("› github  connected · 1 tools"),
                "{name}: {wide}"
            );
            assert!(wide.contains("  archive  disabled"), "{name}");
            assert!(
                wide.contains("runs: npx -y @modelcontextprotocol/server-github"),
                "{name}"
            );
            assert!(
                wide.contains("sets: GITHUB_TOKEN (values hidden)"),
                "{name}"
            );
            assert!(wide.contains("forwards: HOME_DIR"), "{name}");
            assert!(wide.contains("mcp__github__search"), "{name}");
            assert!(wide.contains("l/o sign in/out · c reconnect"), "{name}");

            let small = text(&draw_buffer(&mut view, &state, 30, 8, palette));
            assert!(small.contains("› github"), "{name}: {small}");
            assert!(small.contains("a e d l o c r"), "{name}: {small}");
        }
    }

    #[test]
    fn monochrome_marks_the_selected_server_without_colour() {
        let state = sample_state();
        let mono = crate::theme::resolve("glass").unwrap().palette(true);
        let buffer = draw_buffer(&mut McpView::default(), &state, 100, 30, mono);
        let row = (0..30)
            .find(|y| {
                text(&buffer)
                    .lines()
                    .nth(usize::from(*y))
                    .is_some_and(|line| line.contains("› github"))
            })
            .unwrap();
        assert!(buffer[(2, row)].modifier.contains(Modifier::REVERSED));
    }

    #[test]
    fn empty_and_failed_states_are_explicit() {
        let mut state = state(vec![]);
        let rendered = draw(&mut McpView::default(), &state, 100, 30);
        assert!(rendered.contains("No MCP servers configured. Press a to add one."));
        state.mcp.error = Some("restart failed".into());
        let rendered = draw(&mut McpView::default(), &state, 100, 30);
        assert!(rendered.contains("Error: restart failed"));
    }

    #[test]
    fn pasted_control_and_bidi_characters_render_inertly() {
        let state = state(vec![server(
            "evil",
            McpServerStatus::Disabled,
            launch("npx", &["\u{1b}[31mred", "a\u{202e}b", "two\nlines"]),
        )]);
        let mut view = McpView::default();
        let browse = draw(&mut view, &state, 100, 30);
        assert!(
            !browse.contains('\u{1b}') && !browse.contains('\u{202e}'),
            "{browse}"
        );
        assert!(browse.contains("[31mred"));
        press(&mut view, &state, KeyCode::Char('e'));
        let confirm = draw(&mut view, &state, 100, 12);
        assert!(confirm.contains("Enable MCP server evil"), "{confirm}");
        assert!(
            !confirm.contains('\u{1b}') && !confirm.contains('\u{202e}'),
            "{confirm}"
        );
    }

    #[test]
    fn a_key_pressed_before_the_confirmation_was_painted_cannot_confirm() {
        let state = sample_state();
        let mut view = McpView::default();
        // `d` acts only on a server painted as selected.
        assert!(change(press(&mut view, &state, KeyCode::Char('d'))).is_none());
        assert!(!view.docked());
        draw(&mut view, &state, 100, 30);
        press(&mut view, &state, KeyCode::Char('d'));
        assert!(view.docked());

        // Captured before the panel's first frame: neither a number nor Enter acts.
        assert!(change(press(&mut view, &state, KeyCode::Char('1'))).is_none());
        press(&mut view, &state, KeyCode::Down);
        assert!(change(press(&mut view, &state, KeyCode::Enter)).is_none());
        assert!(view.docked());

        draw(&mut view, &state, 100, 12);
        // A highlight moved after the frame must be painted again before Enter.
        press(&mut view, &state, KeyCode::Down);
        assert!(change(press(&mut view, &state, KeyCode::Enter)).is_none());
        view.invalidate();
        draw(&mut view, &state, 100, 12);
        // The highlight is now on "Keep".
        assert!(change(press(&mut view, &state, KeyCode::Enter)).is_none());
        assert!(!view.docked());

        draw(&mut view, &state, 100, 30);
        press(&mut view, &state, KeyCode::Char('d'));
        view.invalidate();
        draw(&mut view, &state, 100, 12);
        assert_eq!(
            change(press(&mut view, &state, KeyCode::Char('1'))),
            Some(McpChange::Remove("github".into()))
        );
    }

    #[test]
    fn enabling_sends_the_launch_settings_that_were_shown() {
        let state = sample_state();
        let mut view = McpView::default();
        draw(&mut view, &state, 100, 30);
        press(&mut view, &state, KeyCode::Down);
        draw(&mut view, &state, 100, 30);
        press(&mut view, &state, KeyCode::Char('e'));
        let confirm = draw(&mut view, &state, 100, 12);
        assert!(confirm.contains("runs: uvx archive"), "{confirm}");
        assert!(confirm.contains("Wisp runs this command every time it starts."));
        assert_eq!(
            change(press(&mut view, &state, KeyCode::Char('1'))),
            Some(McpChange::SetEnabled {
                name: "archive".into(),
                enabled: true,
                expected: Some(launch("uvx", &["archive"])),
            })
        );

        // Disabling needs no confirmation.
        let mut view = McpView::default();
        draw(&mut view, &state, 100, 30);
        assert_eq!(
            change(press(&mut view, &state, KeyCode::Char('e'))),
            Some(McpChange::SetEnabled {
                name: "github".into(),
                enabled: false,
                expected: None,
            })
        );
        assert_eq!(
            change(press(&mut view, &state, KeyCode::Char('c'))),
            Some(McpChange::Reconnect)
        );
    }

    #[test]
    fn pasting_a_snippet_asks_for_each_value_and_never_shows_one() {
        let mut state = sample_state();
        let mut view = McpView::default();
        press(&mut view, &state, KeyCode::Char('a'));
        let paste = draw(&mut view, &state, 100, 8);
        assert!(
            paste.contains("Paste the server's config snippet."),
            "{paste}"
        );

        let snippet = format!(
            r#"{{"mcpServers": {{"docs": {{"command": "npx", "env": {{"TOKEN": "{SECRET}"}}}}}}}}"#
        );
        let action = view.handle_paste(&snippet, &state);
        assert!(!format!("{action:?}").contains(SECRET));
        assert!(matches!(
            action,
            DiscoveryAction::Refresh(UiAction::PreviewMcpSnippet(_))
        ));
        assert!(draw(&mut view, &state, 100, 8).contains("Reading the snippet…"));

        let mut preview_launch = launch("npx", &["docs-server"]);
        preview_launch.env_names = vec!["TOKEN".into(), "ORG".into()];
        state.mcp_preview.snapshot = Some(Arc::new(vec![McpServerPreview {
            name: "docs".into(),
            launch: preview_launch,
        }]));
        view.advance(&state);
        let first = draw(&mut view, &state, 100, 8);
        assert!(first.contains("docs needs TOKEN (1 of 2)"), "{first}");
        assert!(first.contains("Forward $TOKEN from Wisp's environment"));
        press(&mut view, &state, KeyCode::Char('1'));

        view.invalidate();
        let second = draw(&mut view, &state, 100, 8);
        assert!(second.contains("docs needs ORG (2 of 2)"), "{second}");
        press(&mut view, &state, KeyCode::Char('2'));
        // Digits are part of the value, never a choice.
        assert!(!view.key_activates(key(KeyCode::Char('1'))));
        assert!(view.key_activates(key(KeyCode::Enter)));
        for character in SECRET.chars() {
            press(&mut view, &state, KeyCode::Char(character));
        }
        let typing = draw(&mut view, &state, 100, 8);
        assert!(!typing.contains(SECRET), "{typing}");
        assert!(typing.contains(&"•".repeat(SECRET.chars().count())));
        press(&mut view, &state, KeyCode::Enter);

        for (width, height) in [(100, 30), (30, 8)] {
            for (name, palette) in palettes() {
                let rendered = text(&draw_buffer(&mut view, &state, width, height, palette));
                assert!(!rendered.contains(SECRET), "{name} {width}x{height}");
            }
        }
        view.invalidate();
        let confirm = draw(&mut view, &state, 100, 12);
        assert!(confirm.contains("Add MCP server docs"), "{confirm}");
        assert!(confirm.contains("runs: npx docs-server"));
        assert!(confirm.contains("sets: ORG (values hidden)"));
        assert!(confirm.contains("forwards: TOKEN"));
        let action = press(&mut view, &state, KeyCode::Char('1'));
        assert!(!format!("{action:?}").contains(SECRET));
        let Some(McpChange::Add {
            name,
            launch,
            env,
            headers,
            replace,
        }) = change(action)
        else {
            panic!("expected an add");
        };
        assert_eq!(name, "docs");
        assert!(!replace);
        assert_eq!(launch.env_from, ["TOKEN"]);
        assert_eq!(env.len(), 1);
        assert_eq!(env[0].0, "ORG");
        assert_eq!(env[0].1.as_str(), SECRET);
        assert!(headers.is_empty());
    }

    #[test]
    fn a_snippet_with_several_servers_or_an_error_returns_to_paste() {
        let mut state = sample_state();
        let mut view = McpView::default();
        press(&mut view, &state, KeyCode::Char('a'));
        view.handle_paste("{}", &state);
        state.mcp_preview.snapshot = Some(Arc::new(vec![
            McpServerPreview {
                name: "one".into(),
                launch: launch("a", &[]),
            },
            McpServerPreview {
                name: "two".into(),
                launch: launch("b", &[]),
            },
        ]));
        view.advance(&state);
        let rendered = draw(&mut view, &state, 100, 8);
        assert!(
            rendered.contains("This snippet has 2 servers."),
            "{rendered}"
        );

        view.handle_paste("{}", &state);
        state.mcp_preview.snapshot = None;
        state.mcp_preview.error = Some("server 'x' uses unsupported settings: headers".into());
        view.advance(&state);
        assert!(draw(&mut view, &state, 100, 8).contains("unsupported settings: headers"));
        press(&mut view, &state, KeyCode::Esc);
        assert!(!view.docked());
    }

    #[test]
    fn adding_a_known_name_is_shown_as_a_replacement() {
        let mut state = sample_state();
        let mut view = McpView::default();
        press(&mut view, &state, KeyCode::Char('a'));
        view.handle_paste("{}", &state);
        state.mcp_preview.snapshot = Some(Arc::new(vec![McpServerPreview {
            name: "github".into(),
            launch: launch("npx", &["other"]),
        }]));
        view.advance(&state);
        let confirm = draw(&mut view, &state, 100, 12);
        assert!(confirm.contains("Replace MCP server github"), "{confirm}");
        assert!(confirm.contains("Replaces the server saved under this name."));
    }

    #[test]
    fn a_paste_waits_for_the_previous_preview_and_changes_wait_for_a_prompt() {
        use crate::reducer::{CommandIdSource, CommandKind, reduce};

        #[derive(Default)]
        struct Ids(usize);
        impl CommandIdSource for Ids {
            fn next_id(&mut self, kind: CommandKind) -> String {
                self.0 += 1;
                format!("{}-{}", kind.prefix(), self.0)
            }
        }
        let mut ids = Ids::default();
        let mut state = sample_state();
        let earlier = SecretText::new("{}".into(), 64).unwrap();
        reduce(&mut state, UiAction::PreviewMcpSnippet(earlier), &mut ids).unwrap();
        let mut view = McpView::default();
        press(&mut view, &state, KeyCode::Char('a'));
        assert!(matches!(
            view.handle_paste(r#"{"mcpServers": {}}"#, &state),
            DiscoveryAction::None
        ));
        assert!(draw(&mut view, &state, 100, 8).contains("Still reading the previous snippet"));

        let mut busy = sample_state();
        reduce(&mut busy, UiAction::Submit("work".into()), &mut ids).unwrap();
        let mut view = McpView::default();
        draw(&mut view, &busy, 100, 30);
        for code in [KeyCode::Char('a'), KeyCode::Char('d')] {
            assert!(change(press(&mut view, &busy, code)).is_none());
            assert!(!view.docked());
        }
        assert!(
            draw(&mut view, &busy, 100, 30)
                .contains("Finish the current operation before changing MCP servers.")
        );
    }

    #[test]
    fn a_confirmation_is_armed_only_when_the_whole_command_is_visible() {
        let padding = "x".repeat(400);
        let state = state(vec![server(
            "files",
            McpServerStatus::Disabled,
            launch("npx", &["-y", "server-filesystem", &padding, "/"]),
        )]);
        let mut view = McpView::default();
        draw(&mut view, &state, 100, 30);
        press(&mut view, &state, KeyCode::Char('e'));

        // Too short for the wrapped command: the panel says so and cannot confirm.
        let short = draw(&mut view, &state, 100, 8);
        assert!(short.contains("Make the terminal larger"), "{short}");
        assert!(!short.contains("server-filesystem"));
        assert!(change(press(&mut view, &state, KeyCode::Char('1'))).is_none());

        // Tall enough: every row of the command, including its last argument, is shown.
        let height = view.panel_height(100);
        view.invalidate();
        let whole = draw(&mut view, &state, 100, height);
        let compact: String = whole
            .chars()
            .filter(|character| !character.is_whitespace() && *character != '│')
            .collect();
        assert!(compact.contains(&format!("{padding}/")), "{whole}");
        assert!(change(press(&mut view, &state, KeyCode::Char('1'))).is_some());
    }

    #[test]
    fn choices_stay_usable_in_the_smallest_docked_panel() {
        // At 30×8 the composer leaves a docked panel five rows.
        let state = sample_state();
        let mut view = McpView::default();
        draw(&mut view, &state, 30, 8);
        press(&mut view, &state, KeyCode::Char('d'));
        let remove = draw(&mut view, &state, 30, 5);
        assert!(remove.contains("Remove MCP server github"), "{remove}");
        assert_eq!(
            change(press(&mut view, &state, KeyCode::Char('1'))),
            Some(McpChange::Remove("github".into()))
        );

        let mut state = sample_state();
        let mut view = McpView::default();
        press(&mut view, &state, KeyCode::Char('a'));
        view.handle_paste("{}", &state);
        let mut preview_launch = launch("npx", &["docs"]);
        preview_launch.env_names = vec!["TOKEN".into()];
        state.mcp_preview.snapshot = Some(Arc::new(vec![McpServerPreview {
            name: "docs".into(),
            launch: preview_launch,
        }]));
        view.advance(&state);
        let env = draw(&mut view, &state, 30, 5);
        assert!(env.contains("1  Forward"), "{env}");
        assert!(env.contains("2  Enter a value"), "{env}");
        press(&mut view, &state, KeyCode::Char('1'));
        assert!(view.docked());
        // The add confirmation's details do not fit five rows; it asks for room.
        view.invalidate();
        let confirm = draw(&mut view, &state, 30, 5);
        assert!(confirm.contains("Make the"), "{confirm}");
        assert!(change(press(&mut view, &state, KeyCode::Char('1'))).is_none());
    }

    #[test]
    fn a_snippet_that_was_not_sent_returns_to_the_paste_step() {
        let state = sample_state();
        let mut view = McpView::default();
        press(&mut view, &state, KeyCode::Char('a'));
        view.handle_paste(r#"{"mcpServers": {}}"#, &state);
        view.request_rejected("The MCP request is too large; nothing was sent.");
        let rendered = draw(&mut view, &state, 100, 8);
        assert!(
            rendered.contains("Paste the server's config snippet."),
            "{rendered}"
        );
        assert!(rendered.contains("nothing was sent"));
    }

    #[test]
    fn an_empty_value_can_be_entered() {
        let mut state = sample_state();
        let mut view = McpView::default();
        press(&mut view, &state, KeyCode::Char('a'));
        view.handle_paste("{}", &state);
        let mut preview_launch = launch("npx", &["docs"]);
        preview_launch.env_names = vec!["EMPTY_OK".into()];
        state.mcp_preview.snapshot = Some(Arc::new(vec![McpServerPreview {
            name: "docs".into(),
            launch: preview_launch,
        }]));
        view.advance(&state);
        draw(&mut view, &state, 100, 8);
        press(&mut view, &state, KeyCode::Char('2'));
        draw(&mut view, &state, 100, 8);
        press(&mut view, &state, KeyCode::Enter);
        view.invalidate();
        let confirm = draw(&mut view, &state, 100, 12);
        assert!(
            confirm.contains("sets: EMPTY_OK (values hidden)"),
            "{confirm}"
        );
        let Some(McpChange::Add { env, .. }) = change(press(&mut view, &state, KeyCode::Char('1')))
        else {
            panic!("expected an add");
        };
        assert_eq!(env[0].1.as_str(), "");
    }

    #[test]
    fn launch_rows_name_header_values_and_the_variables_headers_send() {
        let launch = McpServerLaunch {
            url: Some("https://mcp.linear.app/mcp".into()),
            header_names: vec!["X-Workspace".into()],
            headers_from: vec![wisp_protocol::events::McpHeaderFrom {
                header: "Authorization".into(),
                variable: "LINEAR_API_KEY".into(),
                scheme: Some("Bearer".into()),
            }],
            ..Default::default()
        };

        assert_eq!(
            launch_rows(&launch),
            vec![
                "connects to: https://mcp.linear.app/mcp".to_owned(),
                "sends headers: X-Workspace (values hidden)".to_owned(),
                "sends $LINEAR_API_KEY to mcp.linear.app in header Authorization".to_owned(),
            ]
        );
    }

    #[test]
    fn header_templates_parse_only_a_variable_after_an_optional_scheme() {
        assert_eq!(
            parse_header_template("Bearer ${LINEAR_API_KEY}"),
            Some((Some("Bearer"), "LINEAR_API_KEY"))
        );
        assert_eq!(parse_header_template("${TOKEN}"), Some((None, "TOKEN")));
        for invalid in [
            "Bearer abc",
            "Bearer sk-1 ${T}",
            "${1X}",
            "$TOKEN",
            "B3arer ${T}",
            "",
        ] {
            assert_eq!(parse_header_template(invalid), None, "{invalid}");
        }
        assert_eq!(
            url_host("https://mcp.linear.app:8443/mcp"),
            "mcp.linear.app:8443"
        );
    }

    fn http_preview(headers: &[&str]) -> McpServerPreview {
        McpServerPreview {
            name: "linear".into(),
            launch: McpServerLaunch {
                url: Some("https://mcp.linear.app/mcp".into()),
                header_names: headers.iter().map(|name| (*name).to_owned()).collect(),
                headers_from: vec![wisp_protocol::events::McpHeaderFrom {
                    header: "Authorization".into(),
                    variable: "LINEAR_API_KEY".into(),
                    scheme: Some("Bearer".into()),
                }],
                ..Default::default()
            },
        }
    }

    #[test]
    fn a_snippet_header_value_is_typed_masked_and_sent_with_the_server() {
        let mut state = sample_state();
        let mut view = McpView::default();
        press(&mut view, &state, KeyCode::Char('a'));
        view.handle_paste("{}", &state);
        state.mcp_preview.snapshot = Some(Arc::new(vec![http_preview(&["X-Team"])]));
        view.advance(&state);

        let ask = draw(&mut view, &state, 100, 8);
        assert!(ask.contains("linear needs header X-Team (1 of 1)"), "{ask}");
        // An empty value cannot be sent; the step stays open.
        press(&mut view, &state, KeyCode::Enter);
        assert!(draw(&mut view, &state, 100, 8).contains("header X-Team"));
        for character in SECRET.chars() {
            press(&mut view, &state, KeyCode::Char(character));
        }
        let typed = draw(&mut view, &state, 100, 8);
        assert!(!typed.contains(SECRET), "{typed}");
        press(&mut view, &state, KeyCode::Enter);

        view.invalidate();
        let confirm = draw(&mut view, &state, 100, 12);
        assert!(
            confirm.contains("sends headers: X-Team (values hidden)"),
            "{confirm}"
        );
        assert!(
            confirm.contains("sends $LINEAR_API_KEY to mcp.linear.app in header Authorization"),
            "{confirm}"
        );
        let action = press(&mut view, &state, KeyCode::Char('1'));
        assert!(!format!("{action:?}").contains(SECRET));
        let Some(McpChange::Add {
            launch, headers, ..
        }) = change(action)
        else {
            panic!("expected an add");
        };
        assert_eq!(launch.header_names, ["X-Team"]);
        assert_eq!(headers.len(), 1);
        assert_eq!(headers[0].1.as_str(), SECRET);
    }

    #[test]
    fn escape_while_typing_a_header_stops_adding() {
        let mut state = sample_state();
        let mut view = McpView::default();
        press(&mut view, &state, KeyCode::Char('a'));
        view.handle_paste("{}", &state);
        state.mcp_preview.snapshot = Some(Arc::new(vec![http_preview(&["X-Team"])]));
        view.advance(&state);

        press(&mut view, &state, KeyCode::Esc);

        assert!(!view.docked());
    }

    fn signed(sign_in: Option<McpSignIn>) -> UiState {
        let mut linear = server(
            "linear",
            McpServerStatus::Unavailable,
            http_preview(&[]).launch,
        );
        linear.error = Some("MCP server linear is unavailable: sign in".into());
        linear.sign_in = sign_in;
        state(vec![linear])
    }

    #[test]
    fn l_signs_in_to_the_shown_server_and_o_signs_out() {
        let state = signed(Some(McpSignIn::Required));
        let mut view = McpView::default();
        let list = draw(&mut view, &state, 100, 20);
        assert!(
            list.contains("linear  unavailable · sign in required"),
            "{list}"
        );
        assert!(list.contains("Press l to sign in."), "{list}");

        let action = press(&mut view, &state, KeyCode::Char('l'));
        assert_eq!(change(action), Some(McpChange::Login("linear".into())));
        draw(&mut view, &state, 100, 20);
        let action = press(&mut view, &state, KeyCode::Char('o'));
        assert_eq!(change(action), Some(McpChange::Logout("linear".into())));

        let stdio = sample_state();
        let mut view = McpView::default();
        draw(&mut view, &stdio, 100, 20);
        assert_eq!(change(press(&mut view, &stdio, KeyCode::Char('l'))), None);
        assert!(draw(&mut view, &stdio, 100, 20).contains("Only servers added by URL sign in"));
    }

    fn signing_in(url: &str, cancel_requested: bool) -> UiState {
        let mut state = signed(Some(McpSignIn::Required));
        state.mcp_sign_in = Some(crate::reducer::McpSignInState {
            command_id: "login-1".into(),
            name: "linear".into(),
            url: Some(url.to_owned()),
            cancel_requested,
        });
        state
    }

    #[test]
    fn a_long_sign_in_address_scrolls_whole_without_hiding_the_list() {
        let url = format!(
            "https://auth.example.test/authorize?state={}END",
            "s".repeat(1_000)
        );
        let state = signing_in(&url, false);
        let mut view = McpView::default();

        let first = draw(&mut view, &state, 66, 21);
        assert!(first.contains("Signing in to linear"), "{first}");
        assert!(first.contains("linear  unavailable"), "{first}");
        assert!(
            first.contains("https://auth.example.test/authorize"),
            "{first}"
        );
        // The rest of the address is reachable by scrolling, never cut off.
        let mut seen = String::new();
        for _ in 0..40 {
            press(&mut view, &state, KeyCode::PageDown);
            seen = draw(&mut view, &state, 66, 21);
        }
        assert!(seen.contains("END"), "{seen}");
    }

    #[test]
    fn escape_cancels_a_sign_in_once_then_closes() {
        let mut view = McpView::default();
        let waiting = signing_in("https://auth.example.test/authorize", false);
        assert!(matches!(
            press(&mut view, &waiting, KeyCode::Esc),
            DiscoveryAction::Refresh(UiAction::CancelMcpSignIn)
        ));

        let cancelling = signing_in("https://auth.example.test/authorize", true);
        assert!(draw(&mut view, &cancelling, 100, 20).contains("Esc close"));
        assert!(matches!(
            press(&mut view, &cancelling, KeyCode::Esc),
            DiscoveryAction::Close
        ));
    }

    #[test]
    fn a_header_value_is_saved_without_surrounding_spaces() {
        let mut state = sample_state();
        let mut view = McpView::default();
        press(&mut view, &state, KeyCode::Char('a'));
        view.handle_paste("{}", &state);
        state.mcp_preview.snapshot = Some(Arc::new(vec![http_preview(&["X-Team"])]));
        view.advance(&state);
        view.handle_paste(&format!("  {SECRET} \n"), &state);
        press(&mut view, &state, KeyCode::Enter);
        view.invalidate();
        draw(&mut view, &state, 100, 12);

        let Some(McpChange::Add { headers, .. }) =
            change(press(&mut view, &state, KeyCode::Char('1')))
        else {
            panic!("expected an add");
        };
        assert_eq!(headers[0].1.as_str(), SECRET);
    }

    #[test]
    fn the_key_hint_is_never_cut_off() {
        let state = sample_state();
        for width in [40_u16, 64, 70, 80, 100, 110, 120] {
            let mut view = McpView::default();
            let shown = draw(&mut view, &state, width, 20);
            assert!(shown.contains("Esc"), "{width}: {shown}");
        }
    }
}
