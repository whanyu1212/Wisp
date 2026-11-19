//! Provider connection panel with keyboard activation and pointer selection.

use crate::dialog::{self, Dialog, Hint, PickerRow};
use crate::mouse::Rows;
use crate::reducer::{API_KEY_MAX_BYTES, ApiKey};
use crate::session_picker::terminal_row;
use crate::theme::Palette;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::{ConnectionCatalogSnapshot, ConnectionMethodSnapshot};

const PAGE_STEP: usize = 10;
/// Rows the docked API-key entry needs: borders, the masked input, a gap, note, and footer.
pub const API_KEY_PANEL_HEIGHT: u16 = 6;

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
    /// Whether the panel is taking an API key, which docks above the composer.
    pub fn api_key_entry_active(&self) -> bool {
        matches!(self.mode, ConnectionPanelMode::ApiKey { .. })
    }

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

    #[cfg(test)]
    fn masked(&self) -> String {
        "•".repeat(self.0.chars().count())
    }
}

fn is_ctrl_c(key: KeyEvent) -> bool {
    key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL)
}

pub fn render(
    frame: &mut Frame<'_>,
    area: Rect,
    panel: &mut ConnectionPanel,
    palette: Palette,
) -> dialog::Rendered {
    match &mut panel.mode {
        ConnectionPanelMode::Picker { selected } => {
            render_picker(frame, area, &panel.catalog, *selected, palette)
        }
        ConnectionPanelMode::ApiKey { provider, value } => {
            let method = methods(&panel.catalog)
                .into_iter()
                .find(|method| method.provider == *provider && method.kind == "api_key");
            dialog::Rendered {
                rows: Rows::default(),
                controls: render_api_key(frame, area, provider, value, method, palette),
            }
        }
        ConnectionPanelMode::DeviceCode {
            provider,
            verification_uri,
            user_code,
            attempt,
            scroll,
        } => dialog::Rendered {
            rows: Rows::default(),
            controls: render_device_code(
                frame,
                area,
                provider,
                verification_uri.as_deref(),
                user_code.as_deref(),
                *attempt,
                scroll,
                palette,
            ),
        },
    }
}

impl ConnectionPanel {
    pub fn select_mouse(&mut self, index: usize) -> bool {
        let ConnectionPanelMode::Picker { selected } = &mut self.mode else {
            return false;
        };
        if index >= methods(&self.catalog).len() {
            return false;
        }
        *selected = Some(index);
        true
    }
}

fn render_picker(
    frame: &mut Frame<'_>,
    area: Rect,
    catalog: &ConnectionCatalogSnapshot,
    selected: Option<usize>,
    palette: Palette,
) -> dialog::Rendered {
    let methods = methods(catalog);
    let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
    let hints = if methods.is_empty() {
        vec![
            Hint {
                label: "r",
                description: "refresh",
                key: key(KeyCode::Char('r')),
            },
            Hint {
                label: "Esc",
                description: "close",
                key: key(KeyCode::Esc),
            },
        ]
    } else {
        let mut hints = vec![
            Hint {
                label: "Enter",
                description: "connect",
                key: key(KeyCode::Enter),
            },
            Hint {
                label: "d",
                description: "remove",
                key: key(KeyCode::Char('d')),
            },
            Hint {
                label: "r",
                description: "refresh",
                key: key(KeyCode::Char('r')),
            },
            Hint {
                label: "Esc",
                description: "close",
                key: key(KeyCode::Esc),
            },
        ];
        if area.width >= 60 {
            hints.extend([
                Hint {
                    label: "↑",
                    description: "prev",
                    key: key(KeyCode::Up),
                },
                Hint {
                    label: "↓",
                    description: "next",
                    key: key(KeyCode::Down),
                },
            ]);
        }
        hints
    };
    let layout = Dialog {
        title: "connect provider",
        tabs: &[],
        filter: None,
        hints: &hints,
    }
    .render(frame, area, palette);
    let inner = layout.body;
    if inner.width == 0 || inner.height == 0 {
        return dialog::Rendered {
            rows: Rows::default(),
            controls: layout.controls,
        };
    }
    let height = usize::from(inner.height);
    let start = selected
        .map(|index| index.saturating_sub(height.saturating_sub(1)))
        .unwrap_or(0);
    if methods.is_empty() {
        let empty = Rect::new(inner.x + 1, inner.y, inner.width.saturating_sub(2), 1);
        frame.render_widget(
            Paragraph::new(terminal_row(
                "No connection methods reported.",
                usize::from(empty.width),
            ))
            .style(Style::default().fg(palette.muted)),
            empty,
        );
    } else {
        for (visible_index, (index, method)) in methods
            .iter()
            .enumerate()
            .skip(start)
            .take(height)
            .enumerate()
        {
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
            let label = format!(
                "{} / {} ({}) — {}",
                method.provider, method.label, method.kind, source
            );
            let row = Rect::new(inner.x, inner.y + visible_index as u16, inner.width, 1);
            PickerRow {
                label: &label,
                description: None,
                tag: None,
                selected: selected == Some(index),
                muted: false,
            }
            .render(frame, row, palette);
        }
    }
    dialog::Rendered {
        rows: Rows::new(inner, start, methods.len(), 1),
        controls: layout.controls,
    }
}

/// Draw the masked API-key entry with a terminal cursor after the last character, so
/// typing and pasting visibly land. The count in the corner confirms a paste arrived;
/// when an environment variable already supplies this provider's key, the note says it
/// takes precedence over a saved one.
fn render_api_key(
    frame: &mut Frame<'_>,
    area: Rect,
    provider: &str,
    value: &ApiKeyInput,
    method: Option<&ConnectionMethodSnapshot>,
    palette: Palette,
) -> dialog::Controls {
    let muted = Style::default().fg(palette.muted);
    let count = value.0.chars().count();
    let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
    let hints = [
        Hint {
            label: "Enter",
            description: "save",
            key: key(KeyCode::Enter),
        },
        Hint {
            label: "Esc",
            description: "cancel",
            key: key(KeyCode::Esc),
        },
    ];
    let title = format!("API key: {provider}");
    let layout = Dialog {
        title: &title,
        tabs: &[],
        filter: None,
        hints: &hints,
    }
    .render(frame, area, palette);
    let body = layout.body;
    let inner = Rect::new(
        body.x.saturating_add(1),
        body.y,
        body.width.saturating_sub(2),
        body.height,
    );
    if inner.height == 0 || inner.width == 0 {
        return layout.controls;
    }

    // Keep the end of a long key in view, where the cursor is.
    const PROMPT: &str = "› ";
    let count_badge = (count > 0).then(|| format!("{count} chars"));
    let badge_reserve = count_badge
        .as_ref()
        .map_or(0, |badge| badge.width().saturating_add(2));
    let room = usize::from(inner.width).saturating_sub(PROMPT.width() + 1 + badge_reserve);
    let masked = if count > room {
        format!("…{}", "•".repeat(room.saturating_sub(1)))
    } else {
        "•".repeat(count)
    };
    let cursor_x = inner.x + u16::try_from(PROMPT.width() + masked.width()).unwrap_or(0);
    let mut lines = vec![Line::from(vec![
        Span::styled(PROMPT, Style::default().fg(palette.primary)),
        Span::raw(masked),
    ])];
    if inner.height >= 3 {
        let environment = method
            .filter(|method| method.source == "environment")
            .and_then(|method| method.environment_variable.as_deref());
        let note = match environment {
            Some(variable) => Line::styled(
                terminal_row(
                    &format!("{variable} is set and takes precedence over a saved key."),
                    usize::from(inner.width),
                ),
                Style::default().fg(palette.warning),
            ),
            None => Line::styled(
                terminal_row(
                    "Paste or type the key. It stays masked and is saved to Wisp's auth file.",
                    usize::from(inner.width),
                ),
                muted,
            ),
        };
        lines.push(Line::default());
        lines.push(note);
    }
    frame.render_widget(Paragraph::new(lines), inner);
    if let Some(badge) = count_badge.filter(|badge| badge.width() + 4 <= usize::from(inner.width)) {
        let badge_width = badge.width() as u16;
        frame.render_widget(
            Paragraph::new(Span::styled(badge, muted)),
            Rect::new(inner.right() - badge_width, inner.y, badge_width, 1),
        );
    }
    frame.set_cursor_position((cursor_x.min(inner.right().saturating_sub(1)), inner.y));
    layout.controls
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
) -> dialog::Controls {
    let title = format!("device login: {provider}");
    let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
    let hints = [
        Hint {
            label: "Esc",
            description: "cancel",
            key: key(KeyCode::Esc),
        },
        Hint {
            label: "↑",
            description: "up",
            key: key(KeyCode::Up),
        },
        Hint {
            label: "↓",
            description: "down",
            key: key(KeyCode::Down),
        },
    ];
    let layout = Dialog {
        title: &title,
        tabs: &[],
        filter: None,
        hints: &hints,
    }
    .render(frame, area, palette);
    let body = layout.body;
    if body.width == 0 || body.height == 0 {
        return layout.controls;
    }
    let detail = match (verification_uri, user_code) {
        (Some(uri), Some(code)) => format!(
            "Open\n{uri}\nEnter code\n{code}\nLatest poll attempt: {}",
            attempt.map_or_else(|| "waiting".into(), |attempt| attempt.to_string()),
        ),
        _ => "Requesting device code…".into(),
    };
    let rows = wrap_device_challenge(&detail, usize::from(body.width).max(1));
    let height = usize::from(body.height);
    *scroll = (*scroll).min(rows.len().saturating_sub(height));
    frame.render_widget(
        Paragraph::new(
            rows.into_iter()
                .skip(*scroll)
                .take(height)
                .map(Line::raw)
                .collect::<Vec<_>>(),
        ),
        body,
    );
    layout.controls
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
    fn api_key_entry_shows_a_cursor_count_and_environment_precedence() {
        use ratatui::{Terminal, backend::TestBackend};
        let mut panel = ConnectionPanel::new(catalog());
        assert!(!panel.api_key_entry_active());
        panel.handle_key(key(KeyCode::Enter));
        assert!(panel.api_key_entry_active());
        panel.handle_paste("sk-test-123");
        let mut terminal = Terminal::new(TestBackend::new(70, API_KEY_PANEL_HEIGHT)).unwrap();
        terminal
            .draw(|frame| {
                render(frame, frame.area(), &mut panel, Palette::default());
            })
            .unwrap();
        let rendered = terminal.backend().to_string();
        assert!(rendered.contains("API key: openai"), "{rendered}");
        assert!(rendered.contains("› •••••••••••"), "{rendered}");
        assert!(!rendered.contains("sk-test"), "{rendered}");
        assert!(rendered.contains("11 chars"), "{rendered}");
        // The catalog says OPENAI_API_KEY currently supplies the key.
        assert!(rendered.contains("OPENAI_API_KEY is set"), "{rendered}");
        // The cursor sits right after the eleventh mask character.
        let cursor = terminal.get_cursor_position().unwrap();
        assert_eq!((cursor.x, cursor.y), (2 + 2 + 11, 1));

        // A key longer than the row keeps its end, where the cursor is, in view.
        panel.handle_paste(&"x".repeat(200));
        let mut narrow = Terminal::new(TestBackend::new(40, API_KEY_PANEL_HEIGHT)).unwrap();
        narrow
            .draw(|frame| {
                render(frame, frame.area(), &mut panel, Palette::default());
            })
            .unwrap();
        let narrow_text = narrow.backend().to_string();
        assert!(narrow_text.contains("› …•"), "{narrow_text}");
        assert!(narrow.get_cursor_position().unwrap().x < 40 - 2);
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
    fn dynamic_connection_titles_and_challenges_are_terminal_safe() {
        use ratatui::{Terminal, backend::TestBackend};

        let hostile = "provider\u{1b}]52;c;owned\u{7}\u{009b}2J\u{202e}tail";
        let mut catalog = catalog();
        catalog.providers[0].methods[0].provider = hostile.into();
        catalog.providers[0].methods[1].provider = hostile.into();
        let mut panel = ConnectionPanel::new(catalog);
        let mut terminal = Terminal::new(TestBackend::new(30, 8)).unwrap();

        panel.handle_key(key(KeyCode::Enter));
        terminal
            .draw(|frame| {
                render(frame, frame.area(), &mut panel, Palette::default());
            })
            .unwrap();
        assert_terminal_safe(terminal.backend().buffer());

        panel.return_to_picker();
        panel.handle_key(key(KeyCode::Down));
        panel.handle_key(key(KeyCode::Enter));
        panel.show_device_code(
            hostile,
            "https://example.invalid/\u{1b}]0;owned\u{7}".into(),
            "CODE\u{1b}[2J\u{2066}".into(),
        );
        terminal
            .draw(|frame| {
                render(frame, frame.area(), &mut panel, Palette::default());
            })
            .unwrap();
        assert_terminal_safe(terminal.backend().buffer());
    }

    fn assert_terminal_safe(buffer: &ratatui::buffer::Buffer) {
        assert!(buffer.content.iter().all(|cell| {
            cell.symbol()
                .chars()
                .all(|character| !character.is_control() && !crate::is_bidi_control(character))
        }));
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
                .draw(|frame| {
                    render(frame, frame.area(), &mut panel, Palette::default());
                })
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
            .draw(|frame| {
                render(frame, frame.area(), &mut panel, Palette::default());
            })
            .unwrap();
        assert!(terminal.backend().to_string().contains("Open"));
        assert_eq!(
            panel.handle_key(key(KeyCode::Esc)),
            ConnectionPanelAction::CancelDeviceCode
        );
    }
}
