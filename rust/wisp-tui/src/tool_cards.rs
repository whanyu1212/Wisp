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
    pub status: ToolStatus,
    pub arguments_available: bool,
    pub detail: String,
    pub retained_output: BoundedText,
    pub backend_truncated: bool,
    pub failure_code: Option<String>,
    pub retryable: bool,
    pub detail_source: ToolDetailSource,
    pub structured_detail: DetailAvailability,
}

impl ToolCardSnapshot {
    pub fn requested(input: &ToolCallInput, status: ToolStatus) -> Self {
        Self {
            call_id: call_identity_for_display(&input.call_id),
            name: bounded_tool_name(&input.name),
            action_arguments: format_tool_arguments(&input.name, &input.arguments),
            status,
            arguments_available: true,
            detail: String::new(),
            retained_output: BoundedText::default(),
            backend_truncated: false,
            failure_code: None,
            retryable: false,
            detail_source: input.detail_source.clone(),
            structured_detail: DetailAvailability::None,
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

    pub fn action(&self) -> String {
        let verb = action_verb(&self.name, self.status);
        let mut action = if known_tool(&self.name) {
            verb.to_owned()
        } else {
            format!("{verb} {}", self.name)
        };
        if !self.arguments_available {
            action.push_str("  (arguments unavailable)");
        } else if !self.action_arguments.is_empty() {
            action.push_str("  ");
            action.push_str(&self.action_arguments);
        }
        action
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
    pub display_state: ProcessDisplayState,
    pub poll_count: u32,
    pub call_count: u32,
    pub retained_output: BoundedText,
    pub backend_truncated: bool,
    pub backend_dropped_bytes: u64,
    last_stream_label: Option<&'static str>,
    last_sequence: Option<u64>,
    approval_sequence: Option<u64>,
    approval_resolved: bool,
}

impl ProcessCardSnapshot {
    pub fn new(process_id: String) -> Self {
        Self {
            process_id,
            display_state: ProcessDisplayState::Observed,
            poll_count: 0,
            call_count: 0,
            retained_output: BoundedText::default(),
            backend_truncated: false,
            backend_dropped_bytes: 0,
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

    pub(crate) fn merge_historical_newer(&mut self, newer: &Self) {
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

        self.display_state = newer.display_state;
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
        self.last_sequence = newer.last_sequence;
        self.approval_sequence = None;
        self.approval_resolved = false;
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

    pub fn action(&self) -> String {
        let state = match self.display_state {
            ProcessDisplayState::PollAwaitingApproval => "Process poll awaiting approval",
            ProcessDisplayState::CancelAwaitingApproval => "Process cancellation awaiting approval",
            ProcessDisplayState::Polling => "Polling process",
            ProcessDisplayState::Cancelling => "Cancelling process",
            ProcessDisplayState::Running => "Running process",
            ProcessDisplayState::Completed => "Process completed",
            ProcessDisplayState::Failed => "Process failed",
            ProcessDisplayState::TimedOut => "Process timed out",
            ProcessDisplayState::Cancelled => "Process cancelled",
            ProcessDisplayState::PollDenied => "Process poll denied",
            ProcessDisplayState::CancelDenied => "Process cancellation denied",
            ProcessDisplayState::PollInterrupted => "Process poll interrupted",
            ProcessDisplayState::CancelInterrupted => "Process cancellation interrupted",
            ProcessDisplayState::PollFailed => "Process poll failed",
            ProcessDisplayState::CancelFailed => "Process cancellation failed",
            ProcessDisplayState::Observed => "Observed process",
        };
        format!(
            "{state}  {} · {} call{} · {} poll{}",
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

fn action_verb(name: &str, status: ToolStatus) -> &'static str {
    let words = match name {
        "bash" => [
            "Run",
            "Awaiting approval to run",
            "Running",
            "Ran",
            "Failed to run",
            "Denied running",
            "Cancelled running",
        ],
        "read" => [
            "Read",
            "Awaiting approval to read",
            "Reading",
            "Read",
            "Failed to read",
            "Denied reading",
            "Cancelled reading",
        ],
        "grep" | "find" => [
            "Search",
            "Awaiting approval to search",
            "Searching",
            "Searched",
            "Failed to search",
            "Denied searching",
            "Cancelled searching",
        ],
        "ls" => [
            "List",
            "Awaiting approval to list",
            "Listing",
            "Listed",
            "Failed to list",
            "Denied listing",
            "Cancelled listing",
        ],
        "edit" => [
            "Edit",
            "Awaiting approval to edit",
            "Editing",
            "Edited",
            "Failed to edit",
            "Denied editing",
            "Cancelled editing",
        ],
        "write" => [
            "Write",
            "Awaiting approval to write",
            "Writing",
            "Wrote",
            "Failed to write",
            "Denied writing",
            "Cancelled writing",
        ],
        _ => [
            "Call",
            "Awaiting approval to call",
            "Calling",
            "Called",
            "Failed to call",
            "Denied calling",
            "Cancelled calling",
        ],
    };
    words[match status {
        ToolStatus::Requested => 0,
        ToolStatus::AwaitingApproval => 1,
        ToolStatus::Running => 2,
        ToolStatus::Done => 3,
        ToolStatus::Error => 4,
        ToolStatus::Denied => 5,
        ToolStatus::Cancelled => 6,
    }]
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

#[cfg(test)]
mod tests {
    use super::*;

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
        }
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
