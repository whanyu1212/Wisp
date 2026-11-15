//! The session's running background processes, for the side panel and for the
//! notification when one stops while the terminal is in the background.
//!
//! Live events keep the list: a `bash` result that reads a process as running adds
//! it, and `process.ended` or a result with a final state removes it. Every
//! `get_state` reply replaces it with the backend's own list, and one is asked for
//! whenever the transcript is rebuilt, since history replays no process events.
//! Once that list arrives, a history card still reading running for a process it
//! leaves out is closed, and so is any such card a later history page brings.
//!
//! Processes that ended stay listed with their outcome for as long as the backend
//! holds them: `process.ended` adds one, and each `get_processes` reply replaces
//! them with the backend's own list. The side panel's peek reads one process's
//! latest output with `get_process_output`, once when it opens and then on each poll
//! tick while the process runs; reading drains nothing the agent polls.

use std::collections::{HashSet, VecDeque};
use std::sync::Arc;

use super::*;
use crate::tool_cards::{
    ProcessDisplayState, ToolCallInput, ToolResultInput, bounded_identity, identity_for_display,
};

/// More processes than the backend runs at once; the rest are dropped.
const MAX_LISTED: usize = 32;
/// Ended processes remembered, so a result read before an exit cannot list it again.
const MAX_REMEMBERED_ENDS: usize = 64;
/// The longest call id `background_tool_call` accepts.
const MAX_CALL_ID_BYTES: usize = 256;
/// `start` calls whose result has not arrived yet, remembered for their command.
const MAX_PENDING_STARTS: usize = 16;
/// Characters of a command kept for a notification.
const NOTICE_COMMAND_MAX_CHARS: usize = 80;
/// The prompt row of a run Wisp started because a background process stopped; the
/// prompt itself, the process notices, went to the model only.
pub const WAKE_PROMPT_LABEL: &str = "⟳ Background process stopped · Wisp woke the agent";

/// A process started with `bash operation=start` that has not stopped yet.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BackgroundProcess {
    pub process_id: String,
    /// The command as started, unsanitized; views sanitize it when they draw it.
    pub command: String,
    /// When it started, in Unix milliseconds, if the backend said.
    pub started_at_ms: Option<i64>,
}

/// A process as `get_processes` and `get_process_output` report it.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProcessReport {
    pub process_id: String,
    /// The command as started, unsanitized; views sanitize it when they draw it.
    pub command: String,
    /// `Running`, or the outcome it ended with.
    pub state: ProcessDisplayState,
    pub exit_code: Option<i32>,
    pub started_at_ms: Option<i64>,
    /// Whether the user stopped it, rather than the agent or a timeout.
    pub stopped_by_user: bool,
}

/// Which stream printed a run of output.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutputStream {
    Stdout,
    Stderr,
}

/// A process's latest output: both streams in the order they printed, unsanitized.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProcessOutput {
    pub process: ProcessReport,
    pub chunks: Vec<(OutputStream, String)>,
    /// Bytes printed before the first chunk that the backend no longer keeps.
    pub omitted_bytes: u64,
}

/// The process the side panel's peek shows, and what it last read of it.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Peek {
    pub process_id: String,
    /// The latest reply; `None` until the first one arrives.
    pub output: Option<Arc<ProcessOutput>>,
    /// Whether the backend no longer holds the process, so there is nothing to read.
    pub gone: bool,
    /// The read in flight, at most one at a time.
    read: Option<String>,
}

impl Peek {
    /// Whether another read could show something new: the process may still print.
    fn live(&self) -> bool {
        !self.gone
            && self
                .output
                .as_ref()
                .is_none_or(|output| output.process.state == ProcessDisplayState::Running)
    }
}

/// The running list, and the exits seen so far for the notification.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct BackgroundProcesses {
    running: Vec<BackgroundProcess>,
    /// `cancel_process` commands sent and not finished, so a failure is reported.
    stops: Vec<String>,
    /// Foreground `bash` calls running now, oldest first, which the user may move.
    foreground: Vec<String>,
    /// `background_tool_call` commands sent and not finished.
    moves: Vec<String>,
    /// Commands of `start` and foreground `bash` calls by call id, until their result
    /// names the process.
    starts: VecDeque<(String, String)>,
    ended: VecDeque<String>,
    exits: u64,
    last_exit: Option<String>,
    read: Option<String>,
    /// Whether `running` holds the backend's list since the transcript was last
    /// rebuilt, so a card it leaves out can be closed.
    listed: bool,
    /// Processes that ended and the backend still holds, in the order it lists them.
    finished: Vec<ProcessReport>,
    /// The `get_processes` read in flight.
    list_read: Option<String>,
    peek: Option<Peek>,
}

impl BackgroundProcesses {
    /// The processes still running, oldest first.
    pub fn running(&self) -> &[BackgroundProcess] {
        &self.running
    }

    /// Processes that ended and the backend still holds, with their outcome.
    pub fn finished(&self) -> &[ProcessReport] {
        &self.finished
    }

    /// What the side panel's peek shows, while one is open.
    pub fn peek(&self) -> Option<&Peek> {
        self.peek.as_ref()
    }

    /// Whether the open peek wants another read on the next poll tick.
    pub fn peek_polling(&self) -> bool {
        self.peek.as_ref().is_some_and(Peek::live)
    }

    /// Whether a foreground shell command is running that the user may move.
    pub fn foreground_running(&self) -> bool {
        !self.foreground.is_empty()
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

    /// Lists a process that ended with its outcome. One already listed keeps its
    /// place, so the side panel's rows do not move under its focus.
    fn finish(&mut self, report: ProcessReport) {
        self.remove(&report.process_id);
        if let Some(listed) = self
            .finished
            .iter_mut()
            .find(|listed| listed.process_id == report.process_id)
        {
            *listed = report;
            return;
        }
        if self.finished.len() == MAX_LISTED {
            self.finished.remove(0);
        }
        self.finished.push(report);
    }
}

/// Remembers the command of a live `bash` start or foreground run, for the result
/// that names its process; a foreground run can be moved until its result arrives.
pub(super) fn observe_tool_call(state: &mut UiState, call: &ToolCallInput) {
    if call.name != "bash" {
        return;
    }
    let operation = call.arguments["operation"].as_str().unwrap_or("run");
    if operation != "start" && operation != "run" {
        return;
    }
    let Some(command) = call.arguments["command"].as_str() else {
        return;
    };
    // A batch runs its calls in order, so the oldest is the one running: past the
    // bound, later calls go untracked rather than pushing it out.
    let foreground = &mut state.background_processes.foreground;
    if operation == "run" && foreground.len() < MAX_PENDING_STARTS {
        foreground.push(call.call_id.clone());
    }
    let starts = &mut state.background_processes.starts;
    if starts.len() == MAX_PENDING_STARTS {
        starts.pop_front();
    }
    starts.push_back((call.call_id.clone(), command.to_owned()));
}

/// Lists or drops the process a live `bash` result reports on.
pub(super) fn observe_tool_result(state: &mut UiState, result: &ToolResultInput) {
    let processes = &mut state.background_processes;
    processes
        .foreground
        .retain(|call_id| *call_id != result.call_id);
    // Every result retires its call's remembered command, whether or not it names a
    // process, so a later call cannot pick up an earlier one's label.
    let start = processes
        .starts
        .iter()
        .position(|(call_id, _)| *call_id == result.call_id)
        .and_then(|index| processes.starts.remove(index));
    // Results carry identities in the transcript's bounded form; the backend's own
    // events and `get_state` name processes as it does.
    let Some(process_id) = result.process_id.as_deref().map(identity_for_display) else {
        return;
    };
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

/// Drops a process that stopped and, unless the user stopped it, counts its exit for
/// the notification.
pub(super) fn observe_process_ended(
    state: &mut UiState,
    process_id: &str,
    command: &str,
    ended: ProcessDisplayState,
    exit_code: Option<i32>,
) {
    let processes = &mut state.background_processes;
    let started_at_ms = processes
        .running
        .iter()
        .find(|process| process.process_id == process_id)
        .and_then(|process| process.started_at_ms);
    processes.finish(ProcessReport {
        process_id: process_id.to_owned(),
        command: command.to_owned(),
        state: ended,
        exit_code,
        started_at_ms,
        // The backend announces a cancelled process only when the user stopped it.
        stopped_by_user: ended == ProcessDisplayState::Cancelled,
    });
    if ended == ProcessDisplayState::Cancelled {
        return;
    }
    processes.exits = processes.exits.saturating_add(1);
    processes.last_exit = Some(exit_notice(command, ended, exit_code));
}

/// Empties the list and asks the backend for it again, the running processes and
/// those that ended, after the transcript was rebuilt for another session or another
/// point in this one.
pub(super) fn reload(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let processes = &mut state.background_processes;
    processes.running.clear();
    processes.foreground.clear();
    processes.listed = false;
    // Another session's processes are not listed. A list read already sent answers
    // for the session that was left, so its reply, matching no read, is dropped.
    processes.finished.clear();
    processes.peek = None;
    processes.list_read = None;
    let mut effects = Vec::new();
    if state.background_processes.read.is_none() && !state.exit_requested {
        let id = ids.next_id(CommandKind::GetState);
        effects.push(UiEffect::SendCommand(
            WispTypedClientRpcCommands::get_state(&id)?,
        ));
        state.background_processes.read = Some(id);
    }
    effects.extend(load(state, ids)?);
    Ok(effects)
}

/// Closes the process cards still reading running whose process the backend no
/// longer runs, once its list has arrived since the transcript was rebuilt.
///
/// Live events keep the list current after that, so this also serves history
/// pages projected later.
pub(super) fn close_stopped_cards(state: &mut UiState) {
    let processes = &state.background_processes;
    if !processes.listed {
        return;
    }
    // A card names its process as the call or result did: raw, or as an identity.
    let running: HashSet<String> = processes
        .running
        .iter()
        .flat_map(|process| {
            [
                process.process_id.clone(),
                bounded_identity(&process.process_id),
            ]
        })
        .collect();
    state.transcript.close_stopped_process_cards(|process_id| {
        running.contains(process_id) || running.contains(identity_for_display(process_id))
    });
}

/// The move-to-background key: asks the backend to move the running foreground
/// `bash` call, which then ends with the id of the process it became.
///
/// A batch announces all its calls before running any, and runs `bash` calls one
/// at a time in order, so the oldest call without a result is the one running.
pub(super) fn move_running(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let Some(call_id) = state.background_processes.foreground.first().cloned() else {
        return Ok(vec![
            UiEffect::Notice("No shell command is running in the foreground.".into()),
            UiEffect::RequestRender,
        ]);
    };
    // The transcript's call ids are in bounded form; the backend knows the raw one.
    let raw_call_id = identity_for_display(&call_id).to_owned();
    if raw_call_id.len() > MAX_CALL_ID_BYTES || raw_call_id.starts_with("h:") {
        return Ok(vec![
            UiEffect::Notice("This command's call id is too long to move it.".into()),
            UiEffect::RequestRender,
        ]);
    }
    let id = ids.next_id(CommandKind::BackgroundToolCall);
    let command = WispTypedClientRpcCommands::background_tool_call(&id, &raw_call_id)?;
    let moves = &mut state.background_processes.moves;
    if moves.len() == MAX_LISTED {
        moves.remove(0);
    }
    moves.push(id);
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::Notice("Moving the command to the background…".into()),
        UiEffect::RequestRender,
    ])
}

/// `/kill`: stops the running process with this number from `/ps`, or this id.
pub(super) fn stop(
    state: &mut UiState,
    target: &str,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let target = target.trim();
    let running = state.background_processes.running();
    let process = match target.parse::<usize>() {
        Ok(number) => number.checked_sub(1).and_then(|index| running.get(index)),
        Err(_) => running.iter().find(|process| process.process_id == target),
    };
    let Some(process) = process else {
        let notice = format!("No running background process {target}. /ps shows them.");
        return Ok(vec![
            UiEffect::Notice(bounded_session_text(&notice, SESSION_NOTICE_MAX_BYTES)),
            UiEffect::RequestRender,
        ]);
    };
    let notice = format!("Stopping {}", command_line(&process.command));
    let id = ids.next_id(CommandKind::CancelProcess);
    let command = WispTypedClientRpcCommands::cancel_process(&id, &process.process_id)?;
    let stops = &mut state.background_processes.stops;
    if stops.len() == MAX_LISTED {
        stops.remove(0);
    }
    stops.push(id);
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::Notice(bounded_session_text(&notice, SESSION_NOTICE_MAX_BYTES)),
        UiEffect::RequestRender,
    ])
}

/// Asks the backend for the session's processes, running or ended, unless a read
/// is already in flight.
pub(super) fn load(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let processes = &mut state.background_processes;
    if processes.list_read.is_some() || state.exit_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetProcesses);
    let command = WispTypedClientRpcCommands::get_processes(&id)?;
    processes.list_read = Some(id);
    Ok(vec![UiEffect::SendCommand(command)])
}

/// Opens the side panel's peek on a process and reads its output at once, or closes
/// the peek. Opening the one already open changes nothing.
pub(super) fn set_peek(
    state: &mut UiState,
    process_id: Option<String>,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let processes = &mut state.background_processes;
    if processes.peek.as_ref().map(|peek| &peek.process_id) == process_id.as_ref() {
        return Ok(Vec::new());
    }
    // A read still in flight for the peek it replaces matches no read when it
    // answers, so it is dropped.
    processes.peek = None;
    let Some(process_id) = process_id else {
        return Ok(vec![UiEffect::RequestRender]);
    };
    processes.peek = Some(Peek {
        process_id,
        output: None,
        gone: false,
        read: None,
    });
    let mut effects = poll_peek(state, ids)?;
    effects.push(UiEffect::RequestRender);
    Ok(effects)
}

/// Reads the peeked process's output again, unless a read is in flight or the
/// process can print nothing more.
pub(super) fn poll_peek(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.exit_requested {
        return Ok(Vec::new());
    }
    let Some(peek) = state
        .background_processes
        .peek
        .as_mut()
        .filter(|peek| peek.read.is_none() && peek.live())
    else {
        return Ok(Vec::new());
    };
    let id = ids.next_id(CommandKind::GetProcessOutput);
    let command = WispTypedClientRpcCommands::get_process_output(&id, &peek.process_id)?;
    peek.read = Some(id);
    Ok(vec![UiEffect::SendCommand(command)])
}

/// A command's first line, made safe to show, cut to a notice's width.
pub(crate) fn command_line(command: &str) -> String {
    let line = crate::ui::sanitize_for_terminal(command.lines().next().unwrap_or(""));
    let line = line.trim();
    if line.chars().count() > NOTICE_COMMAND_MAX_CHARS {
        let kept: String = line.chars().take(NOTICE_COMMAND_MAX_CHARS - 1).collect();
        format!("{kept}…")
    } else {
        line.to_owned()
    }
}

/// Applies the list from any `get_state` reply, and retires this module's own read;
/// before that, applies and consumes the replies to its process list and output
/// reads.
///
/// A `get_state` reply is not consumed, since another reader may have asked for it;
/// only the read's `command.finished` is.
pub(super) fn observe(state: &mut UiState, event: &BackendEvent) -> Option<Vec<UiEffect>> {
    if let Some(effects) = observe_reads(&mut state.background_processes, event) {
        return Some(effects);
    }
    match event {
        BackendEvent::StateReported {
            command_id,
            background_processes,
            ..
        } => {
            state.background_processes.replace(background_processes);
            if state.background_processes.read.as_deref() == Some(command_id.as_str()) {
                state.background_processes.listed = true;
                close_stopped_cards(state);
            }
            None
        }
        BackendEvent::CommandFinished { command_id, .. }
            if state.background_processes.read.as_deref() == Some(command_id.as_str()) =>
        {
            state.background_processes.read = None;
            Some(vec![UiEffect::RequestRender])
        }
        BackendEvent::CommandFinished {
            command_id,
            ok,
            error,
            ..
        } if state.background_processes.stops.contains(command_id)
            || state.background_processes.moves.contains(command_id) =>
        {
            let processes = &mut state.background_processes;
            processes.stops.retain(|stop| stop != command_id);
            processes.moves.retain(|stop| stop != command_id);
            // A stop that worked shows through `process.ended`.
            Some(if *ok {
                vec![UiEffect::RequestRender]
            } else {
                let error = error
                    .as_deref()
                    .unwrap_or("The process could not be stopped.");
                vec![
                    UiEffect::Notice(bounded_session_text(error, SESSION_NOTICE_MAX_BYTES)),
                    UiEffect::RequestRender,
                ]
            })
        }
        _ => None,
    }
}

/// Applies the replies to this module's `get_processes` and `get_process_output`
/// reads, and consumes them. A reply to a read that no longer applies matches
/// nothing here and goes on unused, as does its `command.finished`.
fn observe_reads(
    processes: &mut BackgroundProcesses,
    event: &BackendEvent,
) -> Option<Vec<UiEffect>> {
    match event {
        BackendEvent::ProcessesReported {
            command_id,
            processes: reported,
        } if processes.list_read.as_deref() == Some(command_id.as_str()) => {
            let previous = std::mem::take(&mut processes.finished);
            for report in reported {
                // A list read before an exit arrived keeps the outcome already seen.
                let ended = previous
                    .iter()
                    .find(|ended| ended.process_id == report.process_id);
                if let (ProcessDisplayState::Running, Some(ended)) = (report.state, ended) {
                    processes.finish(ended.clone());
                } else if report.state == ProcessDisplayState::Running {
                    processes.add(BackgroundProcess {
                        process_id: report.process_id.clone(),
                        command: report.command.clone(),
                        started_at_ms: report.started_at_ms,
                    });
                } else {
                    processes.finish(report.clone());
                }
            }
            Some(vec![UiEffect::RequestRender])
        }
        BackendEvent::ProcessOutputReported { command_id, output } => {
            let peek = processes.peek.as_mut().filter(|peek| {
                peek.read.as_deref() == Some(command_id.as_str())
                    && peek.process_id == output.process.process_id
            });
            let peek = peek?;
            peek.output = Some(Arc::clone(output));
            if output.process.state != ProcessDisplayState::Running {
                processes.finish(output.process.clone());
            }
            Some(vec![UiEffect::RequestRender])
        }
        BackendEvent::CommandFinished { command_id, ok, .. } => {
            if processes.list_read.as_deref() == Some(command_id.as_str()) {
                processes.list_read = None;
                return Some(vec![UiEffect::RequestRender]);
            }
            if let Some(peek) = processes
                .peek
                .as_mut()
                .filter(|peek| peek.read.as_deref() == Some(command_id.as_str()))
            {
                peek.read = None;
                // The backend no longer holds the process: a later session dropped it
                // from the list, or a new process took its slot.
                if !ok {
                    peek.gone = true;
                    let process_id = peek.process_id.clone();
                    processes
                        .finished
                        .retain(|listed| listed.process_id != process_id);
                }
                return Some(vec![UiEffect::RequestRender]);
            }
            None
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

    fn notices(effects: &[UiEffect]) -> Vec<&str> {
        effects
            .iter()
            .filter_map(|effect| match effect {
                UiEffect::Notice(text) => Some(text.as_str()),
                _ => None,
            })
            .collect()
    }

    fn sent(effects: &[UiEffect]) -> Vec<serde_json::Value> {
        effects
            .iter()
            .filter_map(|effect| match effect {
                UiEffect::SendCommand(command) => Some(command.to_value().unwrap()),
                _ => None,
            })
            .collect()
    }

    /// A history page whose `bash` polls each read a process as running.
    fn running_history(prefix: &str, process_ids: &[&str]) -> crate::transcript::SharedTranscript {
        let messages: Vec<serde_json::Value> = process_ids
            .iter()
            .flat_map(|process_id| {
                let call_id = format!("{prefix}-{process_id}");
                let entry = |role: &str, suffix: &str, content: String| {
                    serde_json::json!({
                        "entry_id": format!("{call_id}-{suffix}"), "role": role,
                        "content": content, "content_truncated": false, "tool_calls": [],
                        "is_error": null, "tool_name": null, "tool_call_id": null,
                        "tool_result": null,
                    })
                };
                let mut poll = entry("assistant", "call", String::new());
                poll["tool_calls"] = serde_json::json!([{
                    "call_id": call_id, "name": "bash",
                    "arguments": {"operation": "poll", "process_id": process_id}
                }]);
                // History reads the state from the result's first line.
                let mut polled = entry(
                    "tool",
                    "result",
                    format!("Process {process_id} is still running"),
                );
                polled["tool_call_id"] = serde_json::json!(call_id);
                polled["tool_name"] = serde_json::json!("bash");
                polled["tool_result"] = serde_json::json!({"status": "running"});
                [poll, polled]
            })
            .collect();
        crate::history::project_rpc_messages(&messages).unwrap()
    }

    fn card_states(state: &UiState) -> Vec<(&str, ProcessDisplayState)> {
        state
            .transcript
            .entries()
            .iter()
            .filter_map(|entry| entry.process_card())
            .map(|card| (identity_for_display(&card.process_id), card.display_state))
            .collect()
    }

    #[test]
    fn unlisted_history_cards_close() {
        use ProcessDisplayState::{Gone, Running};

        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = Ids::default();
        reload(&mut state, &mut ids).unwrap();
        state.transcript = running_history("old", &["alive", "dead"]);
        // Nothing is known before the backend's list arrives.
        close_stopped_cards(&mut state);
        assert_eq!(card_states(&state), [("alive", Running), ("dead", Running)]);

        apply(
            &mut state,
            state_reply(serde_json::json!([{
                "process_id": "alive", "command": "make dev",
                "started_at_ms": 1_700_000_000_000_i64, "notify": false
            }])),
        );
        assert_eq!(card_states(&state), [("alive", Running), ("dead", Gone)]);

        // A process started live is listed, so its card stays running; an older page
        // brought in afterwards is checked against the same list.
        start(&mut state, "fresh", "make watch");
        let older = running_history("older", &["fresh", "older-dead"]);
        assert!(state.transcript.prepend_history_page(&older));
        close_stopped_cards(&mut state);
        assert_eq!(
            card_states(&state),
            [
                ("fresh", Running),
                ("older-dead", Gone),
                ("alive", Running),
                ("dead", Gone)
            ]
        );
    }

    #[test]
    fn startup_history_cards_close() {
        // A session opened at launch asks for the list along with its history, and
        // the cards close whichever reply arrives first.
        for list_first in [false, true] {
            let mut state = UiState::new("fake".into(), None, None);
            let mut ids = Ids::default();
            let commands = sent(&reduce(&mut state, UiAction::StartupHydration, &mut ids).unwrap());
            assert_eq!(commands[0]["type"], "get_messages");
            let history_id = commands[0]["id"].as_str().unwrap().to_owned();
            let state_id = commands[1]["id"].as_str().unwrap().to_owned();
            assert_eq!(commands[1]["type"], "get_state");
            let mut reply = state_reply(serde_json::json!([]));
            reply["command_id"] = serde_json::json!(state_id);
            let history = BackendEvent::MessagesReported {
                command_id: history_id.clone(),
                messages: SessionMessages {
                    source_messages: Default::default(),
                    session: None,
                    active_leaf_id: None,
                    truncated: false,
                    next_before_entry_id: None,
                    next_after_entry_id: None,
                    durable_entry_ids: Vec::new(),
                    exact_tool_result: None,
                    transcript: running_history("old", &["dead"]),
                },
            };
            let finished = BackendEvent::CommandFinished {
                command_id: history_id,
                command_type: "get_messages".into(),
                ok: true,
                error: None,
            };
            if list_first {
                apply(&mut state, reply.clone());
            }
            for event in [history, finished] {
                reduce(&mut state, UiAction::BackendEvent(event), &mut ids).unwrap();
            }
            if !list_first {
                assert_eq!(
                    card_states(&state),
                    [("dead", ProcessDisplayState::Running)]
                );
                apply(&mut state, reply);
            }
            assert_eq!(card_states(&state), [("dead", ProcessDisplayState::Gone)]);
        }
    }

    #[test]
    fn history_card_takes_exit() {
        // Reopened while its process runs, the session's card stays running until the
        // process ends, and then that same card says how.
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = Ids::default();
        reload(&mut state, &mut ids).unwrap();
        state.transcript = running_history("old", &["alive"]);
        apply(
            &mut state,
            state_reply(serde_json::json!([{
                "process_id": "alive", "command": "make test",
                "started_at_ms": 1_700_000_000_000_i64, "notify": false
            }])),
        );
        assert_eq!(
            card_states(&state),
            [("alive", ProcessDisplayState::Running)]
        );

        ended(&mut state, "alive", 2);

        assert_eq!(
            card_states(&state),
            [("alive", ProcessDisplayState::Failed)]
        );
    }

    #[test]
    fn background_key_moves_running_call() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = Ids::default();
        assert_eq!(
            notices(&move_running(&mut state, &mut ids).unwrap()),
            ["No shell command is running in the foreground."]
        );
        // A batch announces both calls; the first runs while the second waits.
        for (call_id, command) in [("first", "make build"), ("second", "make test")] {
            apply(
                &mut state,
                serde_json::json!({
                    "type": "tool.call", "call_id": call_id, "name": "bash",
                    "arguments": {"command": command}
                }),
            );
        }
        assert!(state.background_processes.foreground_running());

        let [command] = sent(&move_running(&mut state, &mut ids).unwrap())
            .try_into()
            .unwrap();
        assert_eq!(command["type"], "background_tool_call");
        assert_eq!(command["call_id"], "first");

        // The moved call ends with its process, which the list then shows, and the
        // next call in the batch is the one running.
        apply(
            &mut state,
            serde_json::json!({
                "type": "tool.result", "call_id": "first", "name": "bash",
                "output": "", "is_error": false, "process_id": "moved",
                "process_state": "running", "process_started_at_ms": 1
            }),
        );
        assert_eq!(listed(&state), [("moved", "make build")]);
        let [next] = sent(&move_running(&mut state, &mut ids).unwrap())
            .try_into()
            .unwrap();
        assert_eq!(next["call_id"], "second");
    }

    #[test]
    fn long_batch_keeps_running_call() {
        let mut state = UiState::new("fake".into(), None, None);
        for index in 0..MAX_PENDING_STARTS + 4 {
            apply(
                &mut state,
                serde_json::json!({
                    "type": "tool.call", "call_id": format!("call-{index}"), "name": "bash",
                    "arguments": {"command": "make"}
                }),
            );
        }

        let [command] = sent(&move_running(&mut state, &mut Ids::default()).unwrap())
            .try_into()
            .unwrap();

        assert_eq!(command["call_id"], "call-0");
    }

    #[test]
    fn finished_runs_leave_no_label() {
        let mut state = UiState::new("fake".into(), None, None);
        apply(
            &mut state,
            serde_json::json!({
                "type": "tool.call", "call_id": "reused", "name": "bash",
                "arguments": {"command": "make build"}
            }),
        );
        apply(
            &mut state,
            serde_json::json!({
                "type": "tool.result", "call_id": "reused", "name": "bash",
                "output": "done", "is_error": false
            }),
        );

        // A later call with the same id is labelled by its own command.
        start(&mut state, "reused", "npm run dev");

        assert_eq!(listed(&state), [("reused", "npm run dev")]);
    }

    #[test]
    fn kill_by_number_or_id() {
        let mut state = UiState::new("fake".into(), None, None);
        start(&mut state, "server", "npm run dev");
        start(&mut state, "tests", "make test");

        for (target, process_id) in [("2", "tests"), ("server", "server")] {
            let effects = stop(&mut state, target, &mut Ids::default()).unwrap();
            let [command] = sent(&effects).try_into().unwrap();
            assert_eq!(command["type"], "cancel_process");
            assert_eq!(command["process_id"], process_id);
        }
        for missing in ["0", "3", "other"] {
            let effects = stop(&mut state, missing, &mut Ids::default()).unwrap();
            assert!(sent(&effects).is_empty(), "{missing}");
            assert!(notices(&effects)[0].starts_with("No running background process"));
        }
    }

    #[test]
    fn failed_stop_is_reported() {
        let mut state = UiState::new("fake".into(), None, None);
        start(&mut state, "server", "npm run dev");
        stop(&mut state, "1", &mut Ids::default()).unwrap();

        let effects = apply(
            &mut state,
            serde_json::json!({
                "type": "rpc.command.finished", "timestamp": "2026-11-13T09:00:00Z",
                "command_id": "cancel_process-1", "command_type": "cancel_process",
                "ok": false, "error": "No running background process has the id server"
            }),
        );

        assert_eq!(
            notices(&effects),
            ["No running background process has the id server"]
        );
    }

    #[test]
    fn user_stop_not_announced() {
        let mut state = UiState::new("fake".into(), None, None);
        start(&mut state, "server", "npm run dev");

        apply(
            &mut state,
            serde_json::json!({
                "type": "process.ended", "timestamp": "2026-11-13T09:00:00Z",
                "process_id": "server", "command": "npm run dev", "state": "cancelled",
                "duration_ms": 1000, "notify": false
            }),
        );

        assert!(listed(&state).is_empty());
        assert_eq!(state.background_processes.exits(), (0, None));
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
        let types: Vec<_> = sent(&effects)
            .iter()
            .map(|command| command["type"].clone())
            .collect();
        assert_eq!(types, ["get_state", "get_processes"]);
        // The running list's read is still in flight, so only the ended list, whose
        // earlier read no longer applies, is asked for again.
        let again = reload(&mut state, &mut Ids::default()).unwrap();
        assert_eq!(sent(&again).len(), 1);
        assert_eq!(sent(&again)[0]["type"], "get_processes");
    }

    #[test]
    fn user_stop_is_marked() {
        let mut state = UiState::new("fake".into(), None, None);
        start(&mut state, "server", "npm run dev");
        apply(
            &mut state,
            serde_json::json!({
                "type": "process.ended", "timestamp": "2026-11-13T09:00:00Z",
                "process_id": "server", "command": "npm run dev", "state": "cancelled",
                "exit_code": null, "duration_ms": 1000, "notify": false
            }),
        );

        let ended = &state.background_processes.finished()[0];
        assert_eq!(ended.state, ProcessDisplayState::Cancelled);
        assert!(ended.stopped_by_user);
    }

    #[test]
    fn ended_keeps_its_place() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = Ids::default();
        for process_id in ["a", "b"] {
            start(&mut state, process_id, "make");
            ended(&mut state, process_id, 0);
        }
        let effects = reduce_with(
            &mut state,
            UiAction::PeekProcess(Some("a".into())),
            &mut ids,
        );
        let read = sent(&effects)[0]["id"].as_str().unwrap().to_owned();

        apply(&mut state, output(&read, "a", "completed", "done\n"));

        assert_eq!(
            finished(&state),
            [
                ("a", ProcessDisplayState::Completed),
                ("b", ProcessDisplayState::Completed)
            ]
        );
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

    fn report(process_id: &str, state: &str) -> serde_json::Value {
        serde_json::json!({
            "process_id": process_id, "command": "make test", "state": state,
            "exit_code": if state == "running" { None } else { Some(0) },
            "started_at_ms": 0, "ended_at_ms": null, "stopped_by_user": false,
            "notify": false
        })
    }

    fn output(command_id: &str, process_id: &str, state: &str, text: &str) -> serde_json::Value {
        serde_json::json!({
            "type": "rpc.process_output", "command_id": command_id,
            "timestamp": "2026-11-15T09:00:00Z", "process": report(process_id, state),
            "chunks": [{"stream": "stdout", "text": text}], "omitted_bytes": 0
        })
    }

    fn finished_command(command_id: &str, ok: bool) -> serde_json::Value {
        serde_json::json!({
            "type": "rpc.command.finished", "command_id": command_id,
            "command_type": "get_process_output", "ok": ok,
            "error": if ok { None } else { Some("No background process has the id p") }
        })
    }

    fn reduce_with(state: &mut UiState, action: UiAction, ids: &mut Ids) -> Vec<UiEffect> {
        reduce(state, action, ids).unwrap()
    }

    fn finished(state: &UiState) -> Vec<(&str, ProcessDisplayState)> {
        state
            .background_processes
            .finished()
            .iter()
            .map(|process| (process.process_id.as_str(), process.state))
            .collect()
    }

    #[test]
    fn ended_process_stays_listed() {
        let mut state = UiState::new("fake".into(), None, None);
        start(&mut state, "tests", "make test");
        ended(&mut state, "tests", 2);

        assert!(listed(&state).is_empty());
        assert_eq!(finished(&state), [("tests", ProcessDisplayState::Failed)]);
        assert_eq!(state.background_processes.finished()[0].exit_code, Some(2));
    }

    #[test]
    fn list_reply_keeps_seen_exit() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = Ids::default();
        start(&mut state, "tests", "make test");
        let effects = reduce_with(&mut state, UiAction::LoadBackgroundProcesses, &mut ids);
        let read = sent(&effects)[0]["id"].as_str().unwrap().to_owned();
        assert_eq!(sent(&effects)[0]["type"], "get_processes");
        // The exit arrives before the reply that was read while it still ran.
        ended(&mut state, "tests", 0);
        apply(
            &mut state,
            serde_json::json!({
                "type": "rpc.processes", "command_id": read,
                "timestamp": "2026-11-15T09:00:00Z",
                "processes": [report("tests", "running"), report("build", "completed"),
                    report("server", "running")]
            }),
        );

        assert_eq!(listed(&state), [("server", "make test")]);
        assert_eq!(
            finished(&state),
            [
                ("tests", ProcessDisplayState::Completed),
                ("build", ProcessDisplayState::Completed)
            ]
        );
    }

    #[test]
    fn stale_output_is_dropped() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = Ids::default();
        start(&mut state, "a", "make a");
        start(&mut state, "b", "make b");
        let first = reduce_with(
            &mut state,
            UiAction::PeekProcess(Some("a".into())),
            &mut ids,
        );
        let first = sent(&first)[0]["id"].as_str().unwrap().to_owned();
        reduce_with(
            &mut state,
            UiAction::PeekProcess(Some("b".into())),
            &mut ids,
        );

        let late = apply(&mut state, output(&first, "a", "running", "from a\n"));
        let failed = apply(&mut state, finished_command(&first, false));

        let peek = state.background_processes.peek().unwrap();
        assert_eq!(peek.process_id, "b");
        assert!(peek.output.is_none() && !peek.gone);
        assert!(late.is_empty() && failed.is_empty(), "{late:?} {failed:?}");
    }

    #[test]
    fn failed_read_marks_gone() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = Ids::default();
        start(&mut state, "p", "make test");
        ended(&mut state, "p", 0);
        let effects = reduce_with(
            &mut state,
            UiAction::PeekProcess(Some("p".into())),
            &mut ids,
        );
        let read = sent(&effects)[0]["id"].as_str().unwrap().to_owned();

        let effects = apply(&mut state, finished_command(&read, false));

        assert!(notices(&effects).is_empty(), "{effects:?}");
        assert!(state.background_processes.peek().unwrap().gone);
        assert!(finished(&state).is_empty());
        assert!(!state.background_processes.peek_polling());
        assert!(reduce_with(&mut state, UiAction::PollProcessPeek, &mut ids).is_empty());
    }

    #[test]
    fn reload_closes_peek() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = Ids::default();
        start(&mut state, "p", "make test");
        ended(&mut state, "p", 0);
        let effects = reduce_with(
            &mut state,
            UiAction::PeekProcess(Some("p".into())),
            &mut ids,
        );
        let read = sent(&effects)[0]["id"].as_str().unwrap().to_owned();

        reload(&mut state, &mut ids).unwrap();
        let late = apply(&mut state, output(&read, "p", "completed", "old\n"));

        assert!(state.background_processes.peek().is_none());
        assert!(finished(&state).is_empty());
        assert!(late.is_empty(), "{late:?}");
    }
}
