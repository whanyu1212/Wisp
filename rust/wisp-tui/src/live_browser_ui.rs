//! Address and navigation input for the manual Chromium pane.

use super::*;
use crate::side_panel::BrowserControl;

const MAX_ADDRESS_CHARS: usize = 2048;

impl LiveUi {
    /// Whether the Browser view is on screen and no key help or other popup covers it.
    fn browser_view_reachable(&self) -> bool {
        self.side_panel.mode() == side_panel::PanelMode::Browser
            && self.side_panel.visible()
            && self.key_help.is_none()
            && matches!(self.active_overlay(), None | Some(OverlayKind::SidePanel))
    }

    pub(super) fn live_browser_visible(&self) -> bool {
        self.browser_view_reachable()
            && self
                .state
                .browser_viewport
                .as_ref()
                .is_some_and(|viewport| viewport.live)
    }

    /// Whether the Browser view shows its start page with an address field: no page
    /// is open, and the project is not known to be untrusted.
    pub(super) fn browser_start_visible(&self) -> bool {
        self.browser_view_reachable()
            && self.state.browser_viewport.is_none()
            && !self.state.live_browser_untrusted()
    }

    /// Whether the address field can take focus: on a live page or the start page.
    pub(super) fn browser_address_editable(&self) -> bool {
        self.live_browser_visible() || self.browser_start_visible()
    }

    pub(super) fn browser_address_active(&self) -> bool {
        self.side_panel.browser_address_focused() && self.browser_address_editable()
    }

    pub(super) fn clear_browser_address(&mut self) {
        self.render_pending |= self.side_panel.dismiss_browser_address();
    }

    /// Prepare the start page after its view was opened: focus an empty address field
    /// and ask the backend for its trust and recent addresses. An open page keeps its
    /// focus, and an untrusted project gets no address field.
    pub(super) async fn begin_browser_start(
        &mut self,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        if self.state.browser_viewport.is_some()
            || self.side_panel.mode() != side_panel::PanelMode::Browser
            || !self.side_panel.visible()
        {
            return Ok(LoopControl::Continue);
        }
        if self.browser_start_visible() && !self.browser_address_active() {
            self.side_panel.focus_browser_address("");
        }
        self.render_pending = true;
        self.dispatch(UiAction::LoadBrowserAddresses, writer, limit)
            .await
    }

    /// Open `url`, an address or search text sent as typed, in a new live page. The
    /// backend decides which it is and starts Chrome.
    pub(super) async fn open_live_page(
        &mut self,
        url: String,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        if self.browser_interaction_blocked() {
            return Ok(LoopControl::Continue);
        }
        self.exit_browser_takeover();
        self.clear_browser_address();
        self.side_panel.show(side_panel::PanelMode::Browser);
        self.notice = None;
        self.render_pending = true;
        self.dispatch(
            UiAction::BrowserInteract(BrowserInteractRequest::LiveOpen { url }),
            writer,
            limit,
        )
        .await
    }

    pub(super) async fn handle_browser_control(
        &mut self,
        control: BrowserControl,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        if self.browser_start_visible() {
            return self.handle_start_page_control(control, writer, limit).await;
        }
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
            // Recent addresses are listed on the start page only.
            BrowserControl::Recent(_) => return Ok(LoopControl::Continue),
        };
        self.dispatch(
            UiAction::BrowserInteract(BrowserInteractRequest::Nav(action)),
            writer,
            limit,
        )
        .await
    }

    /// The start page's controls: the address field takes focus and a recent address
    /// opens. With no page there is no history, so Back, Forward and Reload do nothing.
    async fn handle_start_page_control(
        &mut self,
        control: BrowserControl,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        match control {
            BrowserControl::Address => {
                if !self.browser_address_active() {
                    self.side_panel.focus_browser_address("");
                }
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            BrowserControl::Recent(index) => {
                match self.state.browser_recent_addresses().get(index).cloned() {
                    Some(url) => self.open_live_page(url, writer, limit).await,
                    None => Ok(LoopControl::Continue),
                }
            }
            BrowserControl::Back | BrowserControl::Forward | BrowserControl::Reload => {
                Ok(LoopControl::Continue)
            }
        }
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
        let start_page = self.browser_start_visible();
        if start_page {
            if let Input::Key(key) = &input {
                if let Some(control) = self.handle_recent_key(*key, writer, limit).await? {
                    return Ok(control);
                }
            } else if matches!(input, Input::Paste(_)) {
                self.side_panel.select_browser_recent(None);
            }
        }
        let Some(editor) = self.side_panel.browser_address_editor() else {
            return Ok(LoopControl::Continue);
        };
        match input {
            Input::Key(key) if is_escape(key) || is_ctrl_c(key) => self.clear_browser_address(),
            Input::Key(key) if key.code == KeyCode::Enter && key.modifiers.is_empty() => {
                let typed = editor.text().to_owned();
                return self
                    .submit_browser_address(&typed, start_page, writer, limit)
                    .await;
            }
            Input::Key(key) if key.code == KeyCode::Tab || key.code == KeyCode::BackTab => {
                self.clear_browser_address();
                if !start_page {
                    self.try_enter_browser_takeover();
                }
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
                    && !address
                        .chars()
                        .any(|ch| ch.is_control() || crate::is_bidi_control(ch))
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

    /// Enter in the address field: open the text in a new live page from the start
    /// page, or load it in the open page. Invalid text leaves the field focused with a
    /// notice; an empty start page field does nothing.
    async fn submit_browser_address(
        &mut self,
        typed: &str,
        start_page: bool,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        match browser_address_text(typed) {
            Some(url) if start_page => self.open_live_page(url, writer, limit).await,
            Some(url) => {
                self.clear_browser_address();
                self.notice = None;
                self.dispatch(
                    UiAction::BrowserInteract(BrowserInteractRequest::LiveNavigate { url }),
                    writer,
                    limit,
                )
                .await
            }
            None => {
                if !(start_page && typed.trim().is_empty()) {
                    self.notice =
                        Some("Enter an address or a search up to 2,048 characters.".into());
                }
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
        }
    }

    /// Move through the start page's recent addresses: Down enters the list and moves
    /// down it, Up moves up it and returns to the field from the first row, and Enter
    /// opens the highlighted address. Any other key returns focus to the field and is
    /// left to it (`None`), so typing goes on there.
    async fn handle_recent_key(
        &mut self,
        key: KeyEvent,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<Option<LoopControl>, Error> {
        let selected = self.side_panel.browser_recent();
        let count = self.state.browser_recent_addresses().len();
        match (key.code, selected) {
            (KeyCode::Down, _) if key.modifiers.is_empty() => {
                let next = selected.map_or(0, |index| index + 1);
                if next < count {
                    self.side_panel.select_browser_recent(Some(next));
                }
            }
            (KeyCode::Up, _) if key.modifiers.is_empty() => self
                .side_panel
                .select_browser_recent(selected.and_then(|index| index.checked_sub(1))),
            (KeyCode::Enter, Some(index)) if key.modifiers.is_empty() => {
                return self
                    .handle_start_page_control(BrowserControl::Recent(index), writer, limit)
                    .await
                    .map(Some);
            }
            _ => {
                self.side_panel.select_browser_recent(None);
                return Ok(None);
            }
        }
        self.render_pending = true;
        Ok(Some(LoopControl::Continue))
    }
}

pub(super) fn browser_address_shortcut(key: KeyEvent) -> bool {
    matches!(key.code, KeyCode::Char('l' | 'L')) && key.modifiers == KeyModifiers::CONTROL
}

/// The address field's text as the backend receives it: trimmed, and refused when
/// empty, holding a control character, or longer than 2,048 characters. The backend
/// decides whether it is an address or a search, so spaces are kept.
fn browser_address_text(text: &str) -> Option<String> {
    let text = text.trim();
    (!text.is_empty()
        && !text.chars().any(char::is_control)
        && text.chars().count() <= MAX_ADDRESS_CHARS)
        .then(|| text.to_owned())
}
