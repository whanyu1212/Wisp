use std::collections::VecDeque;

use super::{
    BackendEvent, MessageContentKind, PendingApproval, SESSION_CATALOG_LIMIT,
    SESSION_ENTRY_COUNT_MAX, SESSION_ID_MAX_BYTES, SESSION_LABEL_MAX_BYTES, SESSION_PATH_MAX_BYTES,
    SESSION_UPDATED_AT_MAX_BYTES, SessionIdentity, SessionMessages, SessionSummary,
};
use crate::history::project_rpc_message_page;
use crate::tool_cards::{
    BoundedText, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES, ToolCallInput, ToolResultInput,
    bounded_identity, bounded_tool_arguments, bounded_tool_name,
};
use crate::tool_detail::{capture_write_before, project_tool_detail_source};
use serde_json::Value;
use thiserror::Error;
use wisp_protocol::events::WispCurrentLiveEventOutput;

#[derive(Debug, Error)]
pub enum EventProjectionError {
    #[error("failed to encode validated live event: {0}")]
    Encode(#[from] serde_json::Error),
    #[error("event {event_type:?} is missing or has an invalid {field:?} field")]
    InvalidField {
        event_type: String,
        field: &'static str,
    },
    #[error("event {event_type:?} {field:?} exceeds its {limit}-byte retention limit")]
    OversizedField {
        event_type: String,
        field: &'static str,
        limit: usize,
    },
    #[error("session report has more than {SESSION_CATALOG_LIMIT} sessions")]
    TooManySessions,
}

impl BackendEvent {
    /// Project an already validated live event into reducer-owned semantics.
    pub fn from_live(event: &WispCurrentLiveEventOutput) -> Result<Self, EventProjectionError> {
        Self::from_projection_value(&event.to_value()?)
    }

    /// Project a bounded trace event. The caller owns trace-schema validation.
    pub fn from_projection_value(value: &Value) -> Result<Self, EventProjectionError> {
        let event_type = string_field(value, "<unknown>", "type")?;
        let projected = match event_type.as_str() {
            "message.started" => Self::MessageStarted {
                turn: u64_field(value, &event_type, "turn")?,
            },
            "message.delta" => {
                let content_kind = match value
                    .get("content_kind")
                    .and_then(Value::as_str)
                    .unwrap_or("text")
                {
                    "text" => MessageContentKind::Text,
                    "thinking" => MessageContentKind::Thinking,
                    other => MessageContentKind::Other(other.to_owned()),
                };
                Self::MessageDelta {
                    turn: u64_field(value, &event_type, "turn")?,
                    delta: string_field(value, &event_type, "delta")?,
                    content_kind,
                }
            }
            "message.completed" => Self::MessageCompleted {
                turn: u64_field(value, &event_type, "turn")?,
                content: string_field(value, &event_type, "content")?,
            },
            "tool.call" => {
                let raw_name = string_field_ref(value, &event_type, "name")?;
                let arguments = object_field(value, &event_type, "arguments")?;
                Self::ToolCall(ToolCallInput {
                    call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
                    detail_source: project_tool_detail_source(
                        raw_name,
                        arguments
                            .as_object()
                            .expect("object_field returned an object"),
                    ),
                    arguments: bounded_tool_arguments(raw_name, arguments),
                    name: bounded_tool_name(raw_name),
                })
            }
            "tool.approval.requested" => {
                let raw_name = string_field_ref(value, &event_type, "name")?;
                let arguments = object_field(value, &event_type, "arguments")?;
                Self::ToolApprovalRequested(PendingApproval {
                    call_id: string_field(value, &event_type, "call_id")?,
                    detail_source: project_tool_detail_source(
                        raw_name,
                        arguments
                            .as_object()
                            .expect("object_field returned an object"),
                    ),
                    arguments: bounded_tool_arguments(raw_name, arguments),
                    name: bounded_tool_name(raw_name),
                    safety: bounded_display_string_field(value, &event_type, "safety", 128)?,
                })
            }
            "tool.approval.resolved" => Self::ToolApprovalResolved {
                call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
                name: bounded_tool_name(string_field_ref(value, &event_type, "name")?),
                approved: bool_field(value, &event_type, "approved")?,
                reason: optional_bounded_display_string_field(value, &event_type, "reason", 512)?,
            },
            "tool.result" => {
                let raw_name = string_field_ref(value, &event_type, "name")?;
                let is_error = bool_field(value, &event_type, "is_error")?;
                let exit_code = optional_i64_field(value, &event_type, "exit_code")?;
                let process_state = optional_string_field(value, &event_type, "process_state")?;
                let cancelled = process_state.as_deref() == Some("cancelled");
                let failed = !cancelled
                    && (is_error
                        || exit_code.is_some_and(|code| code != 0)
                        || matches!(process_state.as_deref(), Some("failed" | "timed_out")));
                let projected_output = projected_text_field(value, &event_type, "output")?;
                let output_source_bytes = projected_output.source_bytes;
                let output_source_lines = projected_output.source_lines;
                let output_projection_cut_mid_line = !failed && projected_output.head_cut_mid_line;
                let (output, output_tail) = if failed {
                    (projected_output.tail, None)
                } else if cancelled {
                    (projected_output.head, None)
                } else {
                    (projected_output.head, Some(projected_output.tail))
                };
                let (stdout, stdout_source_bytes) =
                    optional_bounded_string_field(value, &event_type, "stdout", true)?;
                let (stderr, stderr_source_bytes) =
                    optional_bounded_string_field(value, &event_type, "stderr", true)?;
                let before_text = capture_write_before(raw_name, value.get("before_text"))
                    .ok()
                    .flatten();
                Self::ToolResult(Box::new(ToolResultInput {
                    call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
                    name: bounded_tool_name(raw_name),
                    output,
                    output_tail,
                    output_source_bytes,
                    output_source_lines,
                    output_projection_cut_mid_line,
                    is_error,
                    failure_code: optional_string_field(value, &event_type, "failure_code")?,
                    retryable: bool_field_or(value, &event_type, "retryable", false)?,
                    recovery_hint: optional_bounded_display_string_field(
                        value,
                        &event_type,
                        "recovery_hint",
                        512,
                    )?,
                    exit_code,
                    output_has_exit_status: bool_field_or(
                        value,
                        &event_type,
                        "output_has_exit_status",
                        false,
                    )?,
                    before_text,
                    created: raw_name == "write"
                        && bool_field_or(value, &event_type, "created", false)?,
                    summary: optional_bounded_display_string_field(
                        value,
                        &event_type,
                        "summary",
                        512,
                    )?,
                    truncated: bool_field_or(value, &event_type, "truncated", false)?,
                    process_id: optional_identity_field(value, &event_type, "process_id")?,
                    process_state,
                    process_error: optional_bounded_display_string_field(
                        value,
                        &event_type,
                        "process_error",
                        512,
                    )?,
                    stdout,
                    stdout_source_bytes,
                    stderr,
                    stderr_source_bytes,
                    stdout_truncated: bool_field_or(value, &event_type, "stdout_truncated", false)?,
                    stderr_truncated: bool_field_or(value, &event_type, "stderr_truncated", false)?,
                    stdout_dropped_bytes: u64_field_or(
                        value,
                        &event_type,
                        "stdout_dropped_bytes",
                        0,
                    )?,
                    stderr_dropped_bytes: u64_field_or(
                        value,
                        &event_type,
                        "stderr_dropped_bytes",
                        0,
                    )?,
                }))
            }
            "trust.requested" => Self::TrustRequested {
                request_id: string_field(value, &event_type, "request_id")?,
                project_path: string_field(value, &event_type, "project_path")?,
            },
            "project.config.applied" => Self::ProjectConfigApplied {
                provider: string_field(value, &event_type, "provider")?,
                model: nullable_string_field(value, &event_type, "model")?,
                effort: nullable_string_field(value, &event_type, "effort")?,
            },
            "rpc.sessions" => Self::SessionsReported {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                sessions: session_summaries(value, &event_type)?,
                selected_session: optional_session_identity(
                    value,
                    &event_type,
                    "selected_session_id",
                    "selected_session_path",
                    Some("selected_session_name"),
                )?,
            },
            "rpc.session.selected" => Self::SessionSelected {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                session: required_session_identity(
                    value,
                    &event_type,
                    "session_id",
                    "session_path",
                    Some("session_name"),
                )?,
            },
            "rpc.messages" => {
                let command_id = exact_string_field(value, &event_type, "command_id", 256)?;
                let session = optional_session_identity(
                    value,
                    &event_type,
                    "session_id",
                    "session_path",
                    None,
                )?;
                match project_rpc_message_page(
                    array_field(value, &event_type, "messages")?,
                    bool_field(value, &event_type, "truncated")?,
                ) {
                    Ok(transcript) => Self::MessagesReported {
                        command_id,
                        messages: SessionMessages {
                            session,
                            transcript,
                        },
                    },
                    Err(error) => Self::MessagesProjectionFailed {
                        command_id,
                        error: BoundedText::head(&error.to_string(), 1024, 8).text,
                    },
                }
            }
            "rpc.command.finished" => Self::CommandFinished {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                command_type: bounded_display_string_field(
                    value,
                    &event_type,
                    "command_type",
                    128,
                )?,
                ok: bool_field(value, &event_type, "ok")?,
                error: optional_bounded_display_string_field(value, &event_type, "error", 1024)?,
            },
            _ => Self::Other { event_type },
        };
        Ok(projected)
    }
}

fn session_summaries(
    value: &Value,
    event_type: &str,
) -> Result<Vec<SessionSummary>, EventProjectionError> {
    let sessions = array_field(value, event_type, "sessions")?;
    if sessions.len() > SESSION_CATALOG_LIMIT {
        return Err(EventProjectionError::TooManySessions);
    }
    sessions
        .iter()
        .map(|session| {
            let session_id =
                exact_string_field(session, event_type, "session_id", SESSION_ID_MAX_BYTES)?;
            let session_path =
                exact_string_field(session, event_type, "session_path", SESSION_PATH_MAX_BYTES)?;
            let name = optional_bounded_display_string_field(
                session,
                event_type,
                "name",
                SESSION_LABEL_MAX_BYTES,
            )?;
            let updated_at = bounded_display_string_field(
                session,
                event_type,
                "updated_at",
                SESSION_UPDATED_AT_MAX_BYTES,
            )?;
            let entry_count = u64_field(session, event_type, "entry_count")?
                .min(u64::from(SESSION_ENTRY_COUNT_MAX)) as u32;
            Ok(SessionSummary {
                session_id,
                session_path,
                name,
                updated_at,
                entry_count,
            })
        })
        .collect()
}

fn optional_session_identity(
    value: &Value,
    event_type: &str,
    id_field: &'static str,
    path_field: &'static str,
    name_field: Option<&'static str>,
) -> Result<Option<SessionIdentity>, EventProjectionError> {
    match (value.get(id_field), value.get(path_field)) {
        (None | Some(Value::Null), None | Some(Value::Null)) => Ok(None),
        (Some(Value::String(_)), Some(Value::String(_))) => {
            required_session_identity(value, event_type, id_field, path_field, name_field).map(Some)
        }
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: id_field,
        }),
    }
}

fn required_session_identity(
    value: &Value,
    event_type: &str,
    id_field: &'static str,
    path_field: &'static str,
    name_field: Option<&'static str>,
) -> Result<SessionIdentity, EventProjectionError> {
    let session_id = exact_string_field(value, event_type, id_field, SESSION_ID_MAX_BYTES)?;
    let session_path = exact_string_field(value, event_type, path_field, SESSION_PATH_MAX_BYTES)?;
    let session_name = name_field
        .map(|field| {
            optional_bounded_display_string_field(value, event_type, field, SESSION_LABEL_MAX_BYTES)
        })
        .transpose()?
        .flatten();
    Ok(SessionIdentity {
        session_id,
        session_path,
        session_name,
    })
}

fn exact_string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
    limit: usize,
) -> Result<String, EventProjectionError> {
    let source = string_field_ref(value, event_type, field)?;
    if source.len() > limit {
        return Err(EventProjectionError::OversizedField {
            event_type: event_type.to_owned(),
            field,
            limit,
        });
    }
    Ok(source.to_owned())
}

fn array_field<'a>(
    value: &'a Value,
    event_type: &str,
    field: &'static str,
) -> Result<&'a [Value], EventProjectionError> {
    value
        .get(field)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        })
}

fn string_field_ref<'a>(
    value: &'a Value,
    event_type: &str,
    field: &'static str,
) -> Result<&'a str, EventProjectionError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        })
}

fn string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<String, EventProjectionError> {
    string_field_ref(value, event_type, field).map(str::to_owned)
}

struct ProjectedText {
    head: String,
    tail: String,
    source_bytes: u64,
    source_lines: u64,
    head_cut_mid_line: bool,
}

fn projected_text_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<ProjectedText, EventProjectionError> {
    let source = value.get(field).and_then(Value::as_str).ok_or_else(|| {
        EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }
    })?;
    Ok(project_text(source))
}

fn project_text(source: &str) -> ProjectedText {
    let mut head = String::new();
    let mut head_lines = 0usize;
    let mut head_at_line_start = true;
    let mut head_stopped = false;
    let mut tail_chars = VecDeque::new();
    let mut tail_bytes = 0usize;
    let mut source_bytes = 0usize;
    let mut source_lines = 0u64;
    let mut source_at_line_start = true;
    let mut characters = source.chars().peekable();

    while let Some(mut character) = characters.next() {
        if character == '\r' {
            if characters.peek() == Some(&'\n') {
                characters.next();
            }
            character = '\n';
        }
        let character_bytes = character.len_utf8();
        source_bytes = source_bytes.saturating_add(character_bytes);
        if source_at_line_start {
            source_lines = source_lines.saturating_add(1);
        }
        source_at_line_start = character == '\n';

        if !head_stopped {
            let starts_line = head_at_line_start;
            if head.len().saturating_add(character_bytes) > TOOL_OUTPUT_MAX_BYTES
                || (starts_line && head_lines >= TOOL_OUTPUT_MAX_LINES)
            {
                head_stopped = true;
            } else {
                head.push(character);
                if starts_line {
                    head_lines = head_lines.saturating_add(1);
                }
                head_at_line_start = character == '\n';
            }
        }

        tail_chars.push_back(character);
        tail_bytes = tail_bytes.saturating_add(character_bytes);
        while tail_bytes > TOOL_OUTPUT_MAX_BYTES {
            let Some(removed) = tail_chars.pop_front() else {
                break;
            };
            tail_bytes = tail_bytes.saturating_sub(removed.len_utf8());
        }
    }

    let tail_candidate = tail_chars.into_iter().collect::<String>();
    let tail = BoundedText::tail(
        &tail_candidate,
        TOOL_OUTPUT_MAX_BYTES,
        TOOL_OUTPUT_MAX_LINES,
    )
    .text;
    let source_bytes = u64::try_from(source_bytes).unwrap_or(u64::MAX);
    let head_cut_mid_line =
        u64::try_from(head.len()).unwrap_or(u64::MAX) < source_bytes && !head.ends_with('\n');
    ProjectedText {
        head,
        tail,
        source_bytes,
        source_lines,
        head_cut_mid_line,
    }
}

fn bounded_string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
    retain_tail: bool,
) -> Result<(String, u64, u64), EventProjectionError> {
    let projected = projected_text_field(value, event_type, field)?;
    let text = if retain_tail {
        projected.tail
    } else {
        projected.head
    };
    Ok((text, projected.source_bytes, projected.source_lines))
}

fn bounded_display_string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
    max_bytes: usize,
) -> Result<String, EventProjectionError> {
    let source = value.get(field).and_then(Value::as_str).ok_or_else(|| {
        EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }
    })?;
    Ok(BoundedText::head(source, max_bytes, 8).text)
}

fn optional_bounded_display_string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
    max_bytes: usize,
) -> Result<Option<String>, EventProjectionError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(_)) => {
            bounded_display_string_field(value, event_type, field, max_bytes).map(Some)
        }
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }),
    }
}

fn optional_identity_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<Option<String>, EventProjectionError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(bounded_identity(value))),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }),
    }
}

fn optional_bounded_string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
    retain_tail: bool,
) -> Result<(Option<String>, u64), EventProjectionError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok((None, 0)),
        Some(Value::String(_)) => bounded_string_field(value, event_type, field, retain_tail)
            .map(|(text, bytes, _)| (Some(text), bytes)),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }),
    }
}

fn nullable_string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<Option<String>, EventProjectionError> {
    match value.get(field) {
        Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }),
    }
}

fn optional_string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<Option<String>, EventProjectionError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }),
    }
}

fn object_field<'a>(
    value: &'a Value,
    event_type: &str,
    field: &'static str,
) -> Result<&'a Value, EventProjectionError> {
    match value.get(field) {
        Some(object @ Value::Object(_)) => Ok(object),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }),
    }
}

fn optional_i64_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<Option<i64>, EventProjectionError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_i64()
            .map(Some)
            .ok_or_else(|| EventProjectionError::InvalidField {
                event_type: event_type.to_owned(),
                field,
            }),
    }
}

fn u64_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<u64, EventProjectionError> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        })
}

fn bool_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<bool, EventProjectionError> {
    value
        .get(field)
        .and_then(Value::as_bool)
        .ok_or_else(|| EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        })
}

fn bool_field_or(
    value: &Value,
    event_type: &str,
    field: &'static str,
    default: bool,
) -> Result<bool, EventProjectionError> {
    match value.get(field) {
        None => Ok(default),
        Some(value) => value
            .as_bool()
            .ok_or_else(|| EventProjectionError::InvalidField {
                event_type: event_type.to_owned(),
                field,
            }),
    }
}

fn u64_field_or(
    value: &Value,
    event_type: &str,
    field: &'static str,
    default: u64,
) -> Result<u64, EventProjectionError> {
    match value.get(field) {
        None => Ok(default),
        Some(value) => value
            .as_u64()
            .ok_or_else(|| EventProjectionError::InvalidField {
                event_type: event_type.to_owned(),
                field,
            }),
    }
}
