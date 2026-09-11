//! Fixed, keyboard-only provider connection panel.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::{ConnectionCatalogSnapshot, ConnectionMethodSnapshot};

use crate::reducer::{API_KEY_MAX_BYTES, ApiKey};
use crate::session_picker::terminal_row;
use crate::theme::Palette;

const PAGE_STEP: usize = 10;

#[derive(Debug, Eq, PartialEq)]
pub enum ConnectionPanelAction {
    None,
    Close,
    Refresh,
    EnterApiKey { provider: String },
    SubmitApiKey,
    Disconnect { provider: String },
    BeginDeviceCode { provider: String },
    CancelDeviceCode,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConnectionPanel {
    catalog: ConnectionCatalogSnapshot,
    mode: ConnectionPanelMode,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum ConnectionPanelMode {
    Picker {
        selected: Option<usize>,
    },
    ApiKey {
        provider: String,
        value: ApiKeyInput,
    },
    DeviceCode {
        provider: String,
        verification_uri: Option<String>,
        user_code: Option<String>,
        attempt: Option<u32>,
        scroll: usize,
    },
}

#[derive(Clone, Default, Eq, PartialEq)]
struct ApiKeyInput(String);

impl std::fmt::Debug for ApiKeyInput {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("ApiKeyInput(<redacted>)")
    }
}

impl ConnectionPanel {
    pub fn new(catalog: ConnectionCatalogSnapshot) -> Self {
        let selected = (!methods(&catalog).is_empty()).then_some(0);
        Self {
            catalog,
            mode: ConnectionPanelMode::Picker { selected },
        }
    }

    pub fn update_catalog(&mut self, catalog: ConnectionCatalogSnapshot) {
        self.catalog = catalog;
        if let ConnectionPanelMode::Picker { selected } = &mut self.mode {
            *selected = selected
                .and_then(|index| (index < methods(&self.catalog).len()).then_some(index))
                .or_else(|| (!methods(&self.catalog).is_empty()).then_some(0));
        }
    }

    pub fn return_to_picker(&mut self) {
        self.mode = ConnectionPanelMode::Picker {
            selected: (!methods(&self.catalog).is_empty()).then_some(0),
        };
    }

    pub fn begin_device_code(&mut self, provider: String) {
        self.mode = ConnectionPanelMode::DeviceCode {
            provider,
            verification_uri: None,
            user_code: None,
            attempt: None,
            scroll: 0,
        };
    }

    pub fn finish_device_code(&mut self) {
        if matches!(self.mode, ConnectionPanelMode::DeviceCode { .. }) {
            self.return_to_picker();
        }
    }

    pub fn show_device_code(
        &mut self,
        provider: &str,
        verification_uri: String,
        user_code: String,
    ) -> bool {
        if let ConnectionPanelMode::DeviceCode {
            provider: active_provider,
            verification_uri: active_uri,
            user_code: active_code,
            ..
        } = &mut self.mode
        {
            if active_provider == provider {
                *active_uri = Some(verification_uri);
                *active_code = Some(user_code);
                return true;
            }
        }
        false
    }

    pub fn show_device_progress(&mut self, provider: &str, attempt: u32) {
        if let ConnectionPanelMode::DeviceCode {
            provider: active_provider,
            attempt: active_attempt,
            ..
        } = &mut self.mode
        {
            if active_provider == provider {
                *active_attempt = Some(attempt);
            }
        }
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> ConnectionPanelAction {
        match &mut self.mode {
            ConnectionPanelMode::Picker { selected } => {
                let action = picker_key(&self.catalog, selected, key);
                if let ConnectionPanelAction::EnterApiKey { provider } = &action {
                    self.mode = ConnectionPanelMode::ApiKey {
                        provider: provider.clone(),
                        value: ApiKeyInput::default(),
                    };
                }
                action
            }
            ConnectionPanelMode::ApiKey { value, .. } => {
                if key.code == KeyCode::Esc || is_ctrl_c(key) {
                    return ConnectionPanelAction::Close;
                }
                if key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                {
                    return ConnectionPanelAction::None;
                }
                match key.code {
                    KeyCode::Enter => {
                        if value.can_submit() {
                            ConnectionPanelAction::SubmitApiKey
                        } else {
                            ConnectionPanelAction::None
                        }
                    }
                    KeyCode::Backspace => {
                        value.pop();
                        ConnectionPanelAction::None
                    }
                    KeyCode::Char(character) => {
                        value.push_str(&character.to_string());
                        ConnectionPanelAction::None
                    }
                    _ => ConnectionPanelAction::None,
                }
            }
            ConnectionPanelMode::DeviceCode { scroll, .. } => {
                if key.code == KeyCode::Esc || is_ctrl_c(key) {
                    return ConnectionPanelAction::CancelDeviceCode;
                }
                match key.code {
                    KeyCode::Up => *scroll = scroll.saturating_sub(1),
                    KeyCode::Down => *scroll = scroll.saturating_add(1),
                    KeyCode::PageUp => *scroll = scroll.saturating_sub(PAGE_STEP),
                    KeyCode::PageDown => *scroll = scroll.saturating_add(PAGE_STEP),
                    KeyCode::Home => *scroll = 0,
                    KeyCode::End => *scroll = usize::MAX,
                    _ => {}
                }
                ConnectionPanelAction::None
            }
        }
    }

    /// Distinguish picker actions from literal characters in the masked editor.
    pub fn key_activates(&self, key: KeyEvent) -> bool {
        match self.mode {
            ConnectionPanelMode::Picker { .. } => {
                key.modifiers == KeyModifiers::NONE
                    && matches!(key.code, KeyCode::Enter | KeyCode::Char('d'))
            }
            ConnectionPanelMode::ApiKey { .. } => key.code == KeyCode::Enter,
            ConnectionPanelMode::DeviceCode { .. } => false,
        }
    }

    pub fn handle_paste(&mut self, pasted: &str) {
        if let ConnectionPanelMode::ApiKey { value, .. } = &mut self.mode {
            value.push_str(pasted);
        }
    }

    pub fn pending_api_key(&self) -> Option<(&str, &str)> {
        match &self.mode {
            ConnectionPanelMode::ApiKey { provider, value } if value.can_submit() => {
                Some((provider, &value.0))
            }
            _ => None,
        }
    }

    pub fn take_api_key(&mut self) -> Option<(String, ApiKey)> {
        match &mut self.mode {
            ConnectionPanelMode::ApiKey { provider, value } => std::mem::take(value)
                .take()
                .map(|key| (provider.clone(), key)),
            _ => None,
        }
    }
}

fn picker_key(
    catalog: &ConnectionCatalogSnapshot,
    selected: &mut Option<usize>,
    key: KeyEvent,
) -> ConnectionPanelAction {
    if key.modifiers != KeyModifiers::NONE {
        return ConnectionPanelAction::None;
    }
    let methods = methods(catalog);
    match key.code {
        KeyCode::Esc => return ConnectionPanelAction::Close,
        KeyCode::Char('r') => return ConnectionPanelAction::Refresh,
        KeyCode::Char('d') => {
            return selected
                .and_then(|index| methods.get(index))
                .filter(|method| method.has_stored_credential)
                .map(|method| ConnectionPanelAction::Disconnect {
                    provider: method.provider.clone(),
                })
                .unwrap_or(ConnectionPanelAction::None);
        }
        KeyCode::Enter => {
            return selected
                .and_then(|index| methods.get(index))
                .map(|method| match method.kind.as_str() {
                    "api_key" => ConnectionPanelAction::EnterApiKey {
                        provider: method.provider.clone(),
                    },
                    "device_code" => ConnectionPanelAction::BeginDeviceCode {
                        provider: method.provider.clone(),
                    },
                    _ => ConnectionPanelAction::None,
                })
                .unwrap_or(ConnectionPanelAction::None);
        }
        KeyCode::Up => move_by(selected, methods.len(), -1),
        KeyCode::Down => move_by(selected, methods.len(), 1),
        KeyCode::PageUp => move_by(selected, methods.len(), -(PAGE_STEP as isize)),
        KeyCode::PageDown => move_by(selected, methods.len(), PAGE_STEP as isize),
        KeyCode::Home => *selected = (!methods.is_empty()).then_some(0),
        KeyCode::End => *selected = methods.len().checked_sub(1),
        _ => {}
    }
    ConnectionPanelAction::None
}

fn move_by(selected: &mut Option<usize>, len: usize, delta: isize) {
    let Some(selected) = selected else {
        return;
    };
    let maximum = len.saturating_sub(1) as isize;
    *selected = ((*selected as isize).saturating_add(delta)).clamp(0, maximum) as usize;
}

fn methods(catalog: &ConnectionCatalogSnapshot) -> Vec<&ConnectionMethodSnapshot> {
    catalog
        .providers
        .iter()
        .flat_map(|provider| provider.methods.iter())
        .collect()
}

impl ApiKeyInput {
    fn can_submit(&self) -> bool {
        !self.0.trim().is_empty()
    }

    fn push_str(&mut self, source: &str) {
        for character in source.chars() {
            if self.0.len().saturating_add(character.len_utf8()) > API_KEY_MAX_BYTES {
                break;
            }
            self.0.push(character);
        }
    }

    fn pop(&mut self) {
        self.0.pop();
    }

    fn take(self) -> Option<ApiKey> {
        ApiKey::new(self.0)
    }

    fn masked(&self) -> String {
        "•".repeat(self.0.chars().count())
    }
}

fn is_ctrl_c(key: KeyEvent) -> bool {
    key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL)
}

pub fn render(frame: &mut Frame<'_>, area: Rect, panel: &mut ConnectionPanel, palette: Palette) {
    match &mut panel.mode {
        ConnectionPanelMode::Picker { selected } => {
            render_picker(frame, area, &panel.catalog, *selected, palette)
        }
        ConnectionPanelMode::ApiKey { provider, value } => {
            render_api_key(frame, area, provider, value, palette)
        }
        ConnectionPanelMode::DeviceCode {
            provider,
            verification_uri,
            user_code,
            attempt,
            scroll,
        } => render_device_code(
            frame,
            area,
            provider,
            verification_uri.as_deref(),
            user_code.as_deref(),
            *attempt,
            scroll,
            palette,
        ),
    }
}

fn render_picker(
    frame: &mut Frame<'_>,
    area: Rect,
    catalog: &ConnectionCatalogSnapshot,
    selected: Option<usize>,
    palette: Palette,
) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(2), Constraint::Length(1)])
        .split(area);
    let height = usize::from(chunks[0].height.saturating_sub(2)).max(1);
    let width = usize::from(chunks[0].width.saturating_sub(2)).max(1);
    let methods = methods(catalog);
    let start = selected
        .map(|index| index.saturating_sub(height.saturating_sub(1)))
        .unwrap_or(0);
    let lines = if methods.is_empty() {
        vec![Line::styled(
            "No connection methods reported.",
            Style::default().fg(palette.muted),
        )]
    } else {
        methods
            .iter()
            .enumerate()
            .skip(start)
            .take(height)
            .map(|(index, method)| method_line(method, selected == Some(index), width, palette))
            .collect()
    };
    frame.render_widget(
        Paragraph::new(Text::from(lines)).block(
            Block::default()
                .title(" connect provider ")
                .border_style(palette.border())
                .borders(Borders::ALL),
        ),
        chunks[0],
    );
    frame.render_widget(
        Paragraph::new(if methods.is_empty() {
            "r refresh · Esc close"
        } else {
            "↑/↓ move · PgUp/PgDn · Home/End · Enter connect · d remove stored credential · r refresh · Esc close"
        })
        .alignment(Alignment::Center)
        .style(Style::default().fg(palette.muted)),
        chunks[1],
    );
}

fn method_line(
    method: &ConnectionMethodSnapshot,
    selected: bool,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let source = format!(
        "source: {} · environment: {} · stored fallback: {} · OAuth expiry: {}",
        method.source,
        method.environment_variable.as_deref().unwrap_or("—"),
        if method.has_stored_credential {
            "yes"
        } else {
            "no"
        },
        method.oauth_expires_at.as_deref().unwrap_or("—"),
    );
    let content = terminal_row(
        &format!(
            "{} {} / {} ({}) — {}",
            if selected { ">" } else { " " },
            method.provider,
            method.label,
            method.kind,
            source,
        ),
        width,
    );
    let style = if selected {
        palette.selection()
    } else {
        Style::default()
    };
    Line::from(Span::styled(content, style))
}

fn render_api_key(
    frame: &mut Frame<'_>,
    area: Rect,
    provider: &str,
    value: &ApiKeyInput,
    palette: Palette,
) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(2), Constraint::Length(1)])
        .split(area);
    frame.render_widget(
        Paragraph::new(terminal_row(
            &value.masked(),
            usize::from(chunks[0].width.saturating_sub(2)),
        ))
        .block(
            Block::default()
                .title(format!(" API key: {provider} "))
                .border_style(palette.border())
                .borders(Borders::ALL),
        ),
        chunks[0],
    );
    frame.render_widget(
        Paragraph::new("Enter save · Backspace edit · Esc cancel")
            .alignment(Alignment::Center)
            .style(Style::default().fg(palette.muted)),
        chunks[1],
    );
}

#[allow(clippy::too_many_arguments)]
fn render_device_code(
    frame: &mut Frame<'_>,
    area: Rect,
    provider: &str,
    verification_uri: Option<&str>,
    user_code: Option<&str>,
    attempt: Option<u32>,
    scroll: &mut usize,
    palette: Palette,
) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(2), Constraint::Length(1)])
        .split(area);
    let detail = match (verification_uri, user_code) {
        (Some(uri), Some(code)) => format!(
            "Open\n{uri}\nEnter code\n{code}\nLatest poll attempt: {}",
            attempt.map_or_else(|| "waiting".into(), |attempt| attempt.to_string()),
        ),
        _ => "Requesting device code…".into(),
    };
    let rows = wrap_device_challenge(
        &detail,
        usize::from(chunks[0].width.saturating_sub(2)).max(1),
    );
    let height = usize::from(chunks[0].height.saturating_sub(2));
    *scroll = (*scroll).min(rows.len().saturating_sub(height));
    frame.render_widget(
        Paragraph::new(Text::from(
            rows.into_iter()
                .skip(*scroll)
                .take(height)
                .map(Line::raw)
                .collect::<Vec<_>>(),
        ))
        .block(
            Block::default()
                .title(format!(" device login: {provider} "))
                .border_style(palette.border())
                .borders(Borders::ALL),
        ),
        chunks[0],
    );
    frame.render_widget(
        Paragraph::new("↑↓ scroll · Esc/Ctrl-C cancel")
            .alignment(Alignment::Center)
            .style(Style::default().fg(palette.muted)),
        chunks[1],
    );
}

/// Device URLs and codes must remain readable even in a minimum-size popup.
fn wrap_device_challenge(content: &str, width: usize) -> Vec<String> {
    let safe = crate::ui::sanitize_for_terminal(content);
    let mut rows = Vec::new();
    for line in safe.lines() {
        let mut row = String::new();
        let mut columns = 0;
        for grapheme in line.graphemes(true) {
            let cells = grapheme.width();
            if columns + cells > width && !row.is_empty() {
                rows.push(std::mem::take(&mut row));
                columns = 0;
            }
            row.push_str(grapheme);
            columns += cells;
        }
        rows.push(row);
    }
    rows
}

#[cfg(test)]
mod tests {
    use super::*;
    use wisp_protocol::events::{ConnectionMethodSnapshot, ConnectionProviderSnapshot};

    fn catalog() -> ConnectionCatalogSnapshot {
        ConnectionCatalogSnapshot {
            providers: vec![ConnectionProviderSnapshot {
                id: "openai".into(),
                label: "OpenAI".into(),
                methods: vec![
                    ConnectionMethodSnapshot {
                        provider: "openai".into(),
                        label: "API key".into(),
                        kind: "api_key".into(),
                        source: "environment".into(),
                        environment_variable: Some("OPENAI_API_KEY".into()),
                        oauth_expires_at: None,
                        has_stored_credential: true,
                    },
                    ConnectionMethodSnapshot {
                        provider: "openai-codex".into(),
                        label: "Device code".into(),
                        kind: "device_code".into(),
                        source: "stored".into(),
                        environment_variable: None,
                        oauth_expires_at: Some("2030-01-01T00:00:00Z".into()),
                        has_stored_credential: true,
                    },
                ],
            }],
        }
    }

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    #[test]
    fn picker_navigates_and_only_disconnects_stored_credentials() {
        let mut panel = ConnectionPanel::new(catalog());
        panel.handle_key(key(KeyCode::Down));
        assert_eq!(
            panel.handle_key(key(KeyCode::Char('d'))),
            ConnectionPanelAction::Disconnect {
                provider: "openai-codex".into()
            }
        );
        assert_eq!(
            panel.handle_key(key(KeyCode::Enter)),
            ConnectionPanelAction::BeginDeviceCode {
                provider: "openai-codex".into()
            }
        );
    }

    #[test]
    fn api_key_is_masked_capped_and_redacted() {
        let mut panel = ConnectionPanel::new(catalog());
        let ConnectionPanelMode::Picker { selected } = &mut panel.mode else {
            panic!("picker expected");
        };
        *selected = Some(0);
        let action = panel.handle_key(key(KeyCode::Enter));
        assert!(matches!(action, ConnectionPanelAction::EnterApiKey { .. }));
        panel.handle_key(KeyEvent::new(KeyCode::Char('A'), KeyModifiers::SHIFT));
        panel.handle_key(KeyEvent::new(KeyCode::Char('!'), KeyModifiers::SHIFT));
        panel.handle_paste(&format!("secret-{}", "x".repeat(API_KEY_MAX_BYTES)));
        let debug = format!("{panel:?}");
        assert!(!debug.contains("secret-"));
        let ConnectionPanelMode::ApiKey { value, .. } = &panel.mode else {
            panic!("API key entry expected");
        };
        assert_eq!(value.0.len(), API_KEY_MAX_BYTES);
        assert!(value.0.starts_with("A!secret-"));
        assert!(value.masked().chars().all(|character| character == '•'));
        let expected = value.0.clone();
        assert_eq!(
            panel.handle_key(key(KeyCode::Enter)),
            ConnectionPanelAction::SubmitApiKey
        );
        assert_eq!(panel.pending_api_key(), Some(("openai", expected.as_str())));
        let (_, api_key) = panel.take_api_key().expect("API key submission expected");
        assert!(!format!("{api_key:?}").contains("secret-"));
    }

    #[test]
    fn device_progress_and_cancel_are_modal() {
        let mut panel = ConnectionPanel::new(catalog());
        panel.mode = ConnectionPanelMode::DeviceCode {
            provider: "openai-codex".into(),
            verification_uri: None,
            user_code: None,
            attempt: None,
            scroll: 0,
        };
        panel.show_device_code(
            "openai-codex",
            "https://example.test/device".into(),
            "ABCD".into(),
        );
        panel.show_device_progress("openai-codex", 2);
        assert_eq!(
            panel.handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
            ConnectionPanelAction::CancelDeviceCode
        );
        panel.finish_device_code();
        assert!(matches!(panel.mode, ConnectionPanelMode::Picker { .. }));
    }
    #[test]
    fn long_device_challenges_wrap_losslessly_and_scroll_at_minimum_size() {
        use ratatui::{Terminal, backend::TestBackend};
        let uri = format!("https://example.test/{}END-URI", "u".repeat(470));
        let code = format!("{}END-CODE", "C".repeat(120));
        let detail = format!("Open\n{uri}\nEnter code\n{code}\nLatest poll attempt: waiting");
        let rows = wrap_device_challenge(&detail, 28);
        assert_eq!(rows.concat(), detail.replace('\n', ""));
        assert!(rows.iter().all(|row| row.width() <= 28));
        let mut panel = ConnectionPanel::new(catalog());
        panel.begin_device_code("openai-codex".into());
        panel.show_device_code("openai-codex", uri, code);
        let mut terminal = Terminal::new(TestBackend::new(30, 8)).unwrap();
        let mut seen = String::new();
        for _ in 0..rows.len() {
            terminal
                .draw(|frame| render(frame, frame.area(), &mut panel, Palette::default()))
                .unwrap();
            seen.push_str(
                &terminal
                    .backend()
                    .buffer()
                    .content
                    .iter()
                    .map(|cell| cell.symbol())
                    .collect::<String>(),
            );
            assert_eq!(
                panel.handle_key(key(KeyCode::Down)),
                ConnectionPanelAction::None
            );
        }
        for row in &rows {
            assert!(seen.contains(row), "missing challenge row: {row}");
        }
        panel.handle_key(key(KeyCode::Home));
        terminal
            .draw(|frame| render(frame, frame.area(), &mut panel, Palette::default()))
            .unwrap();
        assert!(terminal.backend().to_string().contains("Open"));
        assert_eq!(
            panel.handle_key(key(KeyCode::Esc)),
            ConnectionPanelAction::CancelDeviceCode
        );
    }
}
