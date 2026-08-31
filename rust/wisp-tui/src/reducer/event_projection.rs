use std::collections::VecDeque;

use super::{
    BackendEvent, MessageContentKind, PendingApproval, QUEUE_CONTENT_BYTES_LIMIT,
    QUEUE_MESSAGE_LIMIT, QueueRemovalOperation, SESSION_CATALOG_LIMIT, SESSION_ENTRY_COUNT_MAX,
    SESSION_ID_MAX_BYTES, SESSION_LABEL_MAX_BYTES, SESSION_PATH_MAX_BYTES, SESSION_TREE_PAGE_LIMIT,
    SESSION_UPDATED_AT_MAX_BYTES, SessionDerivation, SessionIdentity, SessionMessages,
    SessionNameChange, SessionSummary, SessionTreeNavigation, SessionTreeNode, SessionTreeNodeKind,
    SessionTreePage, SessionTreeUnrevert,
};
use crate::history::project_rpc_message_page_with_origins;
use crate::tool_cards::{
    BoundedText, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES, ToolCallInput, ToolResultInput,
    bounded_identity, bounded_tool_arguments, bounded_tool_name,
};
use crate::tool_detail::{capture_write_before, project_tool_detail_source};
use serde_json::Value;
use thiserror::Error;
use wisp_protocol::MAX_APPLICATION_FRAME_BYTES;
use wisp_protocol::commands::QueueKind;
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
    #[error("session tree report has more than {SESSION_TREE_PAGE_LIMIT} nodes")]
    TooManyTreeNodes,
    #[error("queue event {event_type:?} has more than {QUEUE_MESSAGE_LIMIT} messages")]
    TooManyQueueMessages { event_type: String },
    #[error(
        "queue event {event_type:?} exceeds the {QUEUE_CONTENT_BYTES_LIMIT}-byte content limit"
    )]
    OversizedQueueContent { event_type: String },
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
            "queue.updated" => {
                let steering = array_field(value, &event_type, "steering")?;
                let follow_up = array_field(value, &event_type, "follow_up")?;
                validate_queue_arrays(
                    &event_type,
                    [("steering", steering), ("follow_up", follow_up)],
                )?;
                Self::QueueUpdated {
                    steering: queue_strings(steering),
                    follow_up: queue_strings(follow_up),
                }
            }
            "queue.items.removed" => {
                let steering = array_field(value, &event_type, "steering")?;
                let follow_up = array_field(value, &event_type, "follow_up")?;
                validate_queue_arrays(
                    &event_type,
                    [("steering", steering), ("follow_up", follow_up)],
                )?;
                let operation = queue_removal_operation_field(value, &event_type)?;
                let kind = optional_queue_kind_field(value, &event_type)?;
                validate_queue_removal_shape(&event_type, operation, kind, steering, follow_up)?;
                Self::QueueItemsRemoved {
                    command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                    operation,
                    kind,
                    steering: queue_strings(steering),
                    follow_up: queue_strings(follow_up),
                }
            }
            "queue.message.injected" => {
                let content = string_field(value, &event_type, "content")?;
                validate_queue_payload(&event_type, [content.as_str()])?;
                let visible_content = queue_visible_content(value, &event_type, content)?;
                validate_queue_payload(&event_type, [visible_content.as_str()])?;
                Self::QueueMessageInjected {
                    kind: queue_kind_field(value, &event_type, "kind")?,
                    content: visible_content,
                }
            }
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
            "rpc.session.name_changed" => Self::SessionNameChanged {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                changed: SessionNameChange {
                    session: required_session_identity(
                        value,
                        &event_type,
                        "session_id",
                        "session_path",
                        Some("name"),
                    )?,
                    previous_name: optional_bounded_display_string_field(
                        value,
                        &event_type,
                        "previous_name",
                        SESSION_LABEL_MAX_BYTES,
                    )?,
                    entry_count: bounded_entry_count(value, &event_type)?,
                },
            },
            "rpc.session.cloned" => Self::SessionCloned {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                derived: session_derivation(value, &event_type, false)?,
            },
            "rpc.session.forked" => Self::SessionForked {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                derived: session_derivation(value, &event_type, true)?,
            },
            "rpc.session.tree" => Self::SessionTreeReported {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                page: session_tree_page(value, &event_type)?,
            },
            "rpc.session.tree.navigated" => Self::SessionTreeNavigated {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                navigation: session_tree_navigation(value, &event_type)?,
            },
            "rpc.session.tree.unreverted" => Self::SessionTreeUnreverted {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                unreverted: session_tree_unrevert(value, &event_type)?,
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
                match project_rpc_message_page_with_origins(
                    array_field(value, &event_type, "messages")?,
                    bool_field(value, &event_type, "truncated")?,
                ) {
                    Ok(page) => Self::MessagesReported {
                        command_id,
                        messages: SessionMessages {
                            session,
                            active_leaf_id: optional_exact_string_field(
                                value,
                                &event_type,
                                "active_leaf_id",
                                SESSION_ID_MAX_BYTES,
                            )?,
                            truncated: bool_field(value, &event_type, "truncated")?,
                            next_before_entry_id: optional_exact_string_field(
                                value,
                                &event_type,
                                "next_before_entry_id",
                                SESSION_ID_MAX_BYTES,
                            )?,
                            next_after_entry_id: optional_exact_string_field(
                                value,
                                &event_type,
                                "next_after_entry_id",
                                SESSION_ID_MAX_BYTES,
                            )?,
                            exact_tool_result: array_field(value, &event_type, "messages")?
                                .first()
                                .and_then(|message| message.get("entry_id"))
                                .and_then(Value::as_str)
                                .and_then(|entry_id| {
                                    crate::history::project_rpc_exact_tool_result(
                                        array_field(value, &event_type, "messages").ok()?,
                                        entry_id,
                                    )
                                    .ok()
                                    .map(Box::new)
                                }),
                            durable_entry_ids: page.durable_entry_ids,
                            transcript: page.transcript,
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

fn bounded_entry_count(value: &Value, event_type: &str) -> Result<u32, EventProjectionError> {
    Ok(u64_field(value, event_type, "entry_count")?.min(u64::from(SESSION_ENTRY_COUNT_MAX)) as u32)
}

fn session_derivation(
    value: &Value,
    event_type: &str,
    forked: bool,
) -> Result<SessionDerivation, EventProjectionError> {
    let source = required_session_identity(
        value,
        event_type,
        "source_session_id",
        "source_session_path",
        Some("source_session_name"),
    )?;
    let session = required_session_identity(
        value,
        event_type,
        "session_id",
        "session_path",
        Some("session_name"),
    )?;
    if source.session_id == session.session_id {
        return Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "session_id",
        });
    }
    let selected_entry_id = forked
        .then(|| exact_string_field(value, event_type, "selected_entry_id", SESSION_ID_MAX_BYTES))
        .transpose()?;
    let selected_prompt = forked
        .then(|| {
            exact_string_field(
                value,
                event_type,
                "selected_prompt",
                MAX_APPLICATION_FRAME_BYTES,
            )
        })
        .transpose()?;
    Ok(SessionDerivation {
        source,
        source_active_leaf_id: optional_exact_string_field(
            value,
            event_type,
            "source_active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        session,
        active_leaf_id: optional_exact_string_field(
            value,
            event_type,
            "active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        entry_count: bounded_entry_count(value, event_type)?,
        selected_entry_id,
        selected_prompt,
    })
}

fn session_tree_page(
    value: &Value,
    event_type: &str,
) -> Result<SessionTreePage, EventProjectionError> {
    let raw_nodes = array_field(value, event_type, "nodes")?;
    if raw_nodes.len() > SESSION_TREE_PAGE_LIMIT {
        return Err(EventProjectionError::TooManyTreeNodes);
    }
    let session = optional_session_identity(value, event_type, "session_id", "session_path", None)?;
    let active_leaf_id =
        optional_exact_string_field(value, event_type, "active_leaf_id", SESSION_ID_MAX_BYTES)?;
    let total_node_count = u64_field(value, event_type, "total_node_count")?
        .min(u64::from(SESSION_ENTRY_COUNT_MAX)) as u32;
    let truncated = bool_field(value, event_type, "truncated")?;
    let next_after_entry_id = optional_exact_string_field(
        value,
        event_type,
        "next_after_entry_id",
        SESSION_ID_MAX_BYTES,
    )?;
    let mut entry_ids = std::collections::BTreeSet::new();
    let nodes = raw_nodes
        .iter()
        .map(|node| {
            let entry_id = exact_string_field(node, event_type, "entry_id", SESSION_ID_MAX_BYTES)?;
            if !entry_ids.insert(entry_id.clone()) {
                return Err(EventProjectionError::InvalidField {
                    event_type: event_type.to_owned(),
                    field: "nodes",
                });
            }
            let kind = match string_field_ref(node, event_type, "kind")? {
                "message" => SessionTreeNodeKind::Message,
                "event" => SessionTreeNodeKind::Event,
                "compaction" => SessionTreeNodeKind::Compaction,
                _ => {
                    return Err(EventProjectionError::InvalidField {
                        event_type: event_type.to_owned(),
                        field: "kind",
                    });
                }
            };
            let role = optional_exact_string_field(node, event_type, "role", 32)?;
            let valid_role = matches!(
                role.as_deref(),
                Some("system" | "user" | "assistant" | "tool")
            );
            if (kind == SessionTreeNodeKind::Message) != valid_role {
                return Err(EventProjectionError::InvalidField {
                    event_type: event_type.to_owned(),
                    field: "role",
                });
            }
            let raw_preview = string_field_ref(node, event_type, "preview")?;
            let bounded_preview = BoundedText::head(raw_preview, SESSION_LABEL_MAX_BYTES, 8);
            optional_exact_string_field(node, event_type, "operation_id", SESSION_ID_MAX_BYTES)?;
            Ok(SessionTreeNode {
                entry_id,
                parent_id: optional_exact_string_field(
                    node,
                    event_type,
                    "parent_id",
                    SESSION_ID_MAX_BYTES,
                )?,
                created_at: bounded_display_string_field(
                    node,
                    event_type,
                    "created_at",
                    SESSION_UPDATED_AT_MAX_BYTES,
                )?,
                kind,
                role,
                preview: bounded_preview.text,
                preview_truncated: bool_field(node, event_type, "preview_truncated")?
                    || bounded_preview.dropped_bytes > 0
                    || bounded_preview.dropped_lines > 0,
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    let valid_cursor = truncated
        && !nodes.is_empty()
        && next_after_entry_id.as_ref() == nodes.last().map(|node| &node.entry_id);
    if nodes.len() > total_node_count as usize
        || truncated != next_after_entry_id.is_some()
        || (truncated && !valid_cursor)
        || (session.is_none()
            && (active_leaf_id.is_some()
                || total_node_count != 0
                || !nodes.is_empty()
                || truncated))
    {
        return Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "tree page",
        });
    }
    Ok(SessionTreePage {
        session,
        active_leaf_id,
        total_node_count,
        nodes,
        truncated,
        next_after_entry_id,
    })
}

fn session_tree_navigation(
    value: &Value,
    event_type: &str,
) -> Result<SessionTreeNavigation, EventProjectionError> {
    let previous_active_leaf_id = optional_exact_string_field(
        value,
        event_type,
        "previous_active_leaf_id",
        SESSION_ID_MAX_BYTES,
    )?;
    let active_leaf_id =
        optional_exact_string_field(value, event_type, "active_leaf_id", SESSION_ID_MAX_BYTES)?;
    let changed = bool_field(value, event_type, "changed")?;
    if changed == (previous_active_leaf_id == active_leaf_id) {
        return Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "changed",
        });
    }
    Ok(SessionTreeNavigation {
        session: required_session_identity(value, event_type, "session_id", "session_path", None)?,
        selected_entry_id: exact_string_field(
            value,
            event_type,
            "selected_entry_id",
            SESSION_ID_MAX_BYTES,
        )?,
        previous_active_leaf_id,
        active_leaf_id,
        editor_text: optional_exact_string_field(
            value,
            event_type,
            "editor_text",
            MAX_APPLICATION_FRAME_BYTES,
        )?,
        changed,
        entry_count: bounded_entry_count(value, event_type)?,
    })
}

fn session_tree_unrevert(
    value: &Value,
    event_type: &str,
) -> Result<SessionTreeUnrevert, EventProjectionError> {
    let previous_active_leaf_id = optional_exact_string_field(
        value,
        event_type,
        "previous_active_leaf_id",
        SESSION_ID_MAX_BYTES,
    )?;
    let active_leaf_id =
        optional_exact_string_field(value, event_type, "active_leaf_id", SESSION_ID_MAX_BYTES)?;
    if previous_active_leaf_id == active_leaf_id {
        return Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "active_leaf_id",
        });
    }
    Ok(SessionTreeUnrevert {
        session: required_session_identity(value, event_type, "session_id", "session_path", None)?,
        source_transition_id: exact_string_field(
            value,
            event_type,
            "source_transition_id",
            SESSION_ID_MAX_BYTES,
        )?,
        previous_active_leaf_id,
        active_leaf_id,
        entry_count: bounded_entry_count(value, event_type)?,
    })
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

fn optional_exact_string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
    limit: usize,
) -> Result<Option<String>, EventProjectionError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(_)) => exact_string_field(value, event_type, field, limit).map(Some),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }),
    }
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

fn queue_strings(contents: &[Value]) -> Vec<String> {
    contents
        .iter()
        .map(|item| {
            item.as_str()
                .expect("queue strings were validated before cloning")
                .to_owned()
        })
        .collect()
}

fn validate_queue_arrays<'a>(
    event_type: &str,
    arrays: impl IntoIterator<Item = (&'static str, &'a [Value])>,
) -> Result<(), EventProjectionError> {
    let mut count = 0usize;
    let mut bytes = 0usize;
    for (field, contents) in arrays {
        for content in contents {
            let content = content
                .as_str()
                .ok_or_else(|| EventProjectionError::InvalidField {
                    event_type: event_type.to_owned(),
                    field,
                })?;
            count = count.saturating_add(1);
            bytes = bytes.saturating_add(content.len());
        }
    }
    if count > QUEUE_MESSAGE_LIMIT {
        return Err(EventProjectionError::TooManyQueueMessages {
            event_type: event_type.to_owned(),
        });
    }
    if bytes > QUEUE_CONTENT_BYTES_LIMIT {
        return Err(EventProjectionError::OversizedQueueContent {
            event_type: event_type.to_owned(),
        });
    }
    Ok(())
}

fn validate_queue_payload<'a>(
    event_type: &str,
    contents: impl IntoIterator<Item = &'a str>,
) -> Result<(), EventProjectionError> {
    let mut count = 0usize;
    let mut bytes = 0usize;
    for content in contents {
        count = count.saturating_add(1);
        bytes = bytes.saturating_add(content.len());
    }
    if count > QUEUE_MESSAGE_LIMIT {
        return Err(EventProjectionError::TooManyQueueMessages {
            event_type: event_type.to_owned(),
        });
    }
    if bytes > QUEUE_CONTENT_BYTES_LIMIT {
        return Err(EventProjectionError::OversizedQueueContent {
            event_type: event_type.to_owned(),
        });
    }
    Ok(())
}

fn queue_kind_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<QueueKind, EventProjectionError> {
    match string_field_ref(value, event_type, field)? {
        "steering" => Ok(QueueKind::Steering),
        "follow_up" => Ok(QueueKind::FollowUp),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        }),
    }
}

fn optional_queue_kind_field(
    value: &Value,
    event_type: &str,
) -> Result<Option<QueueKind>, EventProjectionError> {
    match value.get("kind") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(_)) => queue_kind_field(value, event_type, "kind").map(Some),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "kind",
        }),
    }
}

fn queue_removal_operation_field(
    value: &Value,
    event_type: &str,
) -> Result<QueueRemovalOperation, EventProjectionError> {
    match string_field_ref(value, event_type, "operation")? {
        "pop" => Ok(QueueRemovalOperation::Pop),
        "clear" => Ok(QueueRemovalOperation::Clear),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "operation",
        }),
    }
}

fn validate_queue_removal_shape(
    event_type: &str,
    operation: QueueRemovalOperation,
    kind: Option<QueueKind>,
    steering: &[Value],
    follow_up: &[Value],
) -> Result<(), EventProjectionError> {
    let valid = !matches!(operation, QueueRemovalOperation::Pop) || kind.is_some();
    let matching_kind = match kind {
        Some(QueueKind::Steering) => follow_up.is_empty(),
        Some(QueueKind::FollowUp) => steering.is_empty(),
        None => true,
    };
    let pop_size = !matches!(operation, QueueRemovalOperation::Pop)
        || steering.len().saturating_add(follow_up.len()) <= 1;
    if valid && matching_kind && pop_size {
        Ok(())
    } else {
        Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "queue removal",
        })
    }
}

fn queue_visible_content(
    value: &Value,
    event_type: &str,
    content: String,
) -> Result<String, EventProjectionError> {
    match value.get("skill_invocation") {
        None | Some(Value::Null) => Ok(content),
        Some(Value::Object(invocation)) => match invocation.get("original_content") {
            None | Some(Value::Null) => Ok(content),
            Some(Value::String(original_content)) => Ok(original_content.clone()),
            Some(_) => Err(EventProjectionError::InvalidField {
                event_type: event_type.to_owned(),
                field: "skill_invocation",
            }),
        },
        Some(_) => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "skill_invocation",
        }),
    }
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
