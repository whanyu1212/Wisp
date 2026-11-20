//! Address and navigation input for the manual Chromium pane.

use super::*;
use crate::side_panel::BrowserControl;

const MAX_ADDRESS_CHARS: usize = 2048;

impl LiveUi {
    pub(super) fn live_browser_visible(&self) -> bool {
        self.side_panel.mode() == side_panel::PanelMode::Browser
            && self.side_panel.visible()
            && self
                .state
                .browser_viewport
                .as_ref()
                .is_some_and(|viewport| viewport.live)
            && self.key_help.is_none()
            && matches!(self.active_overlay(), None | Some(OverlayKind::SidePanel))
    }

    pub(super) fn browser_address_active(&self) -> bool {
        self.side_panel.browser_address_focused() && self.live_browser_visible()
    }

    pub(super) fn clear_browser_address(&mut self) {
        self.render_pending |= self.side_panel.dismiss_browser_address();
    }

    pub(super) async fn handle_live_browser_control(
        &mut self,
        control: BrowserControl,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        if !self.live_browser_visible() || self.browser_interaction_blocked() {
            return Ok(LoopControl::Continue);
        }
        if control == BrowserControl::Address && self.browser_address_active() {
            return Ok(LoopControl::Continue);
        }
        if self.flush_browser_typing(writer, limit).await? == LoopControl::Exit {
            return Ok(LoopControl::Exit);
        }
        self.notice = None;
        self.exit_browser_takeover();
        self.clear_browser_address();
        self.render_pending = true;
        let action = match control {
            BrowserControl::Address => {
                let address = self
                    .state
                    .browser_viewport
                    .as_ref()
                    .and_then(|viewport| viewport.url.as_deref())
                    .unwrap_or_default();
                self.side_panel.focus_browser_address(address);
                return Ok(LoopControl::Continue);
            }
            BrowserControl::Back => BrowserNavAction::Back,
            BrowserControl::Forward => BrowserNavAction::LiveForward,
            BrowserControl::Reload => BrowserNavAction::Reload,
        };
        self.dispatch(
            UiAction::BrowserInteract(BrowserInteractRequest::Nav(action)),
            writer,
            limit,
        )
        .await
    }

    pub(super) async fn handle_browser_address_input(
        &mut self,
        input: Input,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let input = if matches!(&input, Input::Key(key) if ClipboardAction::for_key(*key) == Some(ClipboardAction::Paste))
        {
            match self.clipboard.paste() {
                Ok(text) => Input::Paste(text),
                Err(error) => {
                    self.notice = Some(format!("Could not paste the browser address: {error}."));
                    self.render_pending = true;
                    return Ok(LoopControl::Continue);
                }
            }
        } else {
            input
        };
        let Some(editor) = self.side_panel.browser_address_editor() else {
            return Ok(LoopControl::Continue);
        };
        match input {
            Input::Key(key) if is_escape(key) || is_ctrl_c(key) => self.clear_browser_address(),
            Input::Key(key) if key.code == KeyCode::Enter && key.modifiers.is_empty() => {
                if let Some(url) = navigation_address(editor.text()) {
                    self.clear_browser_address();
                    self.notice = None;
                    return self
                        .dispatch(
                            UiAction::BrowserInteract(BrowserInteractRequest::LiveNavigate { url }),
                            writer,
                            limit,
                        )
                        .await;
                }
                self.notice = Some("Enter an HTTP or HTTPS address up to 2,048 characters.".into());
            }
            Input::Key(key) if key.code == KeyCode::Tab || key.code == KeyCode::BackTab => {
                self.clear_browser_address();
                self.try_enter_browser_takeover();
            }
            Input::Key(key)
                if browser_address_shortcut(key)
                    || (key.code == KeyCode::Char('a')
                        && key.modifiers == KeyModifiers::CONTROL) =>
            {
                editor.handle_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::ALT));
            }
            Input::Key(key) => {
                let newline_key = key.code == KeyCode::Enter
                    || (key.code == KeyCode::Char('j')
                        && key.modifiers.contains(KeyModifiers::CONTROL));
                let can_edit = key.modifiers.contains(KeyModifiers::CONTROL)
                    || !matches!(key.code, KeyCode::Char(_))
                    || editor.text().chars().count()
                        - editor.text()[editor.insert_range()].chars().count()
                        < MAX_ADDRESS_CHARS;
                if !newline_key && can_edit {
                    editor.handle_key(key);
                }
            }
            Input::Paste(pasted) => {
                let address = pasted.trim();
                let range = editor.insert_range();
                let retained =
                    editor.text().chars().count() - editor.text()[range.clone()].chars().count();
                if retained + address.chars().count() <= MAX_ADDRESS_CHARS
                    && !address.chars().any(|ch| {
                        ch.is_whitespace() || ch.is_control() || crate::is_bidi_control(ch)
                    })
                {
                    editor.replace_range(range, address);
                }
            }
            Input::Redraw => self.refresh_on_redraw(),
            Input::Error(error) => return Err(Error::Io(error)),
            _ => {}
        }
        self.render_pending = true;
        Ok(LoopControl::Continue)
    }
}

pub(super) fn browser_address_shortcut(key: KeyEvent) -> bool {
    matches!(key.code, KeyCode::Char('l' | 'L')) && key.modifiers == KeyModifiers::CONTROL
}

fn navigation_address(text: &str) -> Option<String> {
    let text = text.trim();
    if text.is_empty() || text.chars().any(|ch| ch.is_whitespace() || ch.is_control()) {
        return None;
    }
    let url = if let Some((scheme, _)) = text.split_once("://") {
        if !scheme.eq_ignore_ascii_case("http") && !scheme.eq_ignore_ascii_case("https") {
            return None;
        }
        text.to_owned()
    } else {
        format!("https://{text}")
    };
    (url.chars().count() <= MAX_ADDRESS_CHARS).then_some(url)
}
