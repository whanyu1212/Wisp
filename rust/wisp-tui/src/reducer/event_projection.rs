use super::{BackendEvent, MessageContentKind, PendingApproval};
use crate::tool_cards::{
    BoundedText, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES, ToolCallInput, ToolResultInput,
    bounded_identity, bounded_tool_arguments,
};
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
                let name = bounded_display_string_field(value, &event_type, "name", 512)?;
                let arguments = object_field(value, &event_type, "arguments")?;
                Self::ToolCall(ToolCallInput {
                    call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
                    arguments: bounded_tool_arguments(&name, &arguments),
                    name,
                })
            }
            "tool.approval.requested" => {
                let name = bounded_display_string_field(value, &event_type, "name", 512)?;
                let arguments = object_field(value, &event_type, "arguments")?;
                Self::ToolApprovalRequested(PendingApproval {
                    call_id: string_field(value, &event_type, "call_id")?,
                    arguments: bounded_tool_arguments(&name, &arguments),
                    name,
                    safety: bounded_display_string_field(value, &event_type, "safety", 128)?,
                })
            }
            "tool.approval.resolved" => Self::ToolApprovalResolved {
                call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
                name: bounded_display_string_field(value, &event_type, "name", 512)?,
                approved: bool_field(value, &event_type, "approved")?,
                reason: optional_bounded_display_string_field(value, &event_type, "reason", 512)?,
            },
            "tool.result" => {
                let is_error = bool_field(value, &event_type, "is_error")?;
                let exit_code = optional_i64_field(value, &event_type, "exit_code")?;
                let process_state = optional_string_field(value, &event_type, "process_state")?;
                let retain_output_tail = process_state.as_deref() != Some("cancelled")
                    && (is_error
                        || exit_code.is_some_and(|code| code != 0)
                        || matches!(process_state.as_deref(), Some("failed" | "timed_out")));
                let (output, output_source_bytes) =
                    bounded_string_field(value, &event_type, "output", retain_output_tail)?;
                let (stdout, stdout_source_bytes) =
                    optional_bounded_string_field(value, &event_type, "stdout", true)?;
                let (stderr, stderr_source_bytes) =
                    optional_bounded_string_field(value, &event_type, "stderr", true)?;
                Self::ToolResult(Box::new(ToolResultInput {
                    call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
                    name: bounded_display_string_field(value, &event_type, "name", 512)?,
                    output,
                    output_source_bytes,
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
                    before_text: None,
                    created: bool_field_or(value, &event_type, "created", false)?,
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
            "rpc.command.finished" => Self::CommandFinished {
                command_id: string_field(value, &event_type, "command_id")?,
                command_type: string_field(value, &event_type, "command_type")?,
                ok: bool_field(value, &event_type, "ok")?,
                error: value
                    .get("error")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            },
            _ => Self::Other { event_type },
        };
        Ok(projected)
    }
}

fn string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<String, EventProjectionError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        })
}

fn bounded_string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
    retain_tail: bool,
) -> Result<(String, u64), EventProjectionError> {
    let source = value.get(field).and_then(Value::as_str).ok_or_else(|| {
        EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }
    })?;
    let bounded = if retain_tail {
        BoundedText::tail(source, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES)
    } else {
        BoundedText::head(source, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES)
    };
    Ok((bounded.text, bounded.source_bytes))
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
            .map(|(text, bytes)| (Some(text), bytes)),
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

fn object_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<Value, EventProjectionError> {
    match value.get(field) {
        Some(Value::Object(object)) => Ok(Value::Object(object.clone())),
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
