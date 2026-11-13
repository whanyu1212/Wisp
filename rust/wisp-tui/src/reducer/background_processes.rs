//! The session's running background processes, for the side panel and for the
//! notification when one stops while the terminal is in the background.
//!
//! Live events keep the list: a `bash` result that reads a process as running adds
//! it, and `process.ended` or a result with a final state removes it. Every
//! `get_state` reply replaces it with the backend's own list, and one is asked for
//! whenever the transcript is rebuilt, since history replays no process events.

use std::collections::VecDeque;

use super::*;
use crate::tool_cards::{
    ProcessDisplayState, ToolCallInput, ToolResultInput, identity_for_display,
};

/// More processes than the backend runs at once; the rest are dropped.
const MAX_LISTED: usize = 32;
/// Ended processes remembered, so a result read before an exit cannot list it again.
const MAX_REMEMBERED_ENDS: usize = 64;
/// `start` calls whose result has not arrived yet, remembered for their command.
const MAX_PENDING_STARTS: usize = 16;
/// Characters of a command kept for a notification.
const NOTICE_COMMAND_MAX_CHARS: usize = 80;

/// A process started with `bash operation=start` that has not stopped yet.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BackgroundProcess {
    pub process_id: String,
    /// The command as started, unsanitized; views sanitize it when they draw it.
    pub command: String,
    /// When it started, in Unix milliseconds, if the backend said.
    pub started_at_ms: Option<i64>,
}

/// The running list, and the exits seen so far for the notification.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct BackgroundProcesses {
    running: Vec<BackgroundProcess>,
    /// Commands of `start` calls by call id, until their result names the process.
    starts: VecDeque<(String, String)>,
    ended: VecDeque<String>,
    exits: u64,
    last_exit: Option<String>,
    read: Option<String>,
}

impl BackgroundProcesses {
    /// The processes still running, oldest first.
    pub fn running(&self) -> &[BackgroundProcess] {
        &self.running
    }

    /// How many processes have stopped on their own, and what the latest one says.
    pub fn exits(&self) -> (u64, Option<&str>) {
        (self.exits, self.last_exit.as_deref())
    }

    fn add(&mut self, process: BackgroundProcess) {
        if self.ended.contains(&process.process_id) {
            return;
        }
        if let Some(listed) = self
            .running
            .iter_mut()
            .find(|listed| listed.process_id == process.process_id)
        {
            listed.started_at_ms = listed.started_at_ms.or(process.started_at_ms);
            return;
        }
        if self.running.len() < MAX_LISTED {
            self.running.push(process);
        }
    }

    fn remove(&mut self, process_id: &str) {
        self.running
            .retain(|listed| listed.process_id != process_id);
        if !self.ended.iter().any(|ended| ended == process_id) {
            if self.ended.len() == MAX_REMEMBERED_ENDS {
                self.ended.pop_front();
            }
            self.ended.push_back(process_id.to_owned());
        }
    }

    fn replace(&mut self, processes: &[BackgroundProcess]) {
        self.running.clear();
        for process in processes {
            self.add(process.clone());
        }
    }
}

/// Remembers the command of a live `bash` start, for the result that names its process.
pub(super) fn observe_tool_call(state: &mut UiState, call: &ToolCallInput) {
    if call.name != "bash" || call.arguments["operation"] != "start" {
        return;
    }
    let Some(command) = call.arguments["command"].as_str() else {
        return;
    };
    let starts = &mut state.background_processes.starts;
    if starts.len() == MAX_PENDING_STARTS {
        starts.pop_front();
    }
    starts.push_back((call.call_id.clone(), command.to_owned()));
}

/// Lists or drops the process a live `bash` result reports on.
pub(super) fn observe_tool_result(state: &mut UiState, result: &ToolResultInput) {
    // Results carry identities in the transcript's bounded form; the backend's own
    // events and `get_state` name processes as it does.
    let Some(process_id) = result.process_id.as_deref().map(identity_for_display) else {
        return;
    };
    let processes = &mut state.background_processes;
    let start = processes
        .starts
        .iter()
        .position(|(call_id, _)| *call_id == result.call_id)
        .and_then(|index| processes.starts.remove(index));
    match result.process_state.as_deref() {
        Some("running") => {
            let command = start.map_or_else(|| process_id.to_owned(), |(_, command)| command);
            processes.add(BackgroundProcess {
                process_id: process_id.to_owned(),
                command,
                started_at_ms: result.process_started_at_ms,
            });
        }
        Some(_) => processes.remove(process_id),
        None => {}
    }
}

/// Drops a process that stopped on its own and counts its exit for the notification.
pub(super) fn observe_process_ended(
    state: &mut UiState,
    process_id: &str,
    command: &str,
    ended: ProcessDisplayState,
    exit_code: Option<i32>,
) {
    let processes = &mut state.background_processes;
    processes.remove(process_id);
    processes.exits = processes.exits.saturating_add(1);
    processes.last_exit = Some(exit_notice(command, ended, exit_code));
}

/// Empties the list and asks the backend for it again, after the transcript was
/// rebuilt for another session or another point in this one.
pub(super) fn reload(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    state.background_processes.running.clear();
    if state.background_processes.read.is_some() || state.exit_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetState);
    let command = WispTypedClientRpcCommands::get_state(&id)?;
    state.background_processes.read = Some(id);
    Ok(vec![UiEffect::SendCommand(command)])
}

/// Applies the list from any `get_state` reply, and retires this module's own read.
///
/// A reply is not consumed, since another reader may have asked for it; only the
/// read's `command.finished` is.
pub(super) fn observe(state: &mut UiState, event: &BackendEvent) -> Option<Vec<UiEffect>> {
    match event {
        BackendEvent::StateReported {
            background_processes,
            ..
        } => {
            state.background_processes.replace(background_processes);
            None
        }
        BackendEvent::CommandFinished { command_id, .. }
            if state.background_processes.read.as_deref() == Some(command_id.as_str()) =>
        {
            state.background_processes.read = None;
            Some(vec![UiEffect::RequestRender])
        }
        _ => None,
    }
}

fn exit_notice(command: &str, ended: ProcessDisplayState, exit_code: Option<i32>) -> String {
    let outcome = match (ended, exit_code) {
        (ProcessDisplayState::Completed, _) => "finished".to_owned(),
        (ProcessDisplayState::TimedOut, _) => "timed out".to_owned(),
        (_, Some(code)) => format!("failed (exit {code})"),
        _ => "failed".to_owned(),
    };
    let command = command.split_whitespace().collect::<Vec<_>>().join(" ");
    let command = if command.chars().count() > NOTICE_COMMAND_MAX_CHARS {
        let kept: String = command.chars().take(NOTICE_COMMAND_MAX_CHARS - 1).collect();
        format!("{kept}…")
    } else {
        command
    };
    format!("Background process {outcome}: {command}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct Ids(usize);

    impl CommandIdSource for Ids {
        fn next_id(&mut self, kind: CommandKind) -> String {
            self.0 += 1;
            format!("{}-{}", kind.prefix(), self.0)
        }
    }

    fn apply(state: &mut UiState, value: serde_json::Value) -> Vec<UiEffect> {
        let event = BackendEvent::from_projection_value(&value).unwrap();
        reduce(state, UiAction::BackendEvent(event), &mut Ids::default()).unwrap()
    }

    fn start(state: &mut UiState, process_id: &str, command: &str) {
        apply(
            state,
            serde_json::json!({
                "type": "tool.call", "call_id": process_id, "name": "bash",
                "arguments": {"operation": "start", "command": command}
            }),
        );
        result(state, process_id, process_id, "running");
    }

    fn result(state: &mut UiState, call_id: &str, process_id: &str, process_state: &str) {
        apply(
            state,
            serde_json::json!({
                "type": "tool.result", "call_id": call_id, "name": "bash",
                "output": "", "is_error": false, "process_id": process_id,
                "process_state": process_state, "process_started_at_ms": 1_700_000_000_000_i64
            }),
        );
    }

    fn ended(state: &mut UiState, process_id: &str, exit_code: i32) {
        apply(
            state,
            serde_json::json!({
                "type": "process.ended", "timestamp": "2026-11-13T09:00:00Z",
                "process_id": process_id, "command": "make test", "state": "completed",
                "exit_code": exit_code, "duration_ms": 1000, "notify": false
            }),
        );
    }

    fn listed(state: &UiState) -> Vec<(&str, &str)> {
        state
            .background_processes
            .running()
            .iter()
            .map(|process| (process.process_id.as_str(), process.command.as_str()))
            .collect()
    }

    fn state_reply(processes: serde_json::Value) -> serde_json::Value {
        serde_json::json!({
            "type": "rpc.state", "command_id": "get_state-1",
            "timestamp": "2026-11-13T09:00:00Z",
            "state": {
                "provider": "fake", "auto_compaction_enabled": true,
                "steering_mode": "one_at_a_time", "follow_up_mode": "one_at_a_time",
                "pending_steering_count": 0, "pending_follow_up_count": 0,
                "mode": "build", "background_processes": processes
            }
        })
    }

    #[test]
    fn exit_or_cancel_unlists() {
        let mut state = UiState::new("fake".into(), None, None);
        start(&mut state, "server", "npm run dev");
        start(&mut state, "tests", "make test");
        assert_eq!(
            listed(&state),
            [("server", "npm run dev"), ("tests", "make test")]
        );

        ended(&mut state, "tests", 2);
        result(&mut state, "cancel", "server", "cancelled");

        assert!(listed(&state).is_empty());
        assert_eq!(
            state.background_processes.exits(),
            (1, Some("Background process failed (exit 2): make test"))
        );
    }

    #[test]
    fn late_running_result_ignored() {
        let mut state = UiState::new("fake".into(), None, None);
        start(&mut state, "tests", "make test");
        ended(&mut state, "tests", 0);

        result(&mut state, "poll", "tests", "running");

        assert!(listed(&state).is_empty());
    }

    #[test]
    fn state_reply_replaces_list() {
        let mut state = UiState::new("fake".into(), None, None);
        start(&mut state, "gone", "sleep 1");
        ended(&mut state, "gone", 0);
        start(&mut state, "stale", "sleep 2");

        apply(
            &mut state,
            state_reply(serde_json::json!([
                {"process_id": "server", "command": "npm run dev", "started_at_ms": 5},
                {"process_id": "gone", "command": "sleep 1", "started_at_ms": 5}
            ])),
        );

        assert_eq!(listed(&state), [("server", "npm run dev")]);
        assert_eq!(
            state.background_processes.running()[0].started_at_ms,
            Some(5)
        );
    }

    #[test]
    fn reload_clears_and_asks() {
        let mut state = UiState::new("fake".into(), None, None);
        start(&mut state, "server", "npm run dev");

        let effects = reload(&mut state, &mut Ids::default()).unwrap();

        assert!(listed(&state).is_empty());
        let [UiEffect::SendCommand(command)] = effects.as_slice() else {
            panic!("expected one command, got {effects:?}");
        };
        assert_eq!(command.to_value().unwrap()["type"], "get_state");
        assert!(reload(&mut state, &mut Ids::default()).unwrap().is_empty());
    }

    #[test]
    fn absent_list_reads_empty() {
        let mut reply = state_reply(serde_json::Value::Null);
        reply["state"]
            .as_object_mut()
            .unwrap()
            .remove("background_processes");
        let event = BackendEvent::from_projection_value(&reply).unwrap();
        assert!(matches!(
            event,
            BackendEvent::StateReported { background_processes, .. } if background_processes.is_empty()
        ));
    }
}
