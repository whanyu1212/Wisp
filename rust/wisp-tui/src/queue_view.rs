//! Queue inspection and confirmation over authoritative reducer snapshots.

#[cfg(test)]
mod tests;

use crate::reducer::queue_management::Operation;
use crate::{mouse::Rows, reducer::UiState, theme::Palette, ui::sanitize_for_terminal};
use crossterm::event::KeyCode;
use ratatui::{
    Frame,
    layout::Rect,
    widgets::{Block, Borders, List, ListItem, ListState},
};
use wisp_protocol::commands::{QueueKind, QueueMode};

pub(crate) enum Action {
    None,
    Close,
    Refresh,
    Mutate(Operation, String),
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Identity {
    token: Option<String>,
    session: Option<String>,
    active: bool,
    pending: bool,
}

impl Identity {
    fn of(state: &UiState) -> Self {
        Self {
            token: state.queue.management.token.clone(),
            session: state
                .selected_session
                .as_ref()
                .map(|s| s.session_id.clone()),
            active: state.active_prompt_editable(),
            pending: state.queue_management_pending(),
        }
    }
}

pub(crate) struct QueueView {
    kind: QueueKind,
    selected: usize,
    offset: usize,
    confirmation: Option<(Option<QueueKind>, String)>,
    rendered: Option<Identity>,
    row_count: usize,
    message: Option<String>,
}

impl Default for QueueView {
    fn default() -> Self {
        Self {
            kind: QueueKind::Steering,
            selected: 0,
            offset: 0,
            confirmation: None,
            rendered: None,
            row_count: 0,
            message: None,
        }
    }
}

impl QueueView {
    fn messages<'a>(&self, state: &'a UiState) -> &'a [crate::reducer::QueuedMessage] {
        match self.kind {
            QueueKind::Steering => &state.queue.steering,
            QueueKind::FollowUp => &state.queue.follow_up,
        }
    }

    pub fn select_mouse(&mut self, index: usize, state: &UiState) -> bool {
        if self.rendered.as_ref() != Some(&Identity::of(state)) || index >= self.row_count {
            return false;
        }
        self.selected = index;
        true
    }

    pub fn key(&mut self, key: KeyCode, state: &UiState) -> Action {
        if key == KeyCode::Esc {
            return Action::Close;
        }
        if matches!(
            key,
            KeyCode::Up | KeyCode::Down | KeyCode::Home | KeyCode::End
        ) {
            // A confirmation can open/close before the next paint. Navigate the
            // current layout, not the previous frame's two-row confirmation.
            let row_count = if self.confirmation.is_some() {
                2
            } else {
                6 + self.messages(state).len().max(1)
            };
            self.selected = match key {
                KeyCode::Up => self.selected.saturating_sub(1),
                KeyCode::Down => self
                    .selected
                    .saturating_add(1)
                    .min(row_count.saturating_sub(1)),
                KeyCode::Home => 0,
                _ => row_count.saturating_sub(1),
            };
            self.rendered = None;
            return Action::None;
        }
        if key != KeyCode::Enter || self.rendered.as_ref() != Some(&Identity::of(state)) {
            return Action::None;
        }
        self.rendered = None;
        if let Some((kind, token)) = self.confirmation.take() {
            let confirmed = self.selected == 1;
            self.selected = 0;
            return if confirmed {
                Action::Mutate(Operation::Clear(kind), token)
            } else {
                Action::None
            };
        }
        if self.selected == 0 {
            self.kind = match self.kind {
                QueueKind::Steering => QueueKind::FollowUp,
                QueueKind::FollowUp => QueueKind::Steering,
            };
            self.offset = 0;
            return Action::None;
        }
        if self.selected == 5 {
            return Action::Refresh;
        }
        if self.selected > 5 {
            return Action::None;
        }
        if !state.active_prompt_editable()
            || state.queue_management_pending()
            || state.queue.management.token.is_none()
        {
            self.message = Some("Read-only: active run and fresh snapshot required.".into());
            return Action::None;
        }
        let token = state.queue.management.token.clone().expect("checked token");
        match self.selected {
            1 => {
                let current = match self.kind {
                    QueueKind::Steering => state.queue.management.steering_mode,
                    QueueKind::FollowUp => state.queue.management.follow_up_mode,
                };
                let next = match current {
                    QueueMode::All => QueueMode::OneAtATime,
                    QueueMode::OneAtATime => QueueMode::All,
                };
                Action::Mutate(Operation::Mode(self.kind, next), token)
            }
            2 => Action::Mutate(Operation::Restore(self.kind), token),
            3 | 4 => {
                self.confirmation = Some(((self.selected == 3).then_some(self.kind), token));
                self.selected = 0; // Destruction always defaults to Cancel.
                self.offset = 0;
                Action::None
            }
            _ => Action::None,
        }
    }

    pub fn render(
        &mut self,
        frame: &mut Frame,
        area: Rect,
        state: &UiState,
        palette: Palette,
    ) -> Rows {
        let identity = Identity::of(state);
        if self.confirmation.as_ref().is_some_and(|(_, token)| {
            identity.token.as_ref() != Some(token) || !identity.active || identity.pending
        }) || self
            .rendered
            .as_ref()
            .is_some_and(|old| old.session != identity.session)
        {
            self.confirmation = None;
            self.selected = 0;
            self.message = Some("Queue changed; confirmation cancelled.".into());
        }
        let count = self.messages(state).len();
        let label = match self.kind {
            QueueKind::Steering => "Steering",
            QueueKind::FollowUp => "Follow-up",
        };
        let mode = match self.kind {
            QueueKind::Steering => state.queue.management.steering_mode,
            QueueKind::FollowUp => state.queue.management.follow_up_mode,
        };
        let mode_label = match mode {
            QueueMode::All => "All",
            QueueMode::OneAtATime => "One at a time",
        };
        let title = if let Some((kind, _)) = &self.confirmation {
            let (scope, count) = match kind {
                Some(QueueKind::Steering) => ("steering", state.queued_steering()),
                Some(QueueKind::FollowUp) => ("follow-up", state.queued_follow_ups()),
                None => ("both", state.queued_steering() + state.queued_follow_ups()),
            };
            format!("Clear {scope}: {count} items?")
        } else {
            format!("{label}: {count} · {mode_label}")
        };
        let mut rows = if self.confirmation.is_some() {
            vec!["Cancel".into(), "Confirm clear".into()]
        } else {
            vec![
                format!(
                    "Switch queue ({}/{})",
                    state.queued_steering(),
                    state.queued_follow_ups()
                ),
                "Change drain mode".into(),
                "Restore newest".into(),
                "Clear this queue…".into(),
                "Clear both queues…".into(),
                "Refresh".into(),
            ]
        };
        if self.confirmation.is_none() {
            for (index, message) in self.messages(state).iter().enumerate() {
                let preview: String = message.content.chars().take(160).collect();
                rows.push(format!(
                    "{}. {}",
                    index + 1,
                    sanitize_for_terminal(&preview).replace(['\n', '\r', '\t'], " ")
                ));
            }
            if count == 0 {
                rows.push("No pending items".into());
            }
        }
        let status = if identity.pending {
            "Pending…"
        } else if state.queue.management.refresh.is_some() {
            "Refreshing…"
        } else if let Some(error) = state.queue.management.error.as_deref() {
            error
        } else if let Some(message) = self.message.as_deref() {
            message
        } else if !identity.active || identity.token.is_none() {
            "Read-only · Esc close"
        } else {
            "↑↓ select · Enter · Esc"
        };
        let status: String = status.chars().take(160).collect();
        let block = Block::default()
            .borders(Borders::ALL)
            .title(title)
            .title_bottom(sanitize_for_terminal(&status))
            .style(palette.overlay());
        let inner = block.inner(area);
        self.row_count = rows.len();
        self.selected = self.selected.min(self.row_count.saturating_sub(1));
        let mut selection = ListState::default()
            .with_selected(Some(self.selected))
            .with_offset(self.offset);
        frame.render_stateful_widget(
            List::new(rows.into_iter().map(ListItem::new).collect::<Vec<_>>())
                .block(block)
                .highlight_style(palette.selection())
                .highlight_symbol("› "),
            area,
            &mut selection,
        );
        self.offset = selection.offset();
        self.rendered = Some(identity);
        Rows::new(inner, self.offset, self.row_count, 1)
    }
}
