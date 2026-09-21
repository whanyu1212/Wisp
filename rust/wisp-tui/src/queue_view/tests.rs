use super::*;
use crate::reducer::{ActiveCommand, ActiveCommandType, InteractionStatus, ViewStatus};
use ratatui::{Terminal, backend::TestBackend};

fn active() -> UiState {
    let mut state = UiState::unconfigured();
    state.view_status = ViewStatus::Running;
    state.interaction_status = InteractionStatus::Running;
    state.current_command = Some(ActiveCommand {
        id: "prompt".into(),
        command_type: ActiveCommandType::Prompt,
    });
    state.queue.management.token = Some("snapshot".into());
    state
}

fn paint(view: &mut QueueView, state: &UiState, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| {
            view.render(frame, frame.area(), state, Palette::default());
        })
        .unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}

#[test]
fn clear_confirmation_defaults_to_cancel_and_is_snapshot_bound() {
    let mut view = QueueView::default();
    let mut state = active();
    view.selected = 4;
    paint(&mut view, &state, 80, 24);
    assert!(matches!(view.key(KeyCode::Enter, &state), Action::None));
    assert_eq!(view.selected, 0);
    paint(&mut view, &state, 80, 24);
    assert!(matches!(view.key(KeyCode::Enter, &state), Action::None));
    assert!(view.confirmation.is_none());
    view.selected = 4;
    paint(&mut view, &state, 80, 24);
    view.key(KeyCode::Enter, &state);
    view.selected = 1;
    paint(&mut view, &state, 80, 24);
    state.queue.management.token = Some("changed".into());
    assert!(matches!(view.key(KeyCode::Enter, &state), Action::None));
    assert!(!view.select_mouse(1, &state));
    paint(&mut view, &state, 80, 24);
    assert!(view.confirmation.is_none());
}

#[test]
fn navigation_after_cancelling_confirmation_uses_the_current_layout() {
    let mut view = QueueView::default();
    let state = active();
    view.selected = 4;
    paint(&mut view, &state, 80, 24);
    view.key(KeyCode::Enter, &state);
    paint(&mut view, &state, 80, 24);
    view.key(KeyCode::Enter, &state); // Cancel; the old paint still had two rows.
    for _ in 0..5 {
        view.key(KeyCode::Down, &state);
    }
    assert!(matches!(view.key(KeyCode::Enter, &state), Action::None));
    paint(&mut view, &state, 80, 24);
    assert!(matches!(view.key(KeyCode::Enter, &state), Action::Refresh));
}

#[test]
fn explicit_clear_emits_only_displayed_scope_and_token() {
    let mut view = QueueView::default();
    let state = active();
    view.selected = 3;
    paint(&mut view, &state, 80, 24);
    view.key(KeyCode::Enter, &state);
    paint(&mut view, &state, 80, 24);
    assert!(view.select_mouse(1, &state));
    assert!(
        matches!(view.key(KeyCode::Enter, &state), Action::Mutate(Operation::Clear(Some(QueueKind::Steering)), token) if token == "snapshot")
    );
}

#[test]
fn queue_view_is_safe_and_navigable_at_supported_sizes() {
    let mut state = active();
    crate::reducer::reduce(
        &mut state,
        crate::reducer::UiAction::BackendEvent(crate::reducer::BackendEvent::QueueUpdated {
            steering: vec!["你好\n\u{1b}[2J\u{202e}payload".into()],
            follow_up: vec![],
        }),
        &mut crate::SequentialCommandIds::default(),
    )
    .unwrap();
    for (width, height) in [(30, 8), (80, 24), (160, 40)] {
        let mut view = QueueView::default();
        let text = paint(&mut view, &state, width, height);
        assert!(text.contains("Steering"));
        view.key(KeyCode::End, &state);
        let text = paint(&mut view, &state, width, height);
        assert!(!text.contains('\u{1b}'));
        assert!(!text.contains('\u{202e}'));
        assert!(matches!(view.key(KeyCode::Esc, &state), Action::Close));
    }
}

#[test]
fn run_cancellation_invalidates_a_painted_confirmation() {
    let mut view = QueueView::default();
    let mut state = active();
    view.selected = 4;
    paint(&mut view, &state, 80, 24);
    view.key(KeyCode::Enter, &state);
    paint(&mut view, &state, 80, 24);
    state.cancel_requested = true;
    paint(&mut view, &state, 80, 24);
    assert!(view.confirmation.is_none());
}

#[test]
fn read_only_and_pending_states_cannot_offer_mutation() {
    for condition in ["idle", "pending", "missing"] {
        let mut state = active();
        match condition {
            "idle" => state.current_command = None,
            "pending" => state.queue.management.pending = Some(("clear".into(), "clear_queue")),
            _ => state.queue.management.token = None,
        }
        let mut view = QueueView {
            selected: 1,
            ..QueueView::default()
        };
        paint(&mut view, &state, 80, 24);
        assert!(matches!(view.key(KeyCode::Enter, &state), Action::None));
    }
}
