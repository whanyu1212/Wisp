//! Terminal-independent, bounded tool and managed-process presentation state.

use std::fmt::Write as _;

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::tool_detail::{DetailAvailability, DetailResult, ToolDetailSource, build_tool_detail};

pub const TOOL_OUTPUT_MAX_BYTES: usize = 64 * 1024;
pub const TOOL_OUTPUT_MAX_LINES: usize = 500;
pub const TOOL_PREVIEW_MAX_BYTES: usize = 2_000;
pub const TOOL_PREVIEW_MAX_LINES: usize = 8;
pub const INTERRUPTED_TOOL_RESULT_TEXT: &str =
    "Tool call interrupted before completion; execution outcome is unknown.";
const BUILTIN_ACTION_MAX_CHARS: usize = 200;
const GENERIC_ACTION_MAX_CHARS: usize = 160;
/// An unknown tool's name is its card label, so keep it short enough to scan.
const TOOL_LABEL_MAX_CHARS: usize = 32;
/// Card labels pad to one width, the longest built-in label (`Process`), so the text
/// after every chip starts in the same column.
const CHIP_LABEL_CHARS: usize = 7;
const GENERIC_VALUE_MAX_CHARS: usize = 64;
const GENERIC_MAX_ITEMS: usize = 8;
const GENERIC_ITEMS_KEY: &str = "\0wisp.items";
const GENERIC_OMITTED_KEY: &str = "\0wisp.omitted";
const PATH_MAX_CHARS: usize = 80;
const REASON_MAX_BYTES: usize = 512;
const TOOL_NAME_MAX_CHARS: usize = 128;
const CALL_ID_RETAINED_MAX_CHARS: usize = 128;
const PROCESS_ID_DISPLAY_MAX_CHARS: usize = 64;
const IDENTITY_MAX_BYTES: usize = 4 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolStatus {
    Requested,
    AwaitingApproval,
    Running,
    Done,
    Error,
    Denied,
    Cancelled,
}

impl ToolStatus {
    pub fn terminal(self) -> bool {
        matches!(
            self,
            Self::Done | Self::Error | Self::Denied | Self::Cancelled
        )
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Requested => "requested",
            Self::AwaitingApproval => "awaiting_approval",
            Self::Running => "running",
            Self::Done => "done",
            Self::Error => "error",
            Self::Denied => "denied",
            Self::Cancelled => "cancelled",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProcessOperation {
    Poll,
    Cancel,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProcessDisplayState {
    PollAwaitingApproval,
    CancelAwaitingApproval,
    Polling,
    Cancelling,
    Running,
    Completed,
    Failed,
    TimedOut,
    Cancelled,
    PollDenied,
    CancelDenied,
    PollInterrupted,
    CancelInterrupted,
    PollFailed,
    CancelFailed,
    Observed,
}

impl ProcessDisplayState {
    pub fn terminal(self) -> bool {
        matches!(
            self,
            Self::Completed | Self::Failed | Self::TimedOut | Self::Cancelled
        )
    }

    /// Whether this card has no active process operation and may leave the
    /// bounded process index once all call bindings targeting it are resolved.
    pub fn evictable(self) -> bool {
        self.terminal()
            || matches!(
                self,
                Self::Observed
                    | Self::PollDenied
                    | Self::CancelDenied
                    | Self::PollInterrupted
                    | Self::CancelInterrupted
                    | Self::PollFailed
                    | Self::CancelFailed
            )
    }

    pub fn status(self) -> ToolStatus {
        match self {
            Self::PollAwaitingApproval | Self::CancelAwaitingApproval => {
                ToolStatus::AwaitingApproval
            }
            Self::Polling | Self::Cancelling | Self::Running | Self::Observed => {
                ToolStatus::Running
            }
            Self::Completed => ToolStatus::Done,
            Self::Cancelled
            | Self::PollDenied
            | Self::CancelDenied
            | Self::PollInterrupted
            | Self::CancelInterrupted => ToolStatus::Cancelled,
            Self::Failed | Self::TimedOut | Self::PollFailed | Self::CancelFailed => {
                ToolStatus::Error
            }
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct BoundedText {
    pub text: String,
    pub source_bytes: u64,
    pub source_lines: u64,
    pub dropped_bytes: u64,
    pub dropped_lines: u64,
    /// Absolute source byte represented by `text[0]`; nonzero for retained tails.
    pub base_offset: u64,
}

impl BoundedText {
    pub fn head(source: &str, max_bytes: usize, max_lines: usize) -> Self {
        bounded_text(source, max_bytes, max_lines, Retention::Head)
    }

    pub fn tail(source: &str, max_bytes: usize, max_lines: usize) -> Self {
        bounded_text(source, max_bytes, max_lines, Retention::Tail)
    }

    pub fn preview_head(&self) -> Self {
        Self::head(&self.text, TOOL_PREVIEW_MAX_BYTES, TOOL_PREVIEW_MAX_LINES)
    }

    pub fn preview_tail(&self) -> Self {
        let mut preview = Self::tail(&self.text, TOOL_PREVIEW_MAX_BYTES, TOOL_PREVIEW_MAX_LINES);
        preview.base_offset = preview.base_offset.saturating_add(self.base_offset);
        preview.source_bytes = self.source_bytes;
        preview.source_lines = self.source_lines;
        preview.dropped_bytes = preview.base_offset;
        preview.dropped_lines = self
            .source_lines
            .saturating_sub(logical_line_count(&preview.text));
        preview
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolCallInput {
    pub call_id: String,
    pub name: String,
    pub arguments: Value,
    pub detail_source: ToolDetailSource,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolResultInput {
    pub call_id: String,
    pub name: String,
    pub output: String,
    pub output_tail: Option<String>,
    pub output_source_bytes: u64,
    pub output_source_lines: u64,
    pub output_projection_cut_mid_line: bool,
    pub is_error: bool,
    pub failure_code: Option<String>,
    pub retryable: bool,
    pub recovery_hint: Option<String>,
    pub exit_code: Option<i64>,
    pub output_has_exit_status: bool,
    pub before_text: Option<String>,
    pub created: bool,
    pub summary: Option<String>,
    pub truncated: bool,
    pub process_id: Option<String>,
    pub process_state: Option<String>,
    pub process_error: Option<String>,
    pub stdout: Option<String>,
    pub stdout_source_bytes: u64,
    pub stderr: Option<String>,
    pub stderr_source_bytes: u64,
    pub stdout_truncated: bool,
    pub stderr_truncated: bool,
    pub stdout_dropped_bytes: u64,
    pub stderr_dropped_bytes: u64,
    /// How long the tool ran, as the backend measured it; `None` for results that
    /// never ran or were recorded before durations were kept.
    pub duration_ms: Option<u64>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProcessCallIdentity {
    pub process_id: String,
    pub operation: ProcessOperation,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolCardSnapshot {
    pub call_id: String,
    pub name: String,
    pub action_arguments: String,
    pub command_summary: Option<CommandSummary>,
    pub process_id: Option<String>,
    pub status: ToolStatus,
    pub arguments_available: bool,
    pub detail: String,
    pub retained_output: BoundedText,
    pub backend_truncated: bool,
    pub failure_code: Option<String>,
    pub retryable: bool,
    pub detail_source: ToolDetailSource,
    pub structured_detail: DetailAvailability,
    /// Kept as 32 bits, which spans 49 days, so the card stays compact.
    pub duration_ms: Option<u32>,
    /// The folder this call's run used, which its paths are shown relative to.
    pub display_base: Option<std::sync::Arc<str>>,
    /// A shell's exit status, when its output opens with Wisp's completion envelope.
    pub exit_code: Option<i32>,
}

impl ToolCardSnapshot {
    pub fn requested(input: &ToolCallInput, status: ToolStatus) -> Self {
        Self {
            call_id: call_identity_for_display(&input.call_id),
            name: bounded_tool_name(&input.name),
            action_arguments: format_tool_arguments(&input.name, &input.arguments),
            command_summary: command_summary(input),
            process_id: None,
            status,
            arguments_available: true,
            detail: String::new(),
            retained_output: BoundedText::default(),
            backend_truncated: false,
            failure_code: None,
            retryable: false,
            detail_source: input.detail_source.clone(),
            structured_detail: DetailAvailability::None,
            duration_ms: None,
            display_base: None,
            exit_code: None,
        }
    }

    pub fn result_without_request(input: &ToolResultInput) -> Self {
        let call = ToolCallInput {
            call_id: input.call_id.clone(),
            name: input.name.clone(),
            arguments: Value::Object(Map::new()),
            detail_source: ToolDetailSource::None,
        };
        let mut card = Self::requested(&call, ToolStatus::Requested);
        card.arguments_available = false;
        card.apply_result(input);
        card
    }

    pub fn enrich_call(&mut self, input: &ToolCallInput) -> bool {
        if self.status.terminal() {
            return false;
        }
        let name = bounded_tool_name(&input.name);
        let action_arguments = format_tool_arguments(&input.name, &input.arguments);
        if self.arguments_available
            && (self.name != name || self.action_arguments != action_arguments)
        {
            self.status = ToolStatus::Error;
            self.detail = "conflicting tool lifecycle metadata".into();
            self.detail_source = ToolDetailSource::None;
            self.structured_detail = DetailAvailability::None;
            return true;
        }
        if self.arguments_available && self.detail_source != input.detail_source {
            self.detail_source = ToolDetailSource::Unavailable(
                crate::tool_detail::DetailUnavailableReason::ConflictingLifecycle,
            );
        } else {
            self.detail_source = input.detail_source.clone();
        }
        self.name = name;
        self.action_arguments = action_arguments;
        self.command_summary = command_summary(input);
        self.arguments_available = true;
        true
    }

    pub fn approval_requested(&mut self) -> bool {
        if self.status != ToolStatus::Requested {
            return false;
        }
        self.status = ToolStatus::AwaitingApproval;
        true
    }

    pub fn approval_resolved(&mut self, approved: bool, reason: Option<&str>) -> bool {
        if self.status.terminal() || self.status == ToolStatus::Running {
            return false;
        }
        if approved {
            if self.status == ToolStatus::Running {
                return false;
            }
            self.status = ToolStatus::Running;
        } else {
            self.status = ToolStatus::Denied;
            self.detail = bounded_reason(reason.unwrap_or("denied"));
            self.detail_source = ToolDetailSource::None;
            self.structured_detail = DetailAvailability::None;
        }
        true
    }

    pub fn apply_result(&mut self, input: &ToolResultInput) -> bool {
        if self.status == ToolStatus::Denied || self.status.terminal() {
            return false;
        }
        self.status = tool_result_status(input);
        self.duration_ms = input
            .duration_ms
            .map(|milliseconds| u32::try_from(milliseconds).unwrap_or(u32::MAX));
        self.exit_code = input
            .exit_code
            .filter(|_| input.output_has_exit_status)
            .and_then(|code| i32::try_from(code).ok());
        self.process_id = input
            .process_id
            .as_ref()
            .filter(|id| id.len() <= PROCESS_ID_DISPLAY_MAX_CHARS)
            .cloned();
        let retain_tail = self.status == ToolStatus::Error
            || (self.status == ToolStatus::Done && self.name == "bash");
        let selected_output = if retain_tail && self.status == ToolStatus::Done {
            input.output_tail.as_deref().unwrap_or(&input.output)
        } else {
            &input.output
        };
        let normalized_output = normalize_newlines(selected_output);
        self.retained_output = if retain_tail {
            BoundedText::tail(
                &normalized_output,
                TOOL_OUTPUT_MAX_BYTES,
                TOOL_OUTPUT_MAX_LINES,
            )
        } else {
            BoundedText::head(
                &normalized_output,
                TOOL_OUTPUT_MAX_BYTES,
                TOOL_OUTPUT_MAX_LINES,
            )
        };
        let raw_output_bytes = u64::try_from(selected_output.len()).unwrap_or(u64::MAX);
        let normalized_output_bytes = u64::try_from(normalized_output.len()).unwrap_or(u64::MAX);
        self.retained_output.source_bytes = normalized_output_bytes
            .saturating_add(input.output_source_bytes.saturating_sub(raw_output_bytes));
        self.retained_output.dropped_bytes = self
            .retained_output
            .source_bytes
            .saturating_sub(u64::try_from(self.retained_output.text.len()).unwrap_or(u64::MAX));
        // A projected tail already lost its earlier lines; count them from the source
        // so "(N earlier lines)" names every line above what is shown.
        self.retained_output.source_lines = self
            .retained_output
            .source_lines
            .max(input.output_source_lines);
        self.retained_output.dropped_lines = self
            .retained_output
            .source_lines
            .saturating_sub(logical_line_count(&self.retained_output.text));
        if retain_tail {
            self.retained_output.base_offset = self.retained_output.dropped_bytes;
        }
        let preview = if retain_tail {
            self.retained_output.preview_tail()
        } else {
            self.retained_output.preview_head()
        };
        self.detail = preferred_result_detail(input, &preview.text, self.status);
        self.backend_truncated = input.truncated;
        self.failure_code = input.failure_code.as_deref().map(bounded_reason);
        self.retryable = input.retryable;
        let result_name_matches = self.name == bounded_tool_name(&input.name);
        self.structured_detail = if self.status == ToolStatus::Done && result_name_matches {
            build_tool_detail(
                &self.detail_source,
                DetailResult {
                    output: &normalized_output,
                    before_text: input.before_text.as_deref(),
                    created: input.created,
                    summary: input.summary.as_deref(),
                    truncated: input.truncated,
                    projection_omitted_bytes: input
                        .output_source_bytes
                        .saturating_sub(u64::try_from(normalized_output.len()).unwrap_or(u64::MAX)),
                    projection_omitted_rows: input
                        .output_source_lines
                        .saturating_sub(logical_line_count(&normalized_output)),
                    projection_cut_mid_line: input.output_projection_cut_mid_line,
                },
            )
        } else if self.status == ToolStatus::Done
            && !matches!(&self.detail_source, ToolDetailSource::None)
        {
            DetailAvailability::Unavailable(
                crate::tool_detail::DetailUnavailableReason::ConflictingLifecycle,
            )
        } else {
            DetailAvailability::None
        };
        if matches!(&self.structured_detail, DetailAvailability::LiveRetained(_)) {
            self.retained_output = BoundedText::default();
        }
        if let DetailAvailability::Unavailable(reason) = &self.structured_detail {
            if !self.detail.is_empty() {
                self.detail.push_str(" · ");
            }
            self.detail.push_str(reason.label());
        }
        self.detail_source = ToolDetailSource::None;
        true
    }

    pub fn cancel(&mut self, reason: &str) -> bool {
        if self.status.terminal() {
            return false;
        }
        self.status = ToolStatus::Cancelled;
        self.detail = bounded_reason(reason);
        self.detail_source = ToolDetailSource::None;
        self.structured_detail = DetailAvailability::None;
        true
    }

    pub(crate) fn reconcile_historical_result(
        &mut self,
        result: &ToolResultInput,
        result_card: &Self,
        detail_source: ToolDetailSource,
    ) -> bool {
        if !matches!(self.status, ToolStatus::Requested | ToolStatus::Cancelled) {
            return false;
        }
        self.status = ToolStatus::Requested;
        self.detail_source = detail_source;
        self.structured_detail = DetailAvailability::None;
        if result_card.status == ToolStatus::Denied {
            self.status = ToolStatus::Denied;
            self.detail = result_card.detail.clone();
            self.retained_output = result_card.retained_output.clone();
            self.backend_truncated = result_card.backend_truncated;
            self.failure_code = result_card.failure_code.clone();
            self.retryable = result_card.retryable;
            self.detail_source = ToolDetailSource::None;
            return true;
        }
        self.apply_result(result)
    }

    /// The short tool name that labels the card, such as `Bash` or `Search`. The
    /// status marker carries the state. An MCP tool is labelled `MCP`, with the server
    /// and tool it calls after the label; another unknown tool by its own name.
    pub fn action_label(&self) -> String {
        match self.name.as_str() {
            "bash" => "Bash".into(),
            "read" => "Read".into(),
            "grep" | "find" => "Search".into(),
            "ls" => "List".into(),
            "edit" => "Edit".into(),
            "write" => "Write".into(),
            _ if self.mcp_target().is_some() => "MCP".into(),
            name => clip_chars(&one_line(name), TOOL_LABEL_MAX_CHARS),
        }
    }

    /// The server and remote tool an MCP card calls, such as
    /// `github/create_pull_request`. Wisp names MCP tools `mcp__{server}__{tool}`, and
    /// server names hold no `_`, so the first `__` after the prefix ends the server.
    pub fn mcp_target(&self) -> Option<String> {
        let (server, tool) = self.name.strip_prefix("mcp__")?.split_once("__")?;
        if server.is_empty() || tool.is_empty() {
            return None;
        }
        Some(clip_chars(
            &one_line(&format!("{server}/{tool}")),
            TOOL_LABEL_MAX_CHARS * 2,
        ))
    }

    /// How long a finished call ran, for its action line. Calls that were denied,
    /// cancelled, or never ran show none.
    pub fn duration_label(&self) -> Option<String> {
        matches!(self.status, ToolStatus::Done | ToolStatus::Error)
            .then_some(self.duration_ms?)
            .map(|milliseconds| format_duration(u64::from(milliseconds)))
    }

    /// The state the action line spells out because its marker alone does not: `?`
    /// needs saying what it waits for, and `⊘` stands for both denial and
    /// cancellation.
    pub fn status_note(&self) -> Option<&'static str> {
        match self.status {
            ToolStatus::AwaitingApproval => Some("awaiting approval"),
            ToolStatus::Denied => Some("denied"),
            ToolStatus::Cancelled => Some("cancelled"),
            ToolStatus::Requested | ToolStatus::Running | ToolStatus::Done | ToolStatus::Error => {
                None
            }
        }
    }

    /// The label padded to the shared chip width.
    pub fn chip_label(&self) -> String {
        pad_chip_label(self.action_label())
    }

    pub fn action(&self) -> String {
        let mut action = self.chip_label();
        if let Some(target) = self.mcp_target() {
            action.push_str("  ");
            action.push_str(&target);
        }
        if !self.arguments_available {
            action.push_str("  (arguments unavailable)");
        } else if !self.action_arguments.is_empty() {
            action.push_str("  ");
            action.push_str(&self.action_arguments);
        }
        action
    }

    /// A concise display summary; the original arguments remain in retained detail.
    /// The arguments as the action line shows them, relative to the folder the call's
    /// run used. Before the backend has reported one, the TUI's own working directory
    /// stands in: the launcher starts the TUI and its backend in the same folder.
    pub fn display_arguments(&self) -> String {
        let cwd = self.display_base.as_deref().map(str::to_owned).or_else(|| {
            std::env::current_dir()
                .ok()
                .map(|root| root.display().to_string())
        });
        let Some(cwd) = cwd.as_deref().filter(|cwd| cwd.len() > 1) else {
            return self
                .command_summary
                .filter(|_| self.name == "bash")
                .map_or_else(|| self.action_arguments.clone(), CommandSummary::label);
        };
        match self.name.as_str() {
            "bash" => self.command_summary.map_or_else(
                || workspace_relative_command(&self.action_arguments, cwd),
                CommandSummary::label,
            ),
            "read" | "edit" | "write" | "ls" => match self
                .action_arguments
                .strip_prefix(cwd)
                .and_then(|rest| rest.strip_prefix('/'))
            {
                Some("") => ".".to_owned(),
                Some(relative) => relative.to_owned(),
                None => self.action_arguments.clone(),
            },
            "grep" | "find" => match self.action_arguments.rsplit_once(" in ") {
                Some((pattern, path)) => match path.strip_prefix(cwd) {
                    Some("") => format!("{pattern} in ."),
                    Some(rest) if rest.starts_with('/') => format!("{pattern} in {}", &rest[1..]),
                    _ => self.action_arguments.clone(),
                },
                None => self.action_arguments.clone(),
            },
            _ => self.action_arguments.clone(),
        }
    }

    pub fn preview(&self) -> &str {
        &self.detail
    }

    pub fn has_retained_detail(&self) -> bool {
        matches!(self.structured_detail, DetailAvailability::LiveRetained(_))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProcessCardSnapshot {
    pub process_id: String,
    pub task_label: Option<String>,
    pub display_state: ProcessDisplayState,
    pub poll_count: u32,
    pub call_count: u32,
    pub retained_output: BoundedText,
    pub backend_truncated: bool,
    pub backend_dropped_bytes: u64,
    /// The finished process's exit code; background output carries no exit line.
    pub exit_code: Option<i32>,
    last_stream_label: Option<&'static str>,
    last_sequence: Option<u64>,
    approval_sequence: Option<u64>,
    approval_resolved: bool,
}

impl ProcessCardSnapshot {
    pub fn new(process_id: String) -> Self {
        Self {
            process_id,
            task_label: None,
            display_state: ProcessDisplayState::Observed,
            poll_count: 0,
            call_count: 0,
            retained_output: BoundedText::default(),
            backend_truncated: false,
            backend_dropped_bytes: 0,
            exit_code: None,
            last_stream_label: None,
            last_sequence: None,
            approval_sequence: None,
            approval_resolved: false,
        }
    }

    pub fn begin(&mut self, operation: ProcessOperation, sequence: u64) -> bool {
        if !self.accept_sequence(sequence) {
            return false;
        }
        self.call_count = self.call_count.saturating_add(1);
        self.display_state = match operation {
            ProcessOperation::Poll => {
                self.poll_count = self.poll_count.saturating_add(1);
                ProcessDisplayState::Polling
            }
            ProcessOperation::Cancel => ProcessDisplayState::Cancelling,
        };
        true
    }

    pub fn approval_requested(&mut self, operation: ProcessOperation, sequence: u64) -> bool {
        if self.approval_sequence == Some(sequence) || !self.accept_sequence(sequence) {
            return false;
        }
        self.approval_sequence = Some(sequence);
        self.approval_resolved = false;
        self.display_state = match operation {
            ProcessOperation::Poll => ProcessDisplayState::PollAwaitingApproval,
            ProcessOperation::Cancel => ProcessDisplayState::CancelAwaitingApproval,
        };
        true
    }

    pub fn approve(&mut self, operation: ProcessOperation, sequence: u64) -> bool {
        if (self.approval_sequence == Some(sequence) && self.approval_resolved)
            || !self.accept_sequence(sequence)
        {
            return false;
        }
        self.approval_sequence = Some(sequence);
        self.approval_resolved = true;
        self.display_state = match operation {
            ProcessOperation::Poll => ProcessDisplayState::Polling,
            ProcessOperation::Cancel => ProcessDisplayState::Cancelling,
        };
        true
    }

    pub fn deny(
        &mut self,
        operation: ProcessOperation,
        reason: Option<&str>,
        sequence: u64,
    ) -> bool {
        if (self.approval_sequence == Some(sequence) && self.approval_resolved)
            || !self.accept_sequence(sequence)
        {
            return false;
        }
        self.approval_sequence = Some(sequence);
        self.approval_resolved = true;
        if let Some(reason) = reason.filter(|reason| !reason.is_empty()) {
            self.append_unlabelled(&bounded_reason(reason));
        }
        self.display_state = match operation {
            ProcessOperation::Poll => ProcessDisplayState::PollDenied,
            ProcessOperation::Cancel => ProcessDisplayState::CancelDenied,
        };
        true
    }

    pub fn interrupt(&mut self, operation: ProcessOperation, sequence: u64) -> bool {
        if !self.accept_sequence(sequence) {
            return false;
        }
        self.display_state = match operation {
            ProcessOperation::Poll => ProcessDisplayState::PollInterrupted,
            ProcessOperation::Cancel => ProcessDisplayState::CancelInterrupted,
        };
        true
    }

    pub fn conflict(&mut self, operation: ProcessOperation, sequence: u64) -> bool {
        if !self.accept_sequence(sequence) {
            return false;
        }
        self.append_unlabelled("call ID metadata changed; result correlation is ambiguous");
        self.display_state = match operation {
            ProcessOperation::Poll => ProcessDisplayState::PollInterrupted,
            ProcessOperation::Cancel => ProcessDisplayState::CancelInterrupted,
        };
        true
    }

    pub fn observe(
        &mut self,
        operation: ProcessOperation,
        result: &ToolResultInput,
        sequence: u64,
    ) -> bool {
        let update_display_state = self.accept_sequence(sequence);
        let stdout = result.stdout.as_deref().unwrap_or("");
        let stderr = result.stderr.as_deref().unwrap_or("");
        if !stdout.is_empty() {
            self.append_stream("stdout:", stdout);
        }
        if !stderr.is_empty() {
            self.append_stream("stderr:", stderr);
        }
        let process_error = result
            .process_error
            .as_deref()
            .filter(|value| !value.is_empty());
        let fallback_output = result.output_tail.as_deref().unwrap_or(&result.output);
        let uses_fallback_output = process_error.is_none()
            && stdout.is_empty()
            && stderr.is_empty()
            && result.process_state.is_none()
            && !fallback_output.is_empty();
        if let Some(error) = process_error {
            self.append_unlabelled(&bounded_reason(error));
        } else if uses_fallback_output {
            self.append_unlabelled(fallback_output);
        }
        if let Some(hint) = result
            .recovery_hint
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            self.append_unlabelled(&format!("Recovery: {}", bounded_reason(hint)));
        }
        self.backend_truncated |=
            result.truncated || result.stdout_truncated || result.stderr_truncated;
        self.backend_dropped_bytes = self
            .backend_dropped_bytes
            .saturating_add(result.stdout_dropped_bytes)
            .saturating_add(result.stderr_dropped_bytes)
            .saturating_add(if uses_fallback_output {
                result
                    .output_source_bytes
                    .saturating_sub(u64::try_from(fallback_output.len()).unwrap_or(u64::MAX))
            } else {
                0
            })
            .saturating_add(
                result
                    .stdout_source_bytes
                    .saturating_sub(u64::try_from(stdout.len()).unwrap_or(u64::MAX)),
            )
            .saturating_add(
                result
                    .stderr_source_bytes
                    .saturating_sub(u64::try_from(stderr.len()).unwrap_or(u64::MAX)),
            );
        if update_display_state {
            self.display_state = process_result_state(operation, result);
            self.exit_code = result
                .exit_code
                .and_then(|code| i32::try_from(code).ok())
                .filter(|_| {
                    matches!(
                        self.display_state,
                        ProcessDisplayState::Completed | ProcessDisplayState::Failed
                    )
                });
        }
        true
    }

    pub(crate) fn reconcile_historical_result(
        &mut self,
        operation: ProcessOperation,
        result: &ToolResultInput,
        sequence: u64,
        denied: bool,
    ) -> bool {
        if !denied {
            return self.observe(operation, result, sequence);
        }
        if self.last_sequence.is_some_and(|current| sequence < current) {
            self.append_unlabelled("denied");
            return true;
        }
        self.deny(operation, Some("denied"), sequence)
    }

    pub(crate) fn merge_historical_newer(&mut self, newer: &Self) -> u64 {
        let sequence_offset = self
            .last_sequence
            .map_or(0, |sequence| sequence.saturating_add(1));
        let separator = usize::from(
            !self.retained_output.text.is_empty() && !newer.retained_output.text.is_empty(),
        );
        let combined = match (
            self.retained_output.text.is_empty(),
            newer.retained_output.text.is_empty(),
        ) {
            (true, _) => newer.retained_output.text.clone(),
            (_, true) => self.retained_output.text.clone(),
            (false, false) => format!(
                "{}\n{}",
                self.retained_output.text, newer.retained_output.text
            ),
        };
        let source_bytes = self
            .retained_output
            .source_bytes
            .saturating_add(newer.retained_output.source_bytes)
            .saturating_add(u64::try_from(separator).unwrap_or(u64::MAX));
        let source_lines = self
            .retained_output
            .source_lines
            .saturating_add(newer.retained_output.source_lines)
            .saturating_add(u64::from(
                separator > 0 && self.retained_output.text.ends_with('\n'),
            ));
        let mut retained =
            BoundedText::tail(&combined, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES);
        retained.source_bytes = source_bytes;
        retained.source_lines = source_lines;
        retained.base_offset =
            source_bytes.saturating_sub(u64::try_from(retained.text.len()).unwrap_or(u64::MAX));
        retained.dropped_bytes = retained.base_offset;
        retained.dropped_lines = source_lines.saturating_sub(logical_line_count(&retained.text));

        self.task_label = newer.task_label.clone().or_else(|| self.task_label.clone());
        self.display_state = newer.display_state;
        self.exit_code = newer.exit_code;
        self.poll_count = self.poll_count.saturating_add(newer.poll_count);
        self.call_count = self.call_count.saturating_add(newer.call_count);
        self.retained_output = retained;
        self.backend_truncated |= newer.backend_truncated;
        self.backend_dropped_bytes = self
            .backend_dropped_bytes
            .saturating_add(newer.backend_dropped_bytes);
        let last_stream_label = if newer.retained_output.text.is_empty() {
            self.last_stream_label
        } else {
            newer.last_stream_label
        };
        self.last_stream_label = last_stream_label
            .filter(|label| self.retained_output.text.lines().any(|line| line == *label));
        self.last_sequence = newer
            .last_sequence
            .map(|sequence| sequence.saturating_add(sequence_offset));
        self.approval_sequence = None;
        self.approval_resolved = false;
        sequence_offset
    }

    pub(crate) fn prepend_historical_older(&mut self, older: &Self) -> u64 {
        let approval_sequence = self.approval_sequence;
        let approval_resolved = self.approval_resolved;
        let mut merged = older.clone();
        let sequence_offset = merged.merge_historical_newer(self);
        merged.approval_sequence = approval_sequence.map(|sequence| {
            sequence
                .checked_add(sequence_offset)
                .expect("tool lifecycle sequence exhausted")
        });
        merged.approval_resolved = approval_resolved;
        *self = merged;
        sequence_offset
    }

    pub(crate) fn next_historical_sequence(&self) -> u64 {
        self.last_sequence
            .map_or(0, |sequence| sequence.saturating_add(1))
    }

    pub(crate) fn release_historical_sequence(&mut self) {
        self.last_sequence = None;
        self.approval_sequence = None;
        self.approval_resolved = false;
    }

    fn accept_sequence(&mut self, sequence: u64) -> bool {
        if self.last_sequence.is_some_and(|current| sequence < current) {
            return false;
        }
        self.last_sequence = Some(sequence);
        true
    }

    pub fn action_label(&self) -> &'static str {
        "Process"
    }

    /// The state the action line spells out beyond what its marker says. Plain
    /// running, completion and failure need no words; the marker shows them.
    pub fn status_note(&self) -> Option<&'static str> {
        match self.display_state {
            ProcessDisplayState::Running
            | ProcessDisplayState::Completed
            | ProcessDisplayState::Failed => None,
            ProcessDisplayState::PollAwaitingApproval => Some("poll awaiting approval"),
            ProcessDisplayState::CancelAwaitingApproval => Some("cancellation awaiting approval"),
            ProcessDisplayState::Polling => Some("polling"),
            ProcessDisplayState::Cancelling => Some("cancelling"),
            ProcessDisplayState::TimedOut => Some("timed out"),
            ProcessDisplayState::Cancelled => Some("cancelled"),
            ProcessDisplayState::PollDenied => Some("poll denied"),
            ProcessDisplayState::CancelDenied => Some("cancellation denied"),
            ProcessDisplayState::PollInterrupted => Some("poll interrupted"),
            ProcessDisplayState::CancelInterrupted => Some("cancellation interrupted"),
            ProcessDisplayState::PollFailed => Some("poll failed"),
            ProcessDisplayState::CancelFailed => Some("cancellation failed"),
            ProcessDisplayState::Observed => Some("observed"),
        }
    }

    /// The label padded to the shared chip width.
    pub fn chip_label(&self) -> String {
        pad_chip_label(self.action_label().to_owned())
    }

    pub fn action(&self) -> String {
        let label = self.chip_label();
        format!(
            "{label}  {} · {} call{} · {} poll{}",
            clip_chars(
                &one_line(identity_for_display(&self.process_id)),
                PROCESS_ID_DISPLAY_MAX_CHARS,
            ),
            self.call_count,
            if self.call_count == 1 { "" } else { "s" },
            self.poll_count,
            if self.poll_count == 1 { "" } else { "s" },
        )
    }

    pub fn preview(&self) -> BoundedText {
        self.retained_output.preview_tail()
    }

    fn append_stream(&mut self, label: &'static str, output: &str) {
        let normalized = normalize_newlines(output);
        if normalized.is_empty() {
            return;
        }
        let content = normalized.trim_end_matches('\n');
        let content = if content.is_empty() { "\n" } else { content };
        let chunk = if self.last_stream_label == Some(label) {
            content.to_owned()
        } else {
            format!("{label}\n{content}")
        };
        self.append_chunk(&chunk);
        self.last_stream_label = self
            .retained_output
            .text
            .lines()
            .any(|line| line == label)
            .then_some(label);
    }

    fn append_unlabelled(&mut self, output: &str) {
        let normalized = normalize_newlines(output);
        if normalized.is_empty() {
            return;
        }
        let content = normalized.trim_end_matches('\n');
        self.append_chunk(if content.is_empty() { "\n" } else { content });
        self.last_stream_label = None;
    }

    fn append_chunk(&mut self, chunk: &str) {
        let combined = if self.retained_output.text.is_empty() {
            chunk.to_owned()
        } else {
            format!("{}\n{chunk}", self.retained_output.text)
        };
        let previously_dropped = self.retained_output.base_offset;
        let previously_dropped_lines = self.retained_output.dropped_lines;
        let mut bounded =
            BoundedText::tail(&combined, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES);
        bounded.base_offset = bounded.base_offset.saturating_add(previously_dropped);
        bounded.source_bytes = bounded.source_bytes.saturating_add(previously_dropped);
        bounded.source_lines = bounded
            .source_lines
            .saturating_add(previously_dropped_lines);
        bounded.dropped_bytes = bounded.base_offset;
        bounded.dropped_lines = bounded
            .dropped_lines
            .saturating_add(previously_dropped_lines);
        self.retained_output = bounded;
    }
}

pub fn bounded_tool_arguments(name: &str, arguments: &Value) -> Value {
    let Some(arguments) = arguments.as_object() else {
        return Value::Object(Map::new());
    };
    let keys: &[&str] = match name {
        "read" => &["path", "offset", "limit"],
        "grep" => &[
            "pattern",
            "path",
            "glob",
            "ignore_case",
            "literal",
            "context",
            "max_results",
        ],
        "find" => &["pattern", "path", "max_results"],
        "ls" => &["path", "all"],
        "bash" => &[
            "operation",
            "command",
            "process_id",
            "wait_seconds",
            "lifetime_seconds",
            "yield_seconds",
        ],
        "edit" | "write" => &["path"],
        _ => {
            let mut bounded = Map::new();
            let mut selected: Vec<&String> = Vec::with_capacity(GENERIC_MAX_ITEMS);
            for key in arguments.keys() {
                let position = selected
                    .binary_search_by(|candidate| candidate.as_str().cmp(key.as_str()))
                    .unwrap_or_else(|position| position);
                if position < GENERIC_MAX_ITEMS {
                    selected.insert(position, key);
                    if selected.len() > GENERIC_MAX_ITEMS {
                        selected.pop();
                    }
                }
            }
            for key in selected {
                let value = arguments.get(key).expect("selected key exists");
                bounded.insert(
                    clip_chars(key, 64),
                    bounded_argument_value(value, GENERIC_VALUE_MAX_CHARS),
                );
            }
            let omitted = arguments.len().saturating_sub(bounded.len());
            let mut wrapper = Map::with_capacity(2);
            wrapper.insert(GENERIC_ITEMS_KEY.into(), Value::Object(bounded));
            wrapper.insert(GENERIC_OMITTED_KEY.into(), Value::from(omitted as u64));
            return Value::Object(wrapper);
        }
    };
    let mut bounded = Map::new();
    for key in keys {
        let Some(value) = arguments.get(*key) else {
            continue;
        };
        let retained = if *key == "process_id" {
            value
                .as_str()
                .map(|source| {
                    if source.trim().is_empty() {
                        "b:".to_owned()
                    } else {
                        bounded_identity(source)
                    }
                })
                .map(Value::String)
                .unwrap_or_else(|| bounded_argument_value(value, 64))
        } else {
            let max_chars = if *key == "command" {
                BUILTIN_ACTION_MAX_CHARS
            } else {
                256
            };
            bounded_argument_value(value, max_chars)
        };
        bounded.insert((*key).to_owned(), retained);
    }
    Value::Object(bounded)
}

fn bounded_argument_value(value: &Value, max_chars: usize) -> Value {
    match value {
        Value::String(value) => Value::String(clip_chars(value, max_chars)),
        Value::Null | Value::Bool(_) | Value::Number(_) => value.clone(),
        Value::Array(values) => Value::String(format!("[{} items]", values.len())),
        Value::Object(values) => Value::String(format!("{{{} fields}}", values.len())),
    }
}

pub fn bounded_identity(source: &str) -> String {
    if source.len() <= IDENTITY_MAX_BYTES {
        return format!("r{}:{source}", source.len());
    }
    let digest = Sha256::digest(source.as_bytes());
    let mut encoded = String::with_capacity(2 + digest.len() * 2);
    encoded.push_str("h:");
    for byte in digest {
        write!(&mut encoded, "{byte:02x}").expect("writing to a string cannot fail");
    }
    encoded
}

fn call_identity_for_display(identity: &str) -> String {
    if identity.starts_with("h:") {
        return identity.to_owned();
    }
    let source = identity_for_display(identity);
    if source.strip_prefix("h:").is_some_and(|digest| {
        digest.len() == 64 && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
    }) {
        return identity.to_owned();
    }
    if source.chars().count() <= CALL_ID_RETAINED_MAX_CHARS {
        return source.to_owned();
    }
    let digest = Sha256::digest(source.as_bytes());
    let mut encoded = String::with_capacity(66);
    encoded.push_str("h:");
    for byte in digest {
        write!(&mut encoded, "{byte:02x}").expect("writing to a string cannot fail");
    }
    encoded
}

pub(crate) fn identity_for_display(identity: &str) -> &str {
    if identity == "b:" {
        return "";
    }
    let Some(encoded) = identity.strip_prefix('r') else {
        return identity;
    };
    let Some((length, source)) = encoded.split_once(':') else {
        return identity;
    };
    if length.parse::<usize>().ok() == Some(source.len()) {
        source
    } else {
        identity
    }
}

pub fn process_call_identity(name: &str, arguments: &Value) -> Option<ProcessCallIdentity> {
    if name != "bash" {
        return None;
    }
    let object = arguments.as_object()?;
    let operation = match object.get("operation").and_then(Value::as_str)? {
        "poll" => ProcessOperation::Poll,
        "cancel" => ProcessOperation::Cancel,
        _ => return None,
    };
    let process_id = object.get("process_id")?.as_str()?;
    if identity_for_display(process_id).trim().is_empty() {
        return None;
    }
    Some(ProcessCallIdentity {
        process_id: process_id.to_owned(),
        operation,
    })
}

pub fn tool_result_status(result: &ToolResultInput) -> ToolStatus {
    if result.process_state.as_deref() == Some("cancelled") {
        ToolStatus::Cancelled
    } else if result.is_error
        || result.exit_code.is_some_and(|code| code != 0)
        || matches!(
            result.process_state.as_deref(),
            Some("failed" | "timed_out")
        )
    {
        ToolStatus::Error
    } else {
        ToolStatus::Done
    }
}

fn process_result_state(
    operation: ProcessOperation,
    result: &ToolResultInput,
) -> ProcessDisplayState {
    match result.process_state.as_deref() {
        Some("running") => ProcessDisplayState::Running,
        Some("completed") => {
            if tool_result_status(result) == ToolStatus::Done {
                ProcessDisplayState::Completed
            } else {
                ProcessDisplayState::Failed
            }
        }
        Some("failed") => ProcessDisplayState::Failed,
        Some("timed_out") => ProcessDisplayState::TimedOut,
        Some("cancelled") => ProcessDisplayState::Cancelled,
        _ if tool_result_status(result) == ToolStatus::Error => match operation {
            ProcessOperation::Poll => ProcessDisplayState::PollFailed,
            ProcessOperation::Cancel => ProcessDisplayState::CancelFailed,
        },
        _ => ProcessDisplayState::Observed,
    }
}

fn preferred_result_detail(result: &ToolResultInput, preview: &str, status: ToolStatus) -> String {
    if status == ToolStatus::Error {
        if let Some(error) = result
            .process_error
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            return bounded_reason(error);
        }
        if let Some(hint) = result
            .recovery_hint
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            return bounded_reason(hint);
        }
    }
    if status == ToolStatus::Done {
        if let Some(summary) = result.summary.as_deref().filter(|value| !value.is_empty()) {
            return bounded_reason(summary);
        }
    }
    preview.to_owned()
}

fn known_tool(name: &str) -> bool {
    matches!(
        name,
        "bash" | "read" | "grep" | "find" | "ls" | "edit" | "write"
    )
}

fn format_tool_arguments(name: &str, arguments: &Value) -> String {
    if !known_tool(name) {
        return format_generic_value(arguments);
    }
    let Some(arguments) = arguments.as_object() else {
        return clip_chars(&scalar_value(arguments), GENERIC_VALUE_MAX_CHARS);
    };
    let formatted = match name {
        "read" => format_read(arguments),
        "grep" => format_grep(arguments),
        "find" => format_find(arguments),
        "ls" => path_value(arguments, "path", "."),
        "bash" => format_bash(arguments),
        "edit" | "write" => path_value(arguments, "path", "<path>"),
        _ => unreachable!("unknown tools return before built-in formatting"),
    };
    clip_chars(&formatted, BUILTIN_ACTION_MAX_CHARS)
}

fn format_read(arguments: &Map<String, Value>) -> String {
    let mut output = path_value(arguments, "path", "<path>");
    let offset = positive_u64(arguments.get("offset"));
    let limit = positive_u64(arguments.get("limit"));
    if offset.is_some() || limit.is_some() {
        let start = offset.unwrap_or(1);
        let end = limit.map(|limit| start.saturating_add(limit).saturating_sub(1));
        output.push(':');
        output.push_str(&start.to_string());
        output.push('-');
        if let Some(end) = end {
            output.push_str(&end.to_string());
        }
    }
    output
}

fn format_grep(arguments: &Map<String, Value>) -> String {
    let pattern = string_value(arguments, "pattern", "");
    let path = path_value(arguments, "path", ".");
    format!("/{}/ in {path}", clip_chars(&one_line(&pattern), 64))
}

fn format_find(arguments: &Map<String, Value>) -> String {
    let pattern = string_value(arguments, "pattern", "*");
    let path = path_value(arguments, "path", ".");
    format!("{} in {path}", clip_chars(&one_line(&pattern), 64))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CommandSummary(u8);

impl CommandSummary {
    fn label(self) -> String {
        [
            "Python tests",
            "Python quality checks",
            "Rust tests",
            "Rust build",
            "Rust lint checks",
        ]
        .into_iter()
        .enumerate()
        .filter_map(|(index, label)| (self.0 & (1 << index) != 0).then_some(label))
        .collect::<Vec<_>>()
        .join(" + ")
    }
}

// Classify the complete source command before display normalization or clipping.
fn command_summary(input: &ToolCallInput) -> Option<CommandSummary> {
    if input.name != "bash" {
        return None;
    }
    summarize_command(input.arguments.get("command")?.as_str()?)
}

fn summarize_command(command: &str) -> Option<CommandSummary> {
    if command.contains([';', '|', '`', '$', '\n', '\r', '>', '<', '\'', '"']) {
        return None;
    }
    let mut flags = 0;
    for part in command.split("&&") {
        if part.contains('&') {
            return None;
        }
        let words = part.split_whitespace().collect::<Vec<_>>();
        let words = words.strip_prefix(&["uv", "run"]).unwrap_or(&words);
        flags |= match words {
            ["pytest", ..] => 1,
            ["ruff" | "mypy", ..] => 2,
            ["cargo", "test", ..] => 4,
            ["cargo", "build", ..] => 8,
            ["cargo", "clippy", ..] => 16,
            ["cd", _] => continue,
            _ => return None,
        };
    }
    (flags != 0).then_some(CommandSummary(flags))
}

fn format_bash(arguments: &Map<String, Value>) -> String {
    let operation = arguments
        .get("operation")
        .and_then(Value::as_str)
        .unwrap_or("run");
    if matches!(operation, "poll" | "cancel") {
        return format!(
            "{operation} {}",
            clip_chars(
                &one_line(&string_value(arguments, "process_id", "<process>")),
                PROCESS_ID_DISPLAY_MAX_CHARS,
            )
        );
    }
    let command = string_value(arguments, "command", "");
    if operation == "start" {
        format!("start {}", clip_chars(&one_line(&command), 180))
    } else {
        clip_chars(&one_line(&command), 190)
    }
}

fn format_generic_value(arguments: &Value) -> String {
    let Some(object) = arguments.as_object() else {
        return clip_chars(&scalar_value(arguments), GENERIC_VALUE_MAX_CHARS);
    };
    let wrapped = object.len() == 2
        && object.get(GENERIC_ITEMS_KEY).is_some_and(Value::is_object)
        && object.get(GENERIC_OMITTED_KEY).is_some_and(Value::is_u64);
    if wrapped {
        let items = object[GENERIC_ITEMS_KEY]
            .as_object()
            .expect("validated generic items wrapper");
        let omitted = object[GENERIC_OMITTED_KEY]
            .as_u64()
            .expect("validated generic omission wrapper");
        return format_generic(items, usize::try_from(omitted).unwrap_or(usize::MAX));
    }
    format_generic(object, object.len().saturating_sub(GENERIC_MAX_ITEMS))
}

fn format_generic(arguments: &Map<String, Value>, omitted: usize) -> String {
    let mut keys: Vec<&String> = Vec::with_capacity(GENERIC_MAX_ITEMS);
    for key in arguments.keys() {
        let position = keys
            .binary_search_by(|candidate| candidate.as_str().cmp(key.as_str()))
            .unwrap_or_else(|position| position);
        if position < GENERIC_MAX_ITEMS {
            keys.insert(position, key);
            if keys.len() > GENERIC_MAX_ITEMS {
                keys.pop();
            }
        }
    }
    let mut parts = Vec::new();
    for key in keys {
        let value = arguments.get(key).expect("key came from map");
        parts.push(format!(
            "{}={}",
            clip_chars(&one_line(key), 32),
            clip_chars(&scalar_value(value), GENERIC_VALUE_MAX_CHARS)
        ));
    }
    if omitted > 0 {
        let marker = format!("… +{omitted} fields");
        while !parts.is_empty()
            && parts.join(" ").chars().count() + 1 + marker.chars().count()
                > GENERIC_ACTION_MAX_CHARS
        {
            parts.pop();
        }
        parts.push(marker);
    }
    clip_chars(&parts.join(" "), GENERIC_ACTION_MAX_CHARS)
}

fn scalar_value(value: &Value) -> String {
    match value {
        Value::Null => "null".into(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => one_line(value),
        Value::Array(values) => format!("[{} items]", values.len()),
        Value::Object(values) => format!("{{{} fields}}", values.len()),
    }
}

fn path_value(arguments: &Map<String, Value>, key: &str, default: &str) -> String {
    middle_clip_chars(
        &one_line(&string_value(arguments, key, default)),
        PATH_MAX_CHARS,
    )
}

fn string_value(arguments: &Map<String, Value>, key: &str, default: &str) -> String {
    arguments
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or(default)
        .to_owned()
}

fn positive_u64(value: Option<&Value>) -> Option<u64> {
    value.and_then(Value::as_u64).filter(|value| *value > 0)
}

fn bounded_reason(reason: &str) -> String {
    clip_utf8_bytes(&normalize_newlines(reason), REASON_MAX_BYTES)
}

fn normalize_newlines(source: &str) -> String {
    source.replace("\r\n", "\n").replace('\r', "\n")
}

fn pad_chip_label(mut label: String) -> String {
    let padding = CHIP_LABEL_CHARS.saturating_sub(label.chars().count());
    label.push_str(&" ".repeat(padding));
    label
}

fn one_line(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub fn bounded_tool_name(value: &str) -> String {
    if value.is_empty() {
        return "(unnamed)".to_owned();
    }
    clip_chars(value, TOOL_NAME_MAX_CHARS)
}

fn clip_chars(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        return value.to_owned();
    }
    let keep = max_chars.saturating_sub(1);
    let mut output = value.chars().take(keep).collect::<String>();
    output.push('…');
    output
}

fn middle_clip_chars(value: &str, max_chars: usize) -> String {
    let count = value.chars().count();
    if count <= max_chars {
        return value.to_owned();
    }
    let left = max_chars.saturating_sub(1) / 2;
    let right = max_chars.saturating_sub(1).saturating_sub(left);
    let prefix = value.chars().take(left).collect::<String>();
    let suffix = value
        .chars()
        .skip(count.saturating_sub(right))
        .collect::<String>();
    format!("{prefix}…{suffix}")
}

fn clip_utf8_bytes(value: &str, max_bytes: usize) -> String {
    if value.len() <= max_bytes {
        return value.to_owned();
    }
    let mut end = max_bytes;
    while end > 0 && !value.is_char_boundary(end) {
        end -= 1;
    }
    value[..end].to_owned()
}

#[derive(Clone, Copy)]
enum Retention {
    Head,
    Tail,
}

fn bounded_text(
    source: &str,
    max_bytes: usize,
    max_lines: usize,
    retention: Retention,
) -> BoundedText {
    let source_bytes = u64::try_from(source.len()).unwrap_or(u64::MAX);
    let source_lines = logical_line_count(source);
    let (start, end) = match retention {
        Retention::Head => head_bounds(source, max_bytes, max_lines),
        Retention::Tail => tail_bounds(source, max_bytes, max_lines),
    };
    let text = source[start..end].to_owned();
    let retained_lines = logical_line_count(&text);
    BoundedText {
        text,
        source_bytes,
        source_lines,
        dropped_bytes: source_bytes.saturating_sub(u64::try_from(end - start).unwrap_or(u64::MAX)),
        dropped_lines: source_lines.saturating_sub(retained_lines),
        base_offset: u64::try_from(start).unwrap_or(u64::MAX),
    }
}

fn head_bounds(source: &str, max_bytes: usize, max_lines: usize) -> (usize, usize) {
    if max_lines == 0 {
        return (0, 0);
    }
    if source.len() <= max_bytes && logical_line_count(source) <= max_lines as u64 {
        return (0, source.len());
    }
    let mut end = source.len().min(max_bytes);
    while end > 0 && !source.is_char_boundary(end) {
        end -= 1;
    }
    let mut lines = 1_usize;
    for (index, byte) in source.as_bytes()[..end].iter().enumerate() {
        if *byte == b'\n' {
            if lines == max_lines {
                end = index;
                break;
            }
            lines += 1;
        }
    }
    (0, end)
}

fn tail_bounds(source: &str, max_bytes: usize, max_lines: usize) -> (usize, usize) {
    if max_lines == 0 {
        return (source.len(), source.len());
    }
    if source.len() <= max_bytes && logical_line_count(source) <= max_lines as u64 {
        return (0, source.len());
    }
    let mut start = source.len().saturating_sub(max_bytes);
    while start < source.len() && !source.is_char_boundary(start) {
        start += 1;
    }
    let mut lines = 1_usize;
    for index in (start..source.len()).rev() {
        if source.as_bytes()[index] == b'\n' {
            if index + 1 == source.len() {
                continue;
            }
            if lines == max_lines {
                start = index + 1;
                break;
            }
            lines += 1;
        }
    }
    (start, source.len())
}

pub(crate) fn logical_line_count(source: &str) -> u64 {
    if source.is_empty() {
        return 0;
    }
    u64::try_from(
        source
            .as_bytes()
            .iter()
            .filter(|byte| **byte == b'\n')
            .count(),
    )
    .unwrap_or(u64::MAX)
    .saturating_add(u64::from(!source.ends_with('\n')))
}

/// A Bash command as its action line shows it when it runs in `cwd`: a leading
/// `cd <cwd> &&` is a no-op there and is dropped, and `<cwd>/` path prefixes become
/// relative. Once the command changes folder (`cd`, `pushd`), relative paths would
/// read against the wrong folder, so the rest is shown as written.
fn workspace_relative_command(command: &str, cwd: &str) -> String {
    let (start, rest) = command
        .strip_prefix("start ")
        .map_or(("", command), |rest| ("start ", rest));
    let rest = strip_redundant_cd(rest, cwd).unwrap_or(rest);
    let changes_folder = rest
        .split(|character: char| {
            character.is_whitespace() || matches!(character, ';' | '&' | '|' | '(')
        })
        .any(|word| matches!(word, "cd" | "pushd" | "popd"));
    if changes_folder {
        return format!("{start}{rest}");
    }
    let prefix = format!("{cwd}/");
    let mut shown = String::with_capacity(rest.len());
    let mut remaining = rest;
    while let Some(index) = remaining.find(&prefix) {
        // Only a path that starts a word is under `cwd`; `/x/a/b` is not `a/b` when
        // `cwd` is `/a`.
        let starts_word = remaining[..index]
            .chars()
            .next_back()
            .is_none_or(|before| before.is_whitespace() || matches!(before, '\'' | '"' | '='));
        shown.push_str(&remaining[..index]);
        remaining = &remaining[index + prefix.len()..];
        if !starts_word {
            shown.push_str(&prefix);
        } else if remaining
            .chars()
            .next()
            .is_none_or(|after| after.is_whitespace() || matches!(after, '\'' | '"'))
        {
            // The folder itself, written with a trailing slash.
            shown.push('.');
        }
    }
    shown.push_str(remaining);
    format!("{start}{shown}")
}

/// The command after a leading `cd <cwd> && `, when the target is exactly `cwd`
/// (quoted or with a trailing slash), or None.
fn strip_redundant_cd<'a>(command: &'a str, cwd: &str) -> Option<&'a str> {
    let after_cd = command.strip_prefix("cd ")?;
    let (target, rest) = after_cd.split_once(" && ")?;
    let target = target.trim();
    let unquoted = ['\'', '"']
        .into_iter()
        .find_map(|quote| target.strip_prefix(quote)?.strip_suffix(quote))
        .unwrap_or(target);
    let unquoted = unquoted.strip_suffix('/').unwrap_or(unquoted);
    (unquoted == cwd).then_some(rest)
}

/// Format a duration compactly: `<0.1s`, `0.2s`, `4.5s`, `12s`, `4m 12s`, `1h 05m`.
/// Each unit is chosen after rounding, so 9.96 s reads `10s` rather than `10.0s`.
pub(crate) fn format_duration(milliseconds: u64) -> String {
    if milliseconds < 100 {
        return "<0.1s".into();
    }
    let tenths = milliseconds.saturating_add(50) / 100;
    if tenths < 100 {
        return format!("{}.{}s", tenths / 10, tenths % 10);
    }
    let seconds = milliseconds.saturating_add(500) / 1000;
    if seconds < 60 {
        return format!("{seconds}s");
    }
    if seconds < 3600 {
        return format!("{}m {:02}s", seconds / 60, seconds % 60);
    }
    let minutes = milliseconds.saturating_add(30_000) / 60_000;
    format!("{}h {:02}m", minutes / 60, minutes % 60)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redundant_cd_is_hidden() {
        let cwd = "/Users/ana/Wisp";
        let shown = |command: &str| workspace_relative_command(command, cwd);
        assert_eq!(shown("cd /Users/ana/Wisp && cargo test"), "cargo test");
        assert_eq!(shown("cd '/Users/ana/Wisp/' && git status"), "git status");
        assert_eq!(
            shown("start cd /Users/ana/Wisp && npm run dev"),
            "start npm run dev"
        );
        // A cd that changes folder stays, and paths after it are shown as written.
        assert_eq!(
            shown("cd /Users/ana/Wisp/rust && cargo test"),
            "cd /Users/ana/Wisp/rust && cargo test"
        );
        assert_eq!(
            shown("cd rust && cat /Users/ana/Wisp/README.md"),
            "cd rust && cat /Users/ana/Wisp/README.md"
        );
        assert_eq!(
            shown("cd /Users/ana/Wisp && cd rust && ls /Users/ana/Wisp/a"),
            "cd rust && ls /Users/ana/Wisp/a"
        );
        assert_eq!(shown("cd .. && ls"), "cd .. && ls");
        assert_eq!(
            shown("cd /Users/ana/Wispy && ls"),
            "cd /Users/ana/Wispy && ls"
        );
    }

    #[test]
    fn search_paths_become_relative() {
        let cwd = std::env::current_dir().unwrap().display().to_string();
        let card = |path: String| {
            ToolCardSnapshot::requested(
                &ToolCallInput {
                    call_id: "c".into(),
                    name: "grep".into(),
                    arguments: serde_json::json!({"pattern": "todo", "path": path}),
                    detail_source: ToolDetailSource::None,
                },
                ToolStatus::Requested,
            )
        };
        assert_eq!(
            card(format!("{cwd}/src")).display_arguments(),
            "/todo/ in src"
        );
        assert_eq!(card(cwd.clone()).display_arguments(), "/todo/ in .");
        assert_eq!(
            card("/elsewhere".into()).display_arguments(),
            "/todo/ in /elsewhere"
        );
    }

    #[test]
    fn command_paths_become_relative() {
        let cwd = "/Users/ana/Wisp";
        let shown = |command: &str| workspace_relative_command(command, cwd);
        assert_eq!(
            shown("cat /Users/ana/Wisp/src/a.rs \"/Users/ana/Wisp/b c\""),
            "cat src/a.rs \"b c\""
        );
        assert_eq!(
            shown("ls /mnt/Users/ana/Wisp/x"),
            "ls /mnt/Users/ana/Wisp/x"
        );
        assert_eq!(shown("ls /Users/ana/Wisp"), "ls /Users/ana/Wisp");
        assert_eq!(shown("rm -rf /Users/ana/Wisp/"), "rm -rf .");
        assert_eq!(
            shown("make --prefix=/Users/ana/Wisp/ x"),
            "make --prefix=. x"
        );
    }

    #[test]
    fn duration_formats_compactly() {
        let cases = [
            (40, "<0.1s"),
            (250, "0.3s"),
            (9_960, "10s"),
            (59_600, "1m 00s"),
            (4_500, "4.5s"),
            (12_400, "12s"),
            (252_000, "4m 12s"),
            (3_900_000, "1h 05m"),
        ];
        for (milliseconds, label) in cases {
            assert_eq!(format_duration(milliseconds), label);
        }
    }

    #[test]
    fn command_summaries_only_label_known_invocations() {
        assert_eq!(
            summarize_command("cd /project && uv run pytest -q")
                .unwrap()
                .label(),
            "Python tests"
        );
        assert_eq!(
            summarize_command("uv run ruff check . && uv run mypy")
                .unwrap()
                .label(),
            "Python quality checks"
        );
        for command in [
            "echo pytest",
            "cargo test && rm -rf cache",
            "pytest; deploy",
            "start cargo test",
        ] {
            assert!(summarize_command(command).is_none());
        }
    }

    #[test]
    fn command_summaries_do_not_hide_shell_separators_or_clipped_suffixes() {
        for command in [
            "pytest & deploy".to_owned(),
            "pytest\ndeploy".into(),
            "pytest && deploy".into(),
            format!("pytest {} ; deploy", "x".repeat(300)),
        ] {
            let input = ToolCallInput {
                call_id: "test".into(),
                name: "bash".into(),
                arguments: serde_json::json!({"command": command}),
                detail_source: ToolDetailSource::None,
            };
            let card = ToolCardSnapshot::requested(&input, ToolStatus::Requested);
            assert_eq!(card.display_arguments(), card.action_arguments);
            assert!(card.command_summary.is_none());
        }
    }

    #[test]
    fn historical_merges_keep_known_process_task_labels() {
        let older = ProcessCardSnapshot::new("process-1".into());
        let mut live = older.clone();
        live.task_label = Some("Python tests".into());
        live.prepend_historical_older(&older);
        assert_eq!(live.task_label.as_deref(), Some("Python tests"));
        live.merge_historical_newer(&older);
        assert_eq!(live.task_label.as_deref(), Some("Python tests"));
    }

    fn result(output: &str) -> ToolResultInput {
        ToolResultInput {
            call_id: "call-1".into(),
            name: "read".into(),
            output: output.into(),
            output_tail: None,
            output_source_bytes: output.len() as u64,
            output_source_lines: logical_line_count(output),
            output_projection_cut_mid_line: false,
            is_error: false,
            failure_code: None,
            retryable: false,
            recovery_hint: None,
            exit_code: None,
            output_has_exit_status: false,
            before_text: None,
            created: false,
            summary: None,
            truncated: false,
            process_id: None,
            process_state: None,
            process_error: None,
            stdout: None,
            stdout_source_bytes: 0,
            stderr: None,
            stderr_source_bytes: 0,
            stdout_truncated: false,
            stderr_truncated: false,
            stdout_dropped_bytes: 0,
            stderr_dropped_bytes: 0,
            duration_ms: None,
        }
    }

    #[test]
    fn card_labels_stay_short() {
        let mut transcript = crate::transcript::Transcript::default();
        let call = |name: &str| ToolCallInput {
            call_id: name.into(),
            name: name.into(),
            arguments: serde_json::json!({}),
            detail_source: ToolDetailSource::None,
        };
        for (name, label) in [("bash", "Bash"), ("grep", "Search"), ("find", "Search")] {
            let id = transcript.observe_tool_call(call(name));
            let card = transcript.entry(id).unwrap().tool_card().unwrap();
            assert_eq!(card.action_label(), label);
            assert_eq!(card.chip_label().len(), CHIP_LABEL_CHARS);
            assert_eq!(card.status_note(), None);
        }
        let long = "extension_".repeat(5);
        let id = transcript.observe_approval_requested(call(&long));
        let card = transcript.entry(id).unwrap().tool_card().unwrap();
        assert_eq!(card.action_label().chars().count(), TOOL_LABEL_MAX_CHARS);
        assert_eq!(card.status_note(), Some("awaiting approval"));
    }

    #[test]
    fn mcp_chip_names_its_target() {
        let mut transcript = crate::transcript::Transcript::default();
        let id = transcript.observe_tool_call(ToolCallInput {
            call_id: "mcp".into(),
            name: "mcp__github__create_pull_request".into(),
            arguments: serde_json::json!({"title": "Fix"}),
            detail_source: ToolDetailSource::None,
        });
        let card = transcript.entry(id).unwrap().tool_card().unwrap();
        assert_eq!(card.action_label(), "MCP");
        assert_eq!(
            card.action(),
            "MCP      github/create_pull_request  title=Fix"
        );
    }

    #[test]
    fn argument_formatting_never_retains_write_or_edit_payloads() {
        let huge = "secret".repeat(100_000);
        let write = serde_json::json!({"path": "/tmp/file", "content": huge});
        let edit = serde_json::json!({
            "path": "/tmp/file",
            "edits": [{"oldText": huge, "newText": huge}]
        });

        assert_eq!(format_tool_arguments("write", &write), "/tmp/file");
        assert_eq!(format_tool_arguments("edit", &edit), "/tmp/file");
        assert!(format_tool_arguments("extension", &edit).len() <= GENERIC_ACTION_MAX_CHARS + 3);
    }

    #[test]
    fn bounded_text_respects_utf8_byte_and_line_limits() {
        let source = format!("{}\n{}", "界".repeat(1_000), "tail\n".repeat(20));
        let head = BoundedText::head(&source, 101, 3);
        let tail = BoundedText::tail(&source, 101, 3);

        assert!(head.text.len() <= 101);
        assert!(tail.text.len() <= 101);
        assert!(head.text.is_char_boundary(head.text.len()));
        assert!(tail.text.is_char_boundary(0));
        assert!(logical_line_count(&head.text) <= 3);
        assert!(logical_line_count(&tail.text) <= 3);
        assert_eq!(tail.base_offset, tail.dropped_bytes);

        let exact_lines = "line\n".repeat(TOOL_PREVIEW_MAX_LINES);
        let exact_tail =
            BoundedText::tail(&exact_lines, TOOL_PREVIEW_MAX_BYTES, TOOL_PREVIEW_MAX_LINES);
        assert_eq!(exact_tail.text, exact_lines);
        assert_eq!(exact_tail.base_offset, 0);
        assert_eq!(exact_tail.dropped_bytes, 0);
        assert_eq!(exact_tail.dropped_lines, 0);

        let exact_head =
            BoundedText::head(&exact_lines, TOOL_PREVIEW_MAX_BYTES, TOOL_PREVIEW_MAX_LINES);
        assert_eq!(exact_head.text, exact_lines);
        assert_eq!(exact_head.dropped_bytes, 0);
        assert_eq!(exact_head.dropped_lines, 0);

        let oversized_lines =
            "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\n";
        let oversized_tail = BoundedText::tail(
            oversized_lines,
            TOOL_PREVIEW_MAX_BYTES,
            TOOL_PREVIEW_MAX_LINES,
        );
        assert_eq!(
            oversized_tail.text,
            "line 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\n"
        );
        assert_eq!(oversized_tail.dropped_lines, 1);
        assert_eq!(oversized_tail.dropped_bytes, "line 1\n".len() as u64);
    }

    #[test]
    fn out_of_range_integer_metadata_uses_consistent_number_rounding() {
        let first = ToolCallInput {
            call_id: "large-number".into(),
            name: "extension".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::from_str(r#"{"value":18446744073709551616}"#).unwrap(),
        };
        let second = ToolCallInput {
            arguments: serde_json::from_str(r#"{"value":18446744073709551617}"#).unwrap(),
            ..first.clone()
        };
        let mut card = ToolCardSnapshot::requested(&first, ToolStatus::Requested);
        let original_action = card.action_arguments.clone();

        assert!(card.enrich_call(&second));
        assert_eq!(card.status, ToolStatus::Requested);
        assert_eq!(card.action_arguments, original_action);
    }

    #[test]
    fn generic_action_reserves_a_truthful_omission_marker() {
        let arguments = (0..12)
            .map(|index| (format!("key-{index:02}"), Value::String("x".repeat(64))))
            .collect::<Map<_, _>>();
        let bounded = bounded_tool_arguments("extension", &Value::Object(arguments));
        let card = ToolCardSnapshot::requested(
            &ToolCallInput {
                call_id: "omitted".into(),
                name: "extension".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: bounded,
            },
            ToolStatus::Requested,
        );

        assert!(card.action_arguments.ends_with("… +4 fields"));
        assert!(card.action_arguments.chars().count() <= GENERIC_ACTION_MAX_CHARS);
    }

    #[test]
    fn clipped_generic_key_collisions_are_reported_as_omissions() {
        let prefix = "k".repeat(64);
        let arguments = [
            (format!("{prefix}a"), Value::from(1)),
            (format!("{prefix}b"), Value::from(2)),
        ]
        .into_iter()
        .collect::<Map<_, _>>();
        let bounded = bounded_tool_arguments("extension", &Value::Object(arguments));
        let card = ToolCardSnapshot::requested(
            &ToolCallInput {
                call_id: "collision".into(),
                name: "extension".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: bounded,
            },
            ToolStatus::Requested,
        );

        assert!(card.action_arguments.ends_with("… +1 fields"));
    }

    #[test]
    fn tool_cards_are_monotonic_and_results_are_bounded() {
        let input = ToolCallInput {
            call_id: "call-1".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"path": "README.md"}),
        };
        let mut card = ToolCardSnapshot::requested(&input, ToolStatus::Requested);
        assert!(card.approval_requested());
        assert!(card.approval_resolved(true, None));
        let mut completed = result(&"line\n".repeat(20_000));
        completed.summary = Some("Read 20,000 lines".into());
        assert!(card.apply_result(&completed));
        assert_eq!(card.status, ToolStatus::Done);
        assert!(card.retained_output.text.len() <= TOOL_OUTPUT_MAX_BYTES);
        assert_eq!(card.detail, "Read 20,000 lines");
        assert!(!card.approval_resolved(false, Some("late")));
    }

    #[test]
    fn newline_normalization_does_not_report_phantom_omitted_bytes() {
        let input = ToolCallInput {
            call_id: "call-1".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({}),
        };
        let mut card = ToolCardSnapshot::requested(&input, ToolStatus::Requested);
        let mut completed = result("one\r\ntwo\rthree");
        completed.output_source_bytes = completed.output.len() as u64;

        assert!(card.apply_result(&completed));
        assert_eq!(card.retained_output.text, "one\ntwo\nthree");
        assert_eq!(card.retained_output.source_bytes, 13);
        assert_eq!(card.retained_output.dropped_bytes, 0);
    }

    #[test]
    fn delayed_approval_request_does_not_regress_running_state() {
        let input = ToolCallInput {
            call_id: "call-1".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({}),
        };
        let mut card = ToolCardSnapshot::requested(&input, ToolStatus::Requested);
        assert!(card.approval_requested());
        assert!(card.approval_resolved(true, None));

        assert!(!card.approval_requested());
        assert!(!card.approval_resolved(false, Some("late duplicate")));
        assert_eq!(card.status, ToolStatus::Running);
    }

    #[test]
    fn denial_is_not_overwritten_by_the_synthetic_result() {
        let input = ToolCallInput {
            call_id: "call-1".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({}),
        };
        let mut card = ToolCardSnapshot::requested(&input, ToolStatus::AwaitingApproval);
        assert!(card.approval_resolved(false, Some("policy")));
        assert!(!card.apply_result(&result("denied")));
        assert_eq!(card.status, ToolStatus::Denied);
        assert_eq!(card.detail, "policy");
    }

    #[test]
    fn process_calls_are_identified_only_from_poll_or_cancel_arguments() {
        let whitespace = bounded_tool_arguments(
            "bash",
            &serde_json::json!({"operation": "poll", "process_id": "   "}),
        );
        assert!(process_call_identity("bash", &whitespace).is_none());

        let oversized_multibyte_blank = bounded_tool_arguments(
            "bash",
            &serde_json::json!({
                "operation": "poll",
                "process_id": "\u{3000}".repeat(1_500),
            }),
        );
        assert!(process_call_identity("bash", &oversized_multibyte_blank).is_none());

        let control_separator = bounded_tool_arguments(
            "bash",
            &serde_json::json!({
                "operation": "poll",
                "process_id": "\u{001c}\u{001d}\u{001e}\u{001f}",
            }),
        );
        assert!(process_call_identity("bash", &control_separator).is_some());

        assert_eq!(
            process_call_identity(
                "bash",
                &serde_json::json!({"operation": "poll", "process_id": "abc"})
            ),
            Some(ProcessCallIdentity {
                process_id: "abc".into(),
                operation: ProcessOperation::Poll,
            })
        );
        assert!(
            process_call_identity("bash", &serde_json::json!({"operation": "start"})).is_none()
        );
        assert!(process_call_identity("read", &serde_json::json!({"process_id": "abc"})).is_none());
    }

    #[test]
    fn process_errors_and_recovery_are_retained_alongside_stream_output() {
        let mut card = ProcessCardSnapshot::new("process-1".into());
        card.begin(ProcessOperation::Poll, 0);
        let mut failed = result("generated failure envelope");
        failed.name = "bash".into();
        failed.process_id = Some("process-1".into());
        failed.process_state = Some("failed".into());
        failed.process_error = Some("cleanup failed after reading output".into());
        failed.recovery_hint = Some("restart the command".into());
        failed.stdout = Some("buffered stdout".into());

        card.observe(ProcessOperation::Poll, &failed, 0);

        assert!(card.retained_output.text.contains("buffered stdout"));
        assert!(card.retained_output.text.contains("cleanup failed"));
        assert!(card.retained_output.text.contains("restart the command"));
        assert!(
            !card
                .retained_output
                .text
                .contains("generated failure envelope")
        );
    }

    #[test]
    fn silent_structured_polls_do_not_retain_generated_status_envelopes() {
        let mut card = ProcessCardSnapshot::new("process-1".into());
        for sequence in 0..600 {
            card.begin(ProcessOperation::Poll, sequence);
            let mut silent = result("Process process-1 is still running");
            silent.name = "bash".into();
            silent.process_id = Some("process-1".into());
            silent.process_state = Some("running".into());
            card.observe(ProcessOperation::Poll, &silent, sequence);
        }

        assert!(card.retained_output.text.is_empty());
        assert_eq!(card.retained_output.source_bytes, 0);
        assert_eq!(card.retained_output.source_lines, 0);
    }

    #[test]
    fn process_fallback_output_accounts_for_projection_omissions() {
        let mut card = ProcessCardSnapshot::new("process".into());
        assert!(card.begin(ProcessOperation::Poll, 1));
        let mut input = result("retained head");
        input.name = "bash".into();
        input.output_tail = Some("retained tail".into());
        input.output_source_bytes = 100;

        assert!(card.observe(ProcessOperation::Poll, &input, 1));
        assert_eq!(card.backend_dropped_bytes, 87);
        assert_eq!(card.retained_output.text, "retained tail");
    }

    #[test]
    fn process_lifecycle_coalesces_output_with_a_bounded_tail() {
        let mut card = ProcessCardSnapshot::new("process-1".into());
        card.begin(ProcessOperation::Poll, 0);
        let mut first = result("");
        first.name = "bash".into();
        first.process_id = Some("process-1".into());
        first.process_state = Some("running".into());
        first.stdout = Some("first".into());
        card.observe(ProcessOperation::Poll, &first, 0);
        assert_eq!(card.display_state, ProcessDisplayState::Running);
        assert!(card.retained_output.text.contains("stdout:\nfirst"));

        card.begin(ProcessOperation::Poll, 1);
        let mut second = first.clone();
        second.stdout = Some("x\n".repeat(100_000));
        second.process_state = Some("completed".into());
        card.observe(ProcessOperation::Poll, &second, 1);

        assert_eq!(card.display_state, ProcessDisplayState::Completed);
        assert_eq!(card.call_count, 2);
        assert_eq!(card.poll_count, 2);
        assert!(card.retained_output.text.len() <= TOOL_OUTPUT_MAX_BYTES);
        assert!(logical_line_count(&card.retained_output.text) <= TOOL_OUTPUT_MAX_LINES as u64);
        assert!(card.retained_output.base_offset > 0);
    }

    #[test]
    fn process_stream_labels_reappear_after_tail_eviction_and_blank_chunks_are_visible() {
        let mut card = ProcessCardSnapshot::new("process-1".into());
        card.append_stream("stdout:", &"x".repeat(TOOL_OUTPUT_MAX_BYTES + 100));
        assert!(!card.retained_output.text.contains("stdout:"));
        assert_eq!(card.last_stream_label, None);

        card.append_stream("stdout:", "next");
        assert!(card.retained_output.text.contains("stdout:\nnext"));
        card.append_stream("stderr:", "\n\n");
        assert!(card.retained_output.text.contains("stderr:"));
    }

    #[test]
    fn merged_process_output_counts_inserted_blank_lines() {
        let mut older = ProcessCardSnapshot::new("process-1".into());
        older.append_unlabelled("\n\n");
        let mut newer = ProcessCardSnapshot::new("process-1".into());
        newer.append_unlabelled("next");

        older.merge_historical_newer(&newer);

        assert_eq!(older.retained_output.text, "\n\nnext");
        assert_eq!(older.retained_output.source_lines, 3);
        assert_eq!(older.retained_output.dropped_lines, 0);
    }

    #[test]
    fn bounded_identity_namespaces_raw_and_hashed_values() {
        let long = "x".repeat(IDENTITY_MAX_BYTES + 1);
        let hashed = bounded_identity(&long);
        let raw_lookalike = bounded_identity(&hashed);

        assert!(hashed.starts_with("h:"));
        assert!(raw_lookalike.starts_with('r'));
        assert_ne!(hashed, raw_lookalike);
        assert_eq!(identity_for_display(&bounded_identity("call-1")), "call-1");
    }

    #[test]
    fn completed_process_with_failed_exit_is_terminal_failure() {
        let mut card = ProcessCardSnapshot::new("process-1".into());
        card.begin(ProcessOperation::Poll, 0);
        let mut failed = result("failed");
        failed.name = "bash".into();
        failed.exit_code = Some(2);
        failed.process_id = Some("process-1".into());
        failed.process_state = Some("completed".into());

        assert!(card.observe(ProcessOperation::Poll, &failed, 0));
        assert_eq!(card.display_state, ProcessDisplayState::Failed);
        assert!(card.display_state.terminal());
        assert_eq!(card.exit_code, Some(2));
    }

    #[test]
    fn running_process_has_no_exit() {
        let mut card = ProcessCardSnapshot::new("process-1".into());
        card.begin(ProcessOperation::Poll, 0);
        let mut running = result("partial");
        running.name = "bash".into();
        running.exit_code = Some(0);
        running.process_id = Some("process-1".into());
        running.process_state = Some("running".into());

        assert!(card.observe(ProcessOperation::Poll, &running, 0));
        assert_eq!(card.exit_code, None);
    }

    #[test]
    fn result_status_matches_shared_python_semantics() {
        let mut value = result("ok");
        assert_eq!(tool_result_status(&value), ToolStatus::Done);
        value.exit_code = Some(2);
        assert_eq!(tool_result_status(&value), ToolStatus::Error);
        value.exit_code = None;
        value.process_state = Some("cancelled".into());
        assert_eq!(tool_result_status(&value), ToolStatus::Cancelled);
    }
}
