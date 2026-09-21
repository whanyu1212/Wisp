use super::*;
use serde_json::json;

#[derive(Default)]
struct Ids(usize);
impl CommandIdSource for Ids {
    fn next_id(&mut self, kind: CommandKind) -> String {
        self.0 += 1;
        format!("{}-{}", kind.prefix(), self.0)
    }
}

fn active() -> UiState {
    let mut state = UiState::unconfigured();
    state.view_status = ViewStatus::Running;
    state.interaction_status = InteractionStatus::Running;
    state.current_command = Some(ActiveCommand {
        id: "prompt".into(),
        command_type: ActiveCommandType::Prompt,
    });
    apply_snapshot(&mut state, snapshot("initial", None));
    state
}

fn snapshot(token: &str, command: Option<&str>) -> Snapshot {
    Snapshot {
        steering: vec!["first".into(), "newest".into()],
        follow_up: vec!["later".into()],
        steering_mode: QueueMode::OneAtATime,
        follow_up_mode: QueueMode::All,
        token: Some(token.into()),
        command_id: command.map(str::to_owned),
    }
}

#[test]
fn guarded_mode_and_clear_wait_for_authoritative_snapshots() {
    for operation in [
        Operation::Mode(QueueKind::Steering, QueueMode::All),
        Operation::Clear(Some(QueueKind::FollowUp)),
        Operation::Clear(None),
    ] {
        let mut state = active();
        let before = state.queue.clone();
        let mut ids = Ids::default();
        let effects = request(&mut state, operation, "initial".into(), &mut ids).unwrap();
        let UiEffect::SendCommand(command) = &effects[0] else {
            panic!("expected command")
        };
        let wire = serde_json::to_value(command).unwrap();
        assert_eq!(wire["expected_token"], "initial");
        assert_eq!(state.queue.steering, before.steering);
        assert_eq!(state.queue.follow_up, before.follow_up);
        assert_eq!(state.queue.management.steering_mode, QueueMode::OneAtATime);
        let again = request(&mut state, operation, "initial".into(), &mut ids).unwrap();
        assert!(
            !again
                .iter()
                .any(|effect| matches!(effect, UiEffect::SendCommand(_)))
        );
        let mut updated = snapshot("next", wire["id"].as_str());
        updated.steering_mode = QueueMode::All;
        updated.follow_up.clear();
        apply_snapshot(&mut state, updated);
        assert_eq!(state.queue.management.token.as_deref(), Some("next"));
        let finished = BackendEvent::CommandFinished {
            command_id: wire["id"].as_str().unwrap().into(),
            command_type: wire["type"].as_str().unwrap().into(),
            ok: true,
            error: None,
        };
        finish(&mut state, &finished, &mut ids).unwrap();
        assert!(!state.queue_management_pending());
    }
}

#[test]
fn stale_tokens_and_idle_or_cancelled_runs_never_send_mutations() {
    for condition in ["token", "idle", "cancel"] {
        let mut state = active();
        if condition == "token" {
            state.queue.management.token = Some("changed".into());
        }
        if condition == "idle" {
            state.current_command = None;
        }
        if condition == "cancel" {
            state.cancel_requested = true;
        }
        let effects = request(
            &mut state,
            Operation::Clear(None),
            "initial".into(),
            &mut Ids::default(),
        )
        .unwrap();
        assert!(
            !effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::SendCommand(_)))
        );
        assert_eq!(state.queued_steering(), 2);
    }
}

#[test]
fn old_session_responses_cannot_replace_new_queue_state() {
    let mut state = active();
    let mut ids = Ids::default();
    queue_state_effect(&mut state, &mut ids).unwrap();
    let old_id = state.queue.management.refresh.clone().unwrap();
    clear_queue_cache(&mut state);
    queue_state_effect(&mut state, &mut ids).unwrap();
    apply_snapshot(&mut state, snapshot("old", Some(&old_id)));
    assert!(state.queue.steering.is_empty());
    let new_id = state.queue.management.refresh.clone().unwrap();
    apply_snapshot(&mut state, snapshot("new", Some(&new_id)));
    assert_eq!(state.queue.management.token.as_deref(), Some("new"));
    apply_snapshot(&mut state, snapshot("duplicate-old", Some(&old_id)));
    assert_eq!(state.queue.management.token.as_deref(), Some("new"));
}

#[test]
fn failed_mutation_retains_contents_and_refreshes_once() {
    let mut state = active();
    let mut ids = Ids::default();
    request(
        &mut state,
        Operation::Clear(None),
        "initial".into(),
        &mut ids,
    )
    .unwrap();
    let event = BackendEvent::CommandFinished {
        command_id: "clear_queue-1".into(),
        command_type: "clear_queue".into(),
        ok: false,
        error: Some("Queue changed".into()),
    };
    let effects = finish(&mut state, &event, &mut ids).unwrap().unwrap();
    assert!(
        effects
            .iter()
            .any(|effect| matches!(effect, UiEffect::SendCommand(_)))
    );
    assert_eq!(state.queued_steering(), 2);
    assert_eq!(
        state.queue.management.error.as_deref(),
        Some("Queue changed")
    );
    assert!(finish(&mut state, &event, &mut ids).unwrap().is_none());
}

#[test]
fn projection_preserves_modes_and_guard_identity() {
    let event = BackendEvent::from_projection_value(&json!({
        "type": "queue.updated", "steering": [], "follow_up": ["later"],
        "steering_mode": "all", "follow_up_mode": "one_at_a_time", "token": "token", "command_id": "read"
    })).unwrap();
    let BackendEvent::QueueSnapshot(snapshot) = event else {
        panic!("expected snapshot")
    };
    assert_eq!(snapshot.steering_mode, QueueMode::All);
    assert_eq!(snapshot.follow_up_mode, QueueMode::OneAtATime);
    assert_eq!(snapshot.token.as_deref(), Some("token"));
    assert_eq!(snapshot.command_id.as_deref(), Some("read"));
}
