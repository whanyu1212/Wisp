//! Guarded queue mutations and correlated snapshots, independent of presentation.

#[cfg(test)]
mod tests;

use super::*;
use wisp_protocol::commands::QueueMode;

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Management {
    pub token: Option<String>,
    pub steering_mode: QueueMode,
    pub follow_up_mode: QueueMode,
    pub refresh: Option<String>,
    pub pending: Option<(String, &'static str)>,
    pub error: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Snapshot {
    pub steering: Vec<String>,
    pub follow_up: Vec<String>,
    pub steering_mode: QueueMode,
    pub follow_up_mode: QueueMode,
    pub token: Option<String>,
    pub command_id: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Operation {
    Mode(QueueKind, QueueMode),
    Restore(QueueKind),
    Clear(Option<QueueKind>),
}

pub(super) fn apply_snapshot(state: &mut UiState, snapshot: Snapshot) {
    if let Some(id) = snapshot.command_id.as_deref() {
        let known = state.queue.management.refresh.as_deref() == Some(id)
            || state
                .queue
                .management
                .pending
                .as_ref()
                .is_some_and(|(pending, _)| pending == id)
            || state
                .pending_queue_restore
                .as_ref()
                .is_some_and(|pending| pending.command_id == id)
            || state.pending_queue_submissions.contains_key(id);
        if !known {
            return;
        }
        if state.queue.management.refresh.as_deref() == Some(id) {
            state.queue.management.refresh = None;
        }
    }
    apply_queue_update(state, snapshot.steering, snapshot.follow_up);
    state.queue.management.token = snapshot.token;
    state.queue.management.steering_mode = snapshot.steering_mode;
    state.queue.management.follow_up_mode = snapshot.follow_up_mode;
}

pub(super) fn request(
    state: &mut UiState,
    operation: Operation,
    expected_token: String,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    let error = if !state.active_prompt_editable() {
        Some("Queue changes require an active, editable run.")
    } else if state.queue.management.pending.is_some() || state.pending_queue_restore.is_some() {
        Some("A queue operation is already pending.")
    } else if state.queue.management.token.as_deref() != Some(expected_token.as_str()) {
        Some("Queue changed; review the refreshed snapshot and retry.")
    } else {
        None
    };
    if let Some(error) = error {
        state.queue.management.error = Some(error.into());
        return Ok(vec![
            UiEffect::Notice(error.into()),
            UiEffect::RequestRender,
        ]);
    }
    state.queue.management.error = None;
    if let Operation::Restore(kind) = operation {
        return restore_queue_draft(state, kind, &expected_token, ids);
    }
    let (kind, command_type) = match operation {
        Operation::Mode(..) => (CommandKind::SetQueueMode, "set_queue_mode"),
        Operation::Clear(..) => (CommandKind::ClearQueue, "clear_queue"),
        Operation::Restore(..) => unreachable!(),
    };
    let id = ids.next_id(kind);
    let command = match operation {
        Operation::Mode(queue, mode) => {
            WispTypedClientRpcCommands::set_queue_mode(&id, queue, mode, &expected_token)?
        }
        Operation::Clear(queue) => {
            WispTypedClientRpcCommands::clear_queue(&id, queue, &expected_token)?
        }
        Operation::Restore(..) => unreachable!(),
    };
    state.queue.management.pending = Some((id, command_type));
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

pub(super) fn finish(
    state: &mut UiState,
    event: &BackendEvent,
    ids: &mut impl CommandIdSource,
) -> Result<Option<Vec<UiEffect>>, ProtocolDecodeError> {
    let BackendEvent::CommandFinished {
        command_id,
        command_type,
        ok,
        error,
    } = event
    else {
        return Ok(None);
    };
    let refreshing = state.queue.management.refresh.as_deref() == Some(command_id.as_str())
        && command_type == "get_queue_state";
    let mutating = state
        .queue
        .management
        .pending
        .as_ref()
        .is_some_and(|(id, kind)| id == command_id && kind == command_type);
    if !refreshing && !mutating {
        return Ok(None);
    }
    if refreshing {
        state.queue.management.refresh = None;
    } else {
        state.queue.management.pending = None;
    }
    let mut effects = vec![UiEffect::RequestRender];
    if !ok {
        let message = error
            .clone()
            .unwrap_or_else(|| "Queue operation failed.".into());
        state.queue.management.error = Some(message.clone());
        effects.push(UiEffect::Notice(message));
        if mutating {
            effects.push(queue_state_effect(state, ids)?);
        }
    }
    Ok(Some(effects))
}
