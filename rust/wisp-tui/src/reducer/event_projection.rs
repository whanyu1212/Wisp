use super::{BackendEvent, MessageContentKind, PendingApproval};
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
            "tool.approval.requested" => Self::ToolApprovalRequested(PendingApproval {
                call_id: string_field(value, &event_type, "call_id")?,
                name: string_field(value, &event_type, "name")?,
                arguments: value.get("arguments").cloned().ok_or_else(|| {
                    EventProjectionError::InvalidField {
                        event_type: event_type.clone(),
                        field: "arguments",
                    }
                })?,
                safety: string_field(value, &event_type, "safety")?,
            }),
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
