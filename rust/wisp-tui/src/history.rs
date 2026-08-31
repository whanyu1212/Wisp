//! Bounded, renderer-neutral projection of one validated RPC history page.

use std::collections::{BTreeSet, VecDeque};

use serde_json::{Map, Value};
use thiserror::Error;

use crate::tool_cards::{
    BoundedText, INTERRUPTED_TOOL_RESULT_TEXT, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES,
    ToolCallInput, ToolResultInput, bounded_identity, bounded_tool_arguments, bounded_tool_name,
    identity_for_display, process_call_identity,
};
use crate::tool_detail::{DetailUnavailableReason, ToolDetailSource, project_tool_detail_source};
use crate::transcript::{SharedTranscript, TranscriptEntryId};

pub const HISTORY_MESSAGE_LIMIT: usize = 200;
pub const HISTORY_PAGE_LIMIT: usize = 75;
const HISTORY_TOOL_CALL_LIMIT: usize = 128;
const HISTORY_ENTRY_ID_MAX_BYTES: usize = 4 * 1024;
const HISTORY_PROCESS_CALL_LIMIT: usize = 1024;
const CONTENT_TRUNCATED_MARKER: &str = "[content truncated]";
const EMPTY_ASSISTANT_MESSAGE: &str = "(empty assistant message)";
const MISSING_TOOL_RESULT: &str = "No persisted tool result.";

#[derive(Debug, Error)]
pub enum HistoryProjectionError {
    #[error("history page has more than {HISTORY_MESSAGE_LIMIT} messages")]
    TooManyMessages,
    #[error("history message {index} has more than {HISTORY_TOOL_CALL_LIMIT} tool calls")]
    TooManyToolCalls { index: usize },
    #[error("history message {index} has an invalid {field} field")]
    InvalidField { index: usize, field: &'static str },
    #[error("history page repeats persisted entry {entry_id:?}")]
    DuplicateEntryId { entry_id: String },
    #[error("history exact-detail response did not contain {entry_id:?}")]
    ExactEntryMismatch { entry_id: String },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProjectedHistoryPage {
    pub transcript: SharedTranscript,
    pub durable_entry_ids: Vec<String>,
}

/// Project exactly one latest-first RPC page, already validated by the protocol layer.
///
/// The RPC backend returns message snapshots in chronological order. This function deliberately
/// owns no cursor, durable-entry map, or paging state: a caller installs the returned transcript
/// atomically once its matching command succeeds.
pub fn project_rpc_messages(
    messages: &[Value],
) -> Result<SharedTranscript, HistoryProjectionError> {
    project_rpc_message_page(messages, false)
}

pub fn project_rpc_message_page(
    messages: &[Value],
    truncated: bool,
) -> Result<SharedTranscript, HistoryProjectionError> {
    Ok(project_rpc_message_page_with_origins(messages, truncated)?.transcript)
}

pub fn project_rpc_message_page_with_origins(
    messages: &[Value],
    _truncated: bool,
) -> Result<ProjectedHistoryPage, HistoryProjectionError> {
    if messages.len() > HISTORY_MESSAGE_LIMIT {
        return Err(HistoryProjectionError::TooManyMessages);
    }

    let mut transcript = SharedTranscript::default();
    let mut durable_entry_ids = Vec::with_capacity(messages.len());
    let mut seen = BTreeSet::new();
    let mut process_ids = VecDeque::new();
    for (index, message) in messages.iter().enumerate() {
        let durable_entry_id = string(message, index, "entry_id")?;
        if durable_entry_id.is_empty() || durable_entry_id.len() > HISTORY_ENTRY_ID_MAX_BYTES {
            return Err(HistoryProjectionError::InvalidField {
                index,
                field: "entry_id",
            });
        }
        if !seen.insert(durable_entry_id) {
            return Err(HistoryProjectionError::DuplicateEntryId {
                entry_id: durable_entry_id.to_owned(),
            });
        }
        let start = transcript.entries().len();
        let mut assistant_entries = Vec::new();
        let tool_result_entry = match string(message, index, "role")? {
            "system" => None,
            "user" => {
                transcript.append_prompt(user_content(message, index)?);
                None
            }
            "assistant" => {
                assistant_entries =
                    project_assistant(&mut transcript, &mut process_ids, message, index)?;
                None
            }
            "tool" => Some(project_tool_result(
                &mut transcript,
                &mut process_ids,
                message,
                index,
            )?),
            _ => {
                return Err(HistoryProjectionError::InvalidField {
                    index,
                    field: "role",
                });
            }
        };
        transcript.mark_history_entries(start, durable_entry_id);
        for entry_id in assistant_entries {
            transcript.add_history_origin(entry_id, durable_entry_id);
        }
        if let Some((entry_id, pending_result)) = tool_result_entry {
            transcript.add_history_origin(entry_id, durable_entry_id);
            transcript.mark_history_result_projection(
                entry_id,
                bool(message, index, "content_truncated")?,
            );
            if let Some(result) = pending_result {
                transcript.record_history_pending_result(entry_id, result);
            }
        }
        durable_entry_ids.push(durable_entry_id.to_owned());
    }
    transcript.settle_unresolved_tools(MISSING_TOOL_RESULT);
    transcript.complete_history_entries();
    Ok(ProjectedHistoryPage {
        transcript,
        durable_entry_ids,
    })
}

pub fn project_rpc_exact_tool_result(
    messages: &[Value],
    expected_entry_id: &str,
) -> Result<ToolResultInput, HistoryProjectionError> {
    let [message] = messages else {
        return Err(HistoryProjectionError::ExactEntryMismatch {
            entry_id: expected_entry_id.to_owned(),
        });
    };
    if string(message, 0, "entry_id")? != expected_entry_id || string(message, 0, "role")? != "tool"
    {
        return Err(HistoryProjectionError::ExactEntryMismatch {
            entry_id: expected_entry_id.to_owned(),
        });
    }
    tool_result(message, 0)
}

fn project_assistant(
    transcript: &mut SharedTranscript,
    process_ids: &mut VecDeque<(String, String)>,
    message: &Value,
    index: usize,
) -> Result<Vec<TranscriptEntryId>, HistoryProjectionError> {
    let content = content_for_history(message, index)?;
    let tool_calls = array(message, index, "tool_calls")?;
    let original_count = message
        .get("tool_calls_original_count")
        .and_then(Value::as_u64)
        .unwrap_or_else(|| u64::try_from(tool_calls.len()).unwrap_or(u64::MAX));
    let tool_calls_truncated = message
        .get("tool_calls_truncated")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if tool_calls.len() > HISTORY_TOOL_CALL_LIMIT
        || tool_calls_truncated
        || original_count > u64::try_from(tool_calls.len()).unwrap_or(u64::MAX)
    {
        return Err(HistoryProjectionError::TooManyToolCalls { index });
    }
    if !content.is_empty() {
        transcript.complete_message(turn(index), content);
    } else if tool_calls.is_empty() {
        transcript.complete_message(turn(index), EMPTY_ASSISTANT_MESSAGE.into());
    }
    let mut entries = Vec::with_capacity(tool_calls.len());
    for tool_call in tool_calls {
        let tool_call = tool_call_input(tool_call, index)?;
        if let Some(process) = process_call_identity(&tool_call.name, &tool_call.arguments) {
            process_ids.retain(|(call_id, _)| call_id != &tool_call.call_id);
            if process_ids.len() == HISTORY_PROCESS_CALL_LIMIT {
                process_ids.pop_front();
            }
            process_ids.push_back((tool_call.call_id.clone(), process.process_id));
        }
        let call_id = tool_call.call_id.clone();
        let entry_id = transcript.observe_tool_call(tool_call);
        transcript.record_history_call(entry_id, &call_id);
        entries.push(entry_id);
    }
    Ok(entries)
}

fn user_content(message: &Value, index: usize) -> Result<String, HistoryProjectionError> {
    let Some(invocation) = message
        .get("skill_invocation")
        .filter(|value| !value.is_null())
    else {
        return content_for_history(message, index);
    };
    let Some(invocation) = invocation.as_object() else {
        return Err(HistoryProjectionError::InvalidField {
            index,
            field: "skill_invocation",
        });
    };
    let name = BoundedText::head(
        object_string(invocation, index, "skill_invocation.name")?,
        128,
        1,
    )
    .text;
    let bounded_request = BoundedText::head(
        object_string(invocation, index, "skill_invocation.request")?,
        TOOL_OUTPUT_MAX_BYTES,
        TOOL_OUTPUT_MAX_LINES,
    );
    let request = bounded_request.text;
    let request_truncated = object_bool(invocation, index, "skill_invocation.request_truncated")?
        || bounded_request.dropped_bytes > 0
        || bounded_request.dropped_lines > 0;
    let instructions_truncated =
        object_bool(invocation, index, "skill_invocation.instructions_truncated")?;
    let request = request.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut content = format!("skill /skill:{name}");
    if !request.is_empty() {
        content.push(' ');
        content.push_str(&request);
    }
    if request_truncated {
        content.push_str(" [request truncated]");
    }
    if instructions_truncated {
        content.push_str(" [instructions truncated]");
    }
    Ok(bounded_content(&content, false))
}

fn tool_call_input(value: &Value, index: usize) -> Result<ToolCallInput, HistoryProjectionError> {
    let name = string(value, index, "name")?;
    let arguments = value
        .get("arguments")
        .ok_or(HistoryProjectionError::InvalidField {
            index,
            field: "tool_call.arguments",
        })?;
    let object = arguments
        .as_object()
        .ok_or(HistoryProjectionError::InvalidField {
            index,
            field: "tool_call.arguments",
        })?;
    let arguments_incomplete = value
        .get("arguments_truncated")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        || value
            .get("parse_error")
            .is_some_and(|error| !error.is_null());
    Ok(ToolCallInput {
        call_id: bounded_identity(string(value, index, "call_id")?),
        detail_source: if arguments_incomplete {
            ToolDetailSource::Unavailable(DetailUnavailableReason::MalformedSource)
        } else {
            project_tool_detail_source(name, object)
        },
        arguments: bounded_tool_arguments(name, arguments),
        name: bounded_tool_name(name),
    })
}

fn project_tool_result(
    transcript: &mut SharedTranscript,
    process_ids: &mut VecDeque<(String, String)>,
    message: &Value,
    index: usize,
) -> Result<(TranscriptEntryId, Option<ToolResultInput>), HistoryProjectionError> {
    let mut result = tool_result(message, index)?;
    let request_missing = !transcript.has_unresolved_tool_call(&result.call_id);
    result.process_id = process_ids
        .iter()
        .position(|(call_id, _)| call_id == &result.call_id)
        .and_then(|position| {
            process_ids
                .remove(position)
                .map(|(_, process_id)| process_id)
        });
    if let Some(process_id) = result.process_id.clone() {
        project_historical_process_result(&mut result, &process_id);
    }
    if tool_result_status(message) == Some("denied") {
        if request_missing {
            transcript.observe_tool_call(ToolCallInput {
                call_id: result.call_id.clone(),
                name: result.name.clone(),
                arguments: Value::Object(Map::new()),
                detail_source: ToolDetailSource::None,
            });
        }
        transcript.observe_approval_resolved(&result.call_id, false, Some("denied"));
    }
    let pending_result = request_missing.then(|| result.clone());
    let call_id = result.call_id.clone();
    let entry_id = transcript.observe_tool_result(result);
    if !request_missing {
        transcript.resolve_history_call(entry_id, &call_id);
    }
    Ok((entry_id, pending_result))
}

fn tool_result(message: &Value, index: usize) -> Result<ToolResultInput, HistoryProjectionError> {
    let name = optional_string(message, index, "tool_name")?.unwrap_or("unknown");
    let result = message
        .get("tool_result")
        .filter(|value| !value.is_null())
        .and_then(Value::as_object);
    let status = result
        .and_then(|result| result.get("status"))
        .and_then(Value::as_str);
    let persisted_is_error = optional_bool(message, index, "is_error")?.unwrap_or(false);
    let legacy_interrupted = status.is_none()
        && persisted_is_error
        && string(message, index, "content")? == INTERRUPTED_TOOL_RESULT_TEXT;
    let is_error = persisted_is_error || matches!(status, Some("error" | "denied"));
    let output = content_for_history(message, index)?;
    let raw_before_text = result
        .and_then(|result| result.get("before_text"))
        .map(|value| optional_value_string(value, index, "tool_result.before_text"))
        .transpose()?
        .flatten();
    let (before_text, before_text_locally_truncated) =
        raw_before_text.map_or((None, false), |value| {
            let retained = BoundedText::head(value, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES);
            let truncated = retained.dropped_bytes > 0 || retained.dropped_lines > 0;
            ((!truncated).then_some(retained.text), truncated)
        });
    let summary = result
        .and_then(|result| result.get("summary"))
        .map(|value| optional_value_string(value, index, "tool_result.summary"))
        .transpose()?
        .flatten()
        .map(|value| BoundedText::head(value, 512, 8).text);
    let exit_code = result
        .and_then(|result| result.get("exit_code"))
        .map(|value| optional_i64(value, index, "tool_result.exit_code"))
        .transpose()?
        .flatten();
    let call_id = optional_string(message, index, "tool_call_id")?
        .map(bounded_identity)
        .unwrap_or_else(|| format!("s:history-result-boundary-{index}"));
    Ok(ToolResultInput {
        call_id,
        name: bounded_tool_name(name),
        output_source_bytes: source_count(message, "content_original_bytes", output.len()),
        output_source_lines: BoundedText::head(
            &output,
            TOOL_OUTPUT_MAX_BYTES,
            TOOL_OUTPUT_MAX_LINES,
        )
        .source_lines,
        output_projection_cut_mid_line: false,
        output,
        output_tail: None,
        is_error,
        failure_code: None,
        retryable: false,
        recovery_hint: None,
        exit_code,
        output_has_exit_status: result
            .and_then(|result| result.get("output_has_exit_status"))
            .and_then(Value::as_bool)
            .unwrap_or(false),
        before_text,
        created: result
            .and_then(|result| result.get("created"))
            .and_then(Value::as_bool)
            .unwrap_or(false),
        summary,
        truncated: result
            .and_then(|result| result.get("truncated"))
            .and_then(Value::as_bool)
            .unwrap_or(false)
            || bool(message, index, "content_truncated")?
            || before_text_locally_truncated,
        process_id: None,
        process_state: (status == Some("cancelled") || legacy_interrupted)
            .then(|| "cancelled".into()),
        process_error: None,
        stdout: None,
        stdout_source_bytes: 0,
        stderr: None,
        stderr_source_bytes: 0,
        stdout_truncated: false,
        stderr_truncated: false,
        stdout_dropped_bytes: 0,
        stderr_dropped_bytes: 0,
    })
}

fn tool_result_status(message: &Value) -> Option<&str> {
    message
        .get("tool_result")
        .and_then(Value::as_object)
        .and_then(|result| result.get("status"))
        .and_then(Value::as_str)
}

pub(crate) fn project_historical_process_result(result: &mut ToolResultInput, process_id: &str) {
    if result.process_state.as_deref() == Some("cancelled")
        && result.output != INTERRUPTED_TOOL_RESULT_TEXT
    {
        result.process_state = None;
    }
    let normalized = result.output.replace("\r\n", "\n").replace('\r', "\n");
    let (header, remainder) = normalized
        .split_once('\n')
        .map_or((normalized.as_str(), ""), |(header, remainder)| {
            (header, remainder)
        });
    let prefix = format!("Process {}", identity_for_display(process_id));
    let (state, exit_code, process_error) = if header == format!("{prefix} is still running") {
        ("running", None, None)
    } else if let Some(exit_code) =
        header.strip_prefix(&format!("{prefix} completed with exit code "))
    {
        let Ok(parsed) = exit_code.parse::<i64>() else {
            return;
        };
        if parsed.to_string() != exit_code {
            return;
        }
        ("completed", Some(parsed), None)
    } else if header == format!("{prefix} timed out") {
        ("timed_out", None, None)
    } else if header == format!("{prefix} cancelled") {
        ("cancelled", None, None)
    } else if header == format!("{prefix} failed") {
        ("failed", None, None)
    } else if let Some(error) = header.strip_prefix(&format!("{prefix} failed: ")) {
        ("failed", None, Some(error.to_owned()))
    } else {
        return;
    };
    let Some((stdout, stderr)) = historical_process_streams(remainder) else {
        return;
    };
    result.process_state = Some(state.into());
    if let Some(exit_code) = exit_code {
        result.exit_code = Some(exit_code);
    }
    result.process_error = process_error;
    result.stdout = stdout.map(str::to_owned);
    result.stdout_source_bytes = stdout
        .map(|output| u64::try_from(output.len()).unwrap_or(u64::MAX))
        .unwrap_or(0);
    result.stderr = stderr.map(str::to_owned);
    result.stderr_source_bytes = stderr
        .map(|output| u64::try_from(output.len()).unwrap_or(u64::MAX))
        .unwrap_or(0);
}

fn historical_process_streams(output: &str) -> Option<(Option<&str>, Option<&str>)> {
    if output.is_empty() {
        return Some((None, None));
    }
    if let Some(stdout_and_stderr) = output.strip_prefix("stdout:\n") {
        if let Some((stdout, stderr)) = stdout_and_stderr.split_once("\nstderr:\n") {
            return Some((Some(stdout), Some(stderr)));
        }
        return Some((Some(stdout_and_stderr), None));
    }
    output
        .strip_prefix("stderr:\n")
        .map(|stderr| (None, Some(stderr)))
}

fn content_for_history(message: &Value, index: usize) -> Result<String, HistoryProjectionError> {
    Ok(bounded_content(
        string(message, index, "content")?,
        bool(message, index, "content_truncated")?,
    ))
}

fn bounded_content(source: &str, backend_truncated: bool) -> String {
    let retained = BoundedText::head(source, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES);
    let truncated = backend_truncated || retained.dropped_bytes > 0 || retained.dropped_lines > 0;
    if !truncated {
        return retained.text;
    }
    let separator = if retained.text.is_empty() || retained.text.ends_with('\n') {
        ""
    } else {
        "\n"
    };
    let suffix = format!("{separator}{CONTENT_TRUNCATED_MARKER}");
    let mut output = retained.text;
    while output.len().saturating_add(suffix.len()) > TOOL_OUTPUT_MAX_BYTES {
        output.pop();
    }
    output.push_str(&suffix);
    output
}

fn source_count(message: &Value, field: &'static str, fallback: usize) -> u64 {
    message
        .get(field)
        .and_then(Value::as_u64)
        .unwrap_or_else(|| u64::try_from(fallback).unwrap_or(u64::MAX))
}

fn turn(index: usize) -> u64 {
    u64::try_from(index).unwrap_or(u64::MAX).saturating_add(1)
}

fn array<'a>(
    value: &'a Value,
    index: usize,
    field: &'static str,
) -> Result<&'a [Value], HistoryProjectionError> {
    value
        .get(field)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or(HistoryProjectionError::InvalidField { index, field })
}

fn string<'a>(
    value: &'a Value,
    index: usize,
    field: &'static str,
) -> Result<&'a str, HistoryProjectionError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or(HistoryProjectionError::InvalidField { index, field })
}

fn optional_string<'a>(
    value: &'a Value,
    index: usize,
    field: &'static str,
) -> Result<Option<&'a str>, HistoryProjectionError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value)),
        _ => Err(HistoryProjectionError::InvalidField { index, field }),
    }
}

fn optional_value_string<'a>(
    value: &'a Value,
    index: usize,
    field: &'static str,
) -> Result<Option<&'a str>, HistoryProjectionError> {
    match value {
        Value::Null => Ok(None),
        Value::String(value) => Ok(Some(value)),
        _ => Err(HistoryProjectionError::InvalidField { index, field }),
    }
}

fn bool(value: &Value, index: usize, field: &'static str) -> Result<bool, HistoryProjectionError> {
    value
        .get(field)
        .and_then(Value::as_bool)
        .ok_or(HistoryProjectionError::InvalidField { index, field })
}

fn optional_bool(
    value: &Value,
    index: usize,
    field: &'static str,
) -> Result<Option<bool>, HistoryProjectionError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Bool(value)) => Ok(Some(*value)),
        _ => Err(HistoryProjectionError::InvalidField { index, field }),
    }
}

fn object_string<'a>(
    object: &'a Map<String, Value>,
    index: usize,
    field: &'static str,
) -> Result<&'a str, HistoryProjectionError> {
    object
        .get(field.rsplit('.').next().expect("field has a member"))
        .and_then(Value::as_str)
        .ok_or(HistoryProjectionError::InvalidField { index, field })
}

fn object_bool(
    object: &Map<String, Value>,
    index: usize,
    field: &'static str,
) -> Result<bool, HistoryProjectionError> {
    object
        .get(field.rsplit('.').next().expect("field has a member"))
        .and_then(Value::as_bool)
        .ok_or(HistoryProjectionError::InvalidField { index, field })
}

fn optional_i64(
    value: &Value,
    index: usize,
    field: &'static str,
) -> Result<Option<i64>, HistoryProjectionError> {
    match value {
        Value::Null => Ok(None),
        value => value
            .as_i64()
            .map(Some)
            .ok_or(HistoryProjectionError::InvalidField { index, field }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tool_cards::ProcessDisplayState;
    use crate::transcript::{TranscriptEntryKind, TranscriptRole};
    use serde_json::json;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn message(role: &str, content: &str) -> Value {
        static NEXT_ENTRY_ID: AtomicUsize = AtomicUsize::new(0);
        let entry_id = NEXT_ENTRY_ID.fetch_add(1, Ordering::Relaxed);
        json!({
            "entry_id": format!("entry-{entry_id}"),
            "role": role,
            "content": content,
            "content_truncated": false,
            "tool_calls": [],
            "is_error": null,
            "tool_name": null,
            "tool_call_id": null,
            "tool_result": null,
        })
    }

    #[test]
    fn exact_tool_projection_rejects_the_wrong_persisted_entry() {
        let result = message("tool", "result");
        assert!(matches!(
            project_rpc_exact_tool_result(&[result], "other-entry"),
            Err(HistoryProjectionError::ExactEntryMismatch { .. })
        ));
    }

    #[test]
    fn exact_tool_projection_rejects_oversized_before_snapshots() {
        let oversized = "x".repeat(TOOL_OUTPUT_MAX_BYTES + 1);
        let mut result = message("tool", "wrote file");
        let entry_id = result["entry_id"].as_str().unwrap().to_owned();
        result["tool_call_id"] = json!("write-1");
        result["tool_name"] = json!("write");
        result["tool_result"] = json!({
            "before_text": oversized,
            "created": false,
            "truncated": false,
        });

        let projected = project_rpc_exact_tool_result(&[result], &entry_id).unwrap();

        assert!(projected.before_text.is_none());
        assert!(projected.truncated);
        let arguments = json!({"path": "large.txt", "content": "replacement"});
        let mut transcript = SharedTranscript::default();
        let target = transcript.observe_tool_call(ToolCallInput {
            call_id: projected.call_id.clone(),
            name: "write".into(),
            arguments: arguments.clone(),
            detail_source: project_tool_detail_source("write", arguments.as_object().unwrap()),
        });
        transcript.mark_history_entries(0, "call-entry");
        let mut bounded_page_result = projected.clone();
        bounded_page_result.before_text = None;
        transcript.observe_tool_result(bounded_page_result);
        transcript.add_history_origin(target, "result-entry");
        transcript.mark_history_result_projection(target, true);
        assert!(
            transcript
                .exact_historical_detail(target, &projected)
                .is_none()
        );
    }

    #[test]
    fn omits_system_marks_truncation_and_keeps_empty_assistant() {
        let mut system = message("system", "hidden");
        let mut assistant = message("assistant", "");
        assistant["content_truncated"] = json!(true);
        let empty = message("assistant", "");
        let transcript =
            project_rpc_messages(&[system.take(), message("user", "hello"), assistant, empty])
                .unwrap();
        assert_eq!(transcript.entries().len(), 3);
        assert_eq!(transcript.entries()[0].role, TranscriptRole::User);
        assert!(
            transcript.entries()[1]
                .content
                .contains(CONTENT_TRUNCATED_MARKER)
        );
        assert_eq!(transcript.entries()[2].content, EMPTY_ASSISTANT_MESSAGE);
    }

    #[test]
    fn page_truncation_does_not_create_an_omission_marker() {
        let transcript = project_rpc_message_page(&[message("user", "retained")], true).unwrap();

        assert_eq!(transcript.entries().len(), 1);
        assert_eq!(transcript.entries()[0].role, TranscriptRole::User);
        assert_eq!(transcript.entries()[0].content, "retained");
    }

    #[test]
    fn pairs_multiple_calls_and_settles_call_only_boundaries() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = json!([
            {"call_id": "one", "name": "read", "arguments": {"path": "a"}},
            {"call_id": "two", "name": "read", "arguments": {"path": "b"}}
        ]);
        let mut result = message("tool", "contents");
        result["tool_call_id"] = json!("two");
        result["tool_name"] = json!("read");
        let transcript = project_rpc_messages(&[assistant, result]).unwrap();
        let cards = transcript
            .entries()
            .iter()
            .filter_map(|entry| entry.tool_card())
            .collect::<Vec<_>>();
        assert_eq!(cards.len(), 2);
        assert_eq!(cards[1].status.as_str(), "done");
        assert_eq!(cards[0].status.as_str(), "cancelled");
    }

    #[test]
    fn adjacent_pages_reconcile_split_tool_lifecycles() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = json!([{
            "call_id": "read-boundary",
            "name": "read",
            "arguments": {"path": "README.md"},
        }]);
        let mut result = message("tool", "alpha\nbeta\n");
        result["tool_call_id"] = json!("read-boundary");
        result["tool_name"] = json!("read");
        result["tool_result"] = json!({"summary": "read 2 lines from README.md"});
        let older = project_rpc_messages(&[assistant]).unwrap();
        let mut newer = project_rpc_messages(&[result]).unwrap();
        let mut appended = older.clone();

        assert!(appended.append_history_page(&newer));
        assert_eq!(
            appended
                .entries()
                .iter()
                .filter_map(|entry| entry.tool_card())
                .count(),
            1
        );
        assert!(newer.prepend_history_page(&older));

        let cards = newer
            .entries()
            .iter()
            .filter_map(|entry| entry.tool_card())
            .collect::<Vec<_>>();
        assert_eq!(cards.len(), 1);
        assert!(cards[0].arguments_available);
        assert_eq!(cards[0].status.as_str(), "done");
        assert!(cards[0].has_retained_detail());
    }

    #[test]
    fn adjacent_pages_reconcile_split_process_lifecycles() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = json!([
            {
                "call_id": "poll-boundary-1",
                "name": "bash",
                "arguments": {"operation": "poll", "process_id": "process-1"},
            },
            {
                "call_id": "poll-boundary-2",
                "name": "bash",
                "arguments": {"operation": "poll", "process_id": "process-1"},
            }
        ]);
        let mut first_result = message(
            "tool",
            "Process process-1 is still running\nstdout:\nfirst output",
        );
        first_result["tool_call_id"] = json!("poll-boundary-1");
        first_result["tool_name"] = json!("bash");
        first_result["tool_result"] = json!({"status": "running"});
        let mut second_result = message(
            "tool",
            "Process process-1 is still running\nstdout:\nsecond output",
        );
        second_result["tool_call_id"] = json!("poll-boundary-2");
        second_result["tool_name"] = json!("bash");
        second_result["tool_result"] = json!({"status": "running"});
        let older = project_rpc_messages(&[assistant]).unwrap();
        let mut newer = project_rpc_messages(&[first_result, second_result]).unwrap();

        assert!(
            newer.prepend_history_page(&older),
            "older={:?} newer={:?}",
            older.entries(),
            newer.entries()
        );

        assert_eq!(newer.entries().len(), 1);
        let card = newer.entries()[0].process_card().unwrap();
        assert_eq!(card.display_state, ProcessDisplayState::Running);
        let first = card.retained_output.text.find("first output").unwrap();
        let second = card.retained_output.text.find("second output").unwrap();
        assert!(first < second);
    }

    #[test]
    fn adjacent_pages_preserve_denied_process_states() {
        for (operation, expected) in [
            ("poll", ProcessDisplayState::PollDenied),
            ("cancel", ProcessDisplayState::CancelDenied),
        ] {
            let call_id = format!("{operation}-denied-boundary");
            let mut assistant = message("assistant", "");
            assistant["tool_calls"] = json!([{
                "call_id": call_id,
                "name": "bash",
                "arguments": {"operation": operation, "process_id": "process-1"},
            }]);
            let mut result = message("tool", "denied");
            result["tool_call_id"] = json!(call_id);
            result["tool_name"] = json!("bash");
            result["tool_result"] = json!({"status": "denied"});
            let older = project_rpc_messages(&[assistant]).unwrap();
            let mut newer = project_rpc_messages(&[result]).unwrap();

            assert!(newer.prepend_history_page(&older));

            assert_eq!(newer.entries().len(), 1);
            assert_eq!(
                newer.entries()[0].process_card().unwrap().display_state,
                expected
            );
        }
    }

    #[test]
    fn keeps_result_only_boundaries_and_skill_fallbacks() {
        let mut user = message("user", "expanded provider prompt");
        user["skill_invocation"] = json!({
            "name": "dsa", "request": "  explain   heap ",
            "request_truncated": true, "instructions_truncated": true
        });
        let mut result = message("tool", "late result");
        result["tool_call_id"] = json!("missing");
        result["tool_name"] = json!("read");
        let transcript = project_rpc_messages(&[user, result]).unwrap();
        assert!(
            transcript.entries()[0]
                .content
                .contains("skill /skill:dsa explain heap")
        );
        let card = transcript
            .entries()
            .iter()
            .find_map(|entry| match &entry.kind {
                TranscriptEntryKind::Tool(card) => Some(card),
                TranscriptEntryKind::Message | TranscriptEntryKind::Process(_) => None,
            })
            .unwrap();
        assert!(!card.arguments_available);
        assert_eq!(card.status.as_str(), "done");
    }

    #[test]
    fn incomplete_arguments_disable_structured_history_detail() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = json!([{
            "call_id": "write-1",
            "name": "write",
            "arguments": {"path": "file.txt", "content": "partial"},
            "arguments_truncated": true,
            "parse_error": null,
        }]);
        let mut result = message("tool", "ok");
        result["tool_call_id"] = json!("write-1");
        result["tool_name"] = json!("write");

        let transcript = project_rpc_messages(&[assistant, result]).unwrap();
        let card = transcript
            .entries()
            .iter()
            .find_map(|entry| entry.tool_card())
            .unwrap();
        assert!(matches!(
            card.structured_detail,
            crate::tool_detail::DetailAvailability::Unavailable(
                DetailUnavailableReason::MalformedSource
            )
        ));
    }

    #[test]
    fn exact_detail_requires_backend_projection_truncation() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = json!([{
            "call_id": "read-1",
            "name": "read",
            "arguments": {"path": "large.txt"},
        }]);
        let mut tool_truncated = message("tool", "partial");
        tool_truncated["tool_call_id"] = json!("read-1");
        tool_truncated["tool_name"] = json!("read");
        tool_truncated["tool_result"] = json!({"truncated": true});

        let transcript = project_rpc_messages(&[assistant.clone(), tool_truncated]).unwrap();
        let target = transcript
            .entries()
            .iter()
            .find(|entry| entry.tool_card().is_some())
            .unwrap()
            .id;
        assert_eq!(transcript.exact_historical_detail_target(target), None);

        let mut projection_truncated = message("tool", "partial");
        projection_truncated["tool_call_id"] = json!("read-1");
        projection_truncated["tool_name"] = json!("read");
        projection_truncated["content_truncated"] = json!(true);
        let transcript = project_rpc_messages(&[assistant, projection_truncated]).unwrap();
        let target = transcript
            .entries()
            .iter()
            .find(|entry| entry.tool_card().is_some())
            .unwrap()
            .id;
        assert_eq!(
            transcript.exact_historical_detail_target(target),
            Some(target)
        );
    }

    #[test]
    fn result_only_boundaries_cannot_collide_and_preserve_denial_and_source_counts() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = json!([{
            "call_id": "history-result-boundary-1",
            "name": "read",
            "arguments": {"path": "real.txt"},
        }]);
        let mut result = message("tool", "denied");
        result["tool_name"] = json!("read");
        result["tool_result"] = json!({"status": "denied"});
        let mut large_result = message("tool", "retained head");
        large_result["tool_name"] = json!("read");
        large_result["tool_result"] = json!({"status": "done"});
        large_result["content_original_bytes"] = json!(u64::MAX);

        let transcript = project_rpc_messages(&[assistant, result, large_result]).unwrap();
        let cards = transcript
            .entries()
            .iter()
            .filter_map(|entry| entry.tool_card())
            .collect::<Vec<_>>();
        assert_eq!(cards.len(), 3);
        assert_eq!(cards[0].status.as_str(), "cancelled");
        assert_eq!(cards[1].status.as_str(), "denied");
        assert_eq!(cards[2].status.as_str(), "done");
        assert_eq!(cards[2].retained_output.source_bytes, u64::MAX);
    }

    #[test]
    fn retains_process_identity_for_historical_poll_and_cancel_results() {
        let mut poll = message("assistant", "");
        poll["tool_calls"] = json!([
            {"call_id": "poll", "name": "bash", "arguments": {"operation": "poll", "process_id": "process-1"}}
        ]);
        let mut poll_result = message("tool", "still running");
        poll_result["tool_call_id"] = json!("poll");
        poll_result["tool_name"] = json!("bash");
        poll_result["tool_result"] = json!({"status": "running"});
        let mut cancel = message("assistant", "");
        cancel["tool_calls"] = json!([
            {"call_id": "cancel", "name": "bash", "arguments": {"operation": "cancel", "process_id": "process-1"}}
        ]);
        let mut cancel_result = message("tool", "Process process-1 cancelled");
        cancel_result["tool_call_id"] = json!("cancel");
        cancel_result["tool_name"] = json!("bash");
        cancel_result["tool_result"] = json!({"status": "cancelled"});

        let transcript = project_rpc_messages(&[poll, poll_result, cancel, cancel_result]).unwrap();
        let card = transcript
            .entries()
            .iter()
            .find_map(|entry| entry.process_card())
            .unwrap();
        assert_eq!(card.process_id, bounded_identity("process-1"));
        assert_eq!(card.call_count, 2);
        assert_eq!(card.poll_count, 1);
        assert_eq!(card.display_state.status().as_str(), "cancelled");
    }

    #[test]
    fn successful_process_envelopes_restore_state_and_streams() {
        let mut first_poll = message("assistant", "");
        first_poll["tool_calls"] = json!([{
            "call_id": "poll-running",
            "name": "bash",
            "arguments": {"operation": "poll", "process_id": "process-success"},
        }]);
        let mut running = message(
            "tool",
            "Process process-success is still running\nstdout:\nfirst chunk",
        );
        running["tool_call_id"] = json!("poll-running");
        running["tool_name"] = json!("bash");
        running["tool_result"] = json!({"status": "done"});
        let mut second_poll = message("assistant", "");
        second_poll["tool_calls"] = json!([{
            "call_id": "poll-completed",
            "name": "bash",
            "arguments": {"operation": "poll", "process_id": "process-success"},
        }]);
        let mut completed = message(
            "tool",
            "Process process-success completed with exit code 0\nstdout:\nlast chunk\nstderr:\nwarning",
        );
        completed["tool_call_id"] = json!("poll-completed");
        completed["tool_name"] = json!("bash");
        completed["tool_result"] = json!({"status": "done"});

        let transcript =
            project_rpc_messages(&[first_poll, running, second_poll, completed]).unwrap();
        let card = transcript
            .entries()
            .iter()
            .find_map(|entry| entry.process_card())
            .unwrap();
        assert_eq!(
            card.display_state,
            crate::tool_cards::ProcessDisplayState::Completed
        );
        assert!(card.retained_output.text.contains("stdout:\nfirst chunk"));
        assert!(card.retained_output.text.contains("last chunk"));
        assert!(card.retained_output.text.contains("stderr:\nwarning"));
    }

    #[test]
    fn cancelled_process_envelopes_restore_streams() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = json!([{
            "call_id": "cancel-result",
            "name": "bash",
            "arguments": {"operation": "cancel", "process_id": "process-cancelled"},
        }]);
        let mut result = message(
            "tool",
            "Process process-cancelled cancelled\nstderr:\nterminated cleanly",
        );
        result["tool_call_id"] = json!("cancel-result");
        result["tool_name"] = json!("bash");
        result["tool_result"] = json!({"status": "cancelled"});

        let transcript = project_rpc_messages(&[assistant, result]).unwrap();
        let card = transcript
            .entries()
            .iter()
            .find_map(|entry| entry.process_card())
            .unwrap();
        assert_eq!(
            card.display_state,
            crate::tool_cards::ProcessDisplayState::Cancelled
        );
        assert!(
            card.retained_output
                .text
                .contains("stderr:\nterminated cleanly")
        );
    }

    #[test]
    fn mismatched_cancelled_envelopes_fall_back_without_guessing_state() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = json!([{
            "call_id": "cancel-mismatch",
            "name": "bash",
            "arguments": {"operation": "cancel", "process_id": "process-expected"},
        }]);
        let mut result = message(
            "tool",
            "Process process-foreign cancelled\nstderr:\nforeign diagnostic",
        );
        result["tool_call_id"] = json!("cancel-mismatch");
        result["tool_name"] = json!("bash");
        result["tool_result"] = json!({"status": "cancelled"});

        let transcript = project_rpc_messages(&[assistant, result]).unwrap();
        let card = transcript
            .entries()
            .iter()
            .find_map(|entry| entry.process_card())
            .unwrap();
        assert_eq!(
            card.display_state,
            crate::tool_cards::ProcessDisplayState::Observed
        );
        assert!(
            card.retained_output
                .text
                .contains("Process process-foreign cancelled")
        );
        assert!(card.retained_output.text.contains("foreign diagnostic"));
    }

    #[test]
    fn denied_process_results_preserve_the_matching_call_identity() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = json!([{
            "call_id": "poll-denied",
            "name": "bash",
            "arguments": {"operation": "poll", "process_id": "process-denied"},
        }]);
        let mut result = message("tool", "denied");
        result["tool_call_id"] = json!("poll-denied");
        result["tool_name"] = json!("bash");
        result["tool_result"] = json!({"status": "denied"});

        let transcript = project_rpc_messages(&[assistant, result]).unwrap();
        let card = transcript
            .entries()
            .iter()
            .find_map(|entry| entry.process_card())
            .unwrap();
        assert_eq!(card.process_id, bounded_identity("process-denied"));
        assert_eq!(card.call_count, 1);
        assert_eq!(
            card.display_state,
            crate::tool_cards::ProcessDisplayState::PollDenied
        );
    }

    #[test]
    fn legacy_interrupted_process_results_project_as_cancelled() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = json!([{
            "call_id": "poll-interrupted",
            "name": "bash",
            "arguments": {"operation": "poll", "process_id": "process-interrupted"},
        }]);
        let mut result = message("tool", INTERRUPTED_TOOL_RESULT_TEXT);
        result["tool_call_id"] = json!("poll-interrupted");
        result["tool_name"] = json!("bash");
        result["is_error"] = json!(true);

        let transcript = project_rpc_messages(&[assistant, result]).unwrap();
        let card = transcript
            .entries()
            .iter()
            .find_map(|entry| entry.process_card())
            .unwrap();
        assert_eq!(card.process_id, bounded_identity("process-interrupted"));
        assert_eq!(card.display_state.status().as_str(), "cancelled");
    }

    #[test]
    fn rejects_schema_valid_tool_call_overflow_truthfully() {
        let mut assistant = message("assistant", "");
        assistant["tool_calls"] = Value::Array(
            (0..=HISTORY_TOOL_CALL_LIMIT)
                .map(|index| {
                    json!({
                        "call_id": format!("call-{index}"),
                        "name": "read",
                        "arguments": {"path": format!("file-{index}")},
                    })
                })
                .collect(),
        );

        assert!(matches!(
            project_rpc_messages(&[assistant]),
            Err(HistoryProjectionError::TooManyToolCalls { index: 0 })
        ));
    }
}
