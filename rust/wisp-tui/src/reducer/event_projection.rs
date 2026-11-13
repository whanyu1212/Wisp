use std::collections::VecDeque;
use std::sync::Arc;

use super::{
    BackendEvent, CatalogNavigation, MessageContentKind, PendingApproval,
    QUEUE_CONTENT_BYTES_LIMIT, QUEUE_MESSAGE_LIMIT, QueueRemovalOperation, SESSION_CATALOG_LIMIT,
    SESSION_ENTRY_COUNT_MAX, SESSION_ID_MAX_BYTES, SESSION_LABEL_MAX_BYTES, SESSION_PATH_MAX_BYTES,
    SESSION_TREE_PAGE_LIMIT, SESSION_UPDATED_AT_MAX_BYTES, SessionDerivation, SessionIdentity,
    SessionMessages, SessionNameChange, SessionSummary, SessionTreeNavigation, SessionTreeNode,
    SessionTreeNodeKind, SessionTreePage, SessionTreeUnrevert,
};
use crate::composer_images::{self, AttachedImage, ComposerImages};
use crate::history::project_rpc_message_page_with_origins;
use crate::tool_cards::{
    BoundedText, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES, ToolCallInput, ToolProgressInput,
    ToolResultInput, bounded_identity, bounded_tool_arguments, bounded_tool_name,
    proxied_arguments,
};
use crate::tool_detail::{capture_write_before, parse_edit_locations, project_tool_detail_source};
use serde_json::Value;
use thiserror::Error;
use wisp_protocol::MAX_APPLICATION_FRAME_BYTES;
use wisp_protocol::commands::QueueKind;
use wisp_protocol::events::{
    ConnectionCatalogSnapshot, DeviceCodeChallenge, DeviceCodeProgress, WispCurrentLiveEventOutput,
};

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

pub(super) const DISCOVERY_REPORT_MAX_BYTES: usize = 1024 * 1024;

/// Count the serialized payload without allocating another unbounded copy.
struct DiscoveryBudget(usize);

impl std::io::Write for DiscoveryBudget {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self
            .0
            .checked_sub(bytes.len())
            .ok_or_else(|| std::io::Error::other("discovery report exceeds retention limit"))?;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

fn discovery_report<T>(
    value: &Value,
    project: impl FnOnce() -> Option<T>,
) -> Result<Arc<T>, String> {
    if serde_json::to_writer(DiscoveryBudget(DISCOVERY_REPORT_MAX_BYTES), value).is_err() {
        return Err("Discovery report exceeds the 1 MiB display limit. Press r to retry.".into());
    }
    project()
        .map(Arc::new)
        .ok_or_else(|| "Discovery report could not be displayed. Press r to retry.".into())
}

impl BackendEvent {
    /// Project an already validated live event into reducer-owned semantics.
    pub fn from_live(event: &WispCurrentLiveEventOutput) -> Result<Self, EventProjectionError> {
        let value = event.to_value()?;
        let event_type = string_field(&value, "<unknown>", "type")?;
        match event_type.as_str() {
            "rpc.subagents" => {
                let command_id = exact_string_field(&value, &event_type, "command_id", 256)?;
                let subagents = discovery_report(&value["subagents"], || {
                    serde_json::from_value(value["subagents"].clone()).ok()
                });
                return Ok(Self::SubagentsReported {
                    command_id,
                    subagents,
                });
            }
            "rpc.codemode" => {
                let command_id = exact_string_field(&value, &event_type, "command_id", 256)?;
                let codemode = discovery_report(&value["codemode"], || {
                    serde_json::from_value(value["codemode"].clone()).ok()
                });
                return Ok(Self::CodemodeReported {
                    command_id,
                    codemode,
                });
            }
            "rpc.permissions" => {
                let command_id = exact_string_field(&value, &event_type, "command_id", 256)?;
                let permissions = discovery_report(&value["permissions"], || {
                    serde_json::from_value(value["permissions"].clone()).ok()
                });
                return Ok(Self::PermissionsReported {
                    command_id,
                    permissions,
                });
            }
            "rpc.git_diff" => {
                let command_id = exact_string_field(&value, &event_type, "command_id", 256)?;
                let snapshot =
                    discovery_report(&value, || serde_json::from_value(value.clone()).ok())
                        .map_err(|_| {
                            "Git diff exceeds the display limit or could not be decoded.".into()
                        });
                return Ok(Self::GitDiffReported {
                    command_id,
                    snapshot,
                });
            }
            "rpc.project_files" => {
                let command_id = exact_string_field(&value, &event_type, "command_id", 256)?;
                let snapshot = discovery_report(&value, || event.project_files(&command_id))
                    .map_err(|_| "Project discovery report exceeds the 1 MiB display limit or could not be decoded.".into());
                return Ok(Self::ProjectFilesReported {
                    command_id,
                    snapshot,
                });
            }
            "project_files.invalidated" => {
                return Ok(Self::ProjectFilesInvalidated(u64_field(
                    &value,
                    &event_type,
                    "generation",
                )?));
            }
            "rpc.skills" => {
                let command_id = exact_string_field(&value, &event_type, "command_id", 256)?;
                let catalog =
                    discovery_report(&value["catalog"], || event.skill_catalog(&command_id));
                return Ok(Self::SkillCatalogReported {
                    command_id,
                    catalog,
                });
            }
            "skill.catalog.updated" => {
                return Ok(Self::SkillCatalogUpdated(discovery_report(
                    &value["catalog"],
                    || event.updated_skill_catalog(),
                )));
            }
            "rpc.mcp" => {
                let command_id = exact_string_field(&value, &event_type, "command_id", 256)?;
                let status = discovery_report(&value["status"], || event.mcp_status(&command_id));
                return Ok(Self::McpStatusReported { command_id, status });
            }
            "mcp.status.updated" => {
                return Ok(Self::McpStatusUpdated(discovery_report(
                    &value["status"],
                    || event.updated_mcp_status(),
                )));
            }
            "rpc.mcp.login" => {
                let command_id = exact_string_field(&value, &event_type, "command_id", 256)?;
                let name = exact_string_field(&value, &event_type, "name", 256)?;
                let authorization_url =
                    exact_string_field(&value, &event_type, "authorization_url", 8_192 * 4)?;
                return Ok(Self::McpLoginStarted {
                    command_id,
                    name,
                    authorization_url,
                });
            }
            "rpc.mcp.preview" => {
                let command_id = exact_string_field(&value, &event_type, "command_id", 256)?;
                let servers =
                    discovery_report(&value["servers"], || event.mcp_snippet_preview(&command_id));
                return Ok(Self::McpSnippetPreviewed {
                    command_id,
                    servers,
                });
            }
            "session.stats" => {
                let command_id = exact_string_field(&value, &event_type, "command_id", 256)?;
                let mut stats = event.session_stats(&command_id).ok_or_else(|| {
                    EventProjectionError::InvalidField {
                        event_type: event_type.clone(),
                        field: "stats",
                    }
                })?;
                if stats
                    .session_id
                    .as_ref()
                    .is_some_and(|id| id.len() > SESSION_ID_MAX_BYTES)
                {
                    return Err(EventProjectionError::OversizedField {
                        event_type,
                        field: "stats.session_id",
                        limit: SESSION_ID_MAX_BYTES,
                    });
                }
                // All retained prose is bounded independently of the negotiated wire limit.
                if let Some(policy) = &mut stats.compaction {
                    policy.threshold_ineligible_reason = policy
                        .threshold_ineligible_reason
                        .as_ref()
                        .map(|reason| super::bounded_session_text(reason, 1024));
                }
                if stats.cost.known_usd.len() > 128 {
                    return Err(EventProjectionError::OversizedField {
                        event_type,
                        field: "stats.cost.known_usd",
                        limit: 128,
                    });
                }
                return Ok(Self::SessionStatsReported {
                    command_id,
                    stats: Box::new(stats),
                });
            }
            "context.estimated" => {
                let mut estimate = event.context_estimated().ok_or_else(|| {
                    EventProjectionError::InvalidField {
                        event_type: event_type.clone(),
                        field: "budget",
                    }
                })?;
                estimate.provider = exact_string_field(&value, &event_type, "provider", 256)?;
                if estimate
                    .model
                    .as_ref()
                    .is_some_and(|model| model.len() > 1024)
                {
                    return Err(EventProjectionError::OversizedField {
                        event_type,
                        field: "model",
                        limit: 1024,
                    });
                }
                return Ok(Self::ContextEstimated(estimate));
            }
            "compaction.started" => {
                let mut started = event.compaction_started().ok_or_else(|| {
                    EventProjectionError::InvalidField {
                        event_type: event_type.clone(),
                        field: "compaction",
                    }
                })?;
                started.session_id =
                    exact_string_field(&value, &event_type, "session_id", SESSION_ID_MAX_BYTES)?;
                return Ok(Self::CompactionStarted(started));
            }
            "compaction.completed" => {
                let mut completed = event.compaction_completed().ok_or_else(|| {
                    EventProjectionError::InvalidField {
                        event_type: event_type.clone(),
                        field: "compaction",
                    }
                })?;
                completed.session_id =
                    exact_string_field(&value, &event_type, "session_id", SESSION_ID_MAX_BYTES)?;
                completed.error = completed
                    .error
                    .as_ref()
                    .map(|error| super::bounded_session_text(error, 1024));
                return Ok(Self::CompactionCompleted(completed));
            }
            _ => {}
        }
        let command_id = match event_type.as_str() {
            "rpc.connection_catalog"
            | "rpc.model_catalog"
            | "rpc.commands"
            | "rpc.state"
            | "rpc.device_code"
            | "rpc.device_code.progress" => {
                exact_string_field(&value, &event_type, "command_id", 256)?
            }
            _ => return Self::from_projection_value(&value),
        };
        match event_type.as_str() {
            "rpc.commands" => event
                .command_catalog(&command_id)
                .map(|mut catalog| {
                    catalog.sort_by(|a, b| (a.order, &a.name).cmp(&(b.order, &b.name)));
                    for descriptor in &mut catalog {
                        descriptor.description =
                            super::bounded_session_text(&descriptor.description, 1024);
                    }
                    Self::CommandCatalogReported {
                        command_id,
                        catalog: catalog.into(),
                    }
                })
                .ok_or(EventProjectionError::InvalidField {
                    event_type,
                    field: "commands",
                }),
            "rpc.state" => {
                let background_processes =
                    background_processes(&value["state"]["background_processes"], &event_type)?;
                event
                    .agent_mode(&command_id)
                    .map(|mode| Self::StateReported {
                        command_id,
                        mode,
                        web_search: value["state"]["web_search"].as_bool().unwrap_or(false),
                        background_processes,
                    })
                    .ok_or(EventProjectionError::InvalidField {
                        event_type,
                        field: "state.mode",
                    })
            }
            "rpc.model_catalog" => event
                .model_catalog(&command_id)
                .map(|catalog| Self::ModelCatalogReported {
                    command_id,
                    catalog,
                })
                .ok_or(EventProjectionError::InvalidField {
                    event_type,
                    field: "catalog",
                }),
            "rpc.connection_catalog" => event
                .connection_catalog(&command_id)
                .map(|catalog| Self::ConnectionCatalogReported {
                    command_id,
                    catalog,
                })
                .ok_or(EventProjectionError::InvalidField {
                    event_type,
                    field: "command_id",
                }),
            "rpc.device_code" => event
                .device_code(&command_id)
                .map(|challenge| Self::DeviceCodeReported {
                    command_id,
                    challenge,
                })
                .ok_or(EventProjectionError::InvalidField {
                    event_type,
                    field: "command_id",
                }),
            "rpc.device_code.progress" => event
                .device_code_progress(&command_id)
                .map(|progress| Self::DeviceCodeProgress {
                    command_id,
                    progress,
                })
                .ok_or(EventProjectionError::InvalidField {
                    event_type,
                    field: "command_id",
                }),
            _ => unreachable!("connection events were selected above"),
        }
    }

    /// Project a bounded trace event. The caller owns trace-schema validation.
    pub fn from_projection_value(value: &Value) -> Result<Self, EventProjectionError> {
        let event_type = string_field(value, "<unknown>", "type")?;
        let projected = match event_type.as_str() {
            "error" | "warning" => Self::Diagnostic(super::bounded_session_text(
                &string_field(value, &event_type, "message")?,
                super::SESSION_NOTICE_MAX_BYTES,
            )),
            "process.ended" => {
                let exit_code = optional_i64_field(value, &event_type, "exit_code")?
                    .map(i32::try_from)
                    .transpose()
                    .map_err(|_| EventProjectionError::InvalidField {
                        event_type: event_type.clone(),
                        field: "exit_code",
                    })?;
                Self::ProcessEnded {
                    process_id: exact_string_field(value, &event_type, "process_id", 128)?,
                    command: string_field(value, &event_type, "command")?,
                    state: ended_process_state(value, &event_type, exit_code)?,
                    exit_code,
                    duration_ms: u64_field(value, &event_type, "duration_ms")?,
                }
            }
            "rpc.model_catalog"
            | "rpc.project_files"
            | "project_files.invalidated"
            | "rpc.skills"
            | "skill.catalog.updated"
            | "rpc.mcp"
            | "mcp.status.updated"
            | "rpc.mcp.preview"
            | "rpc.mcp.login"
            | "rpc.permissions"
            | "rpc.codemode"
            | "rpc.subagents"
            | "rpc.commands"
            | "rpc.state"
            | "session.stats"
            | "context.estimated"
            | "compaction.started"
            | "compaction.completed" => {
                let event = wisp_protocol::events::deserialize(value.clone()).map_err(|_| {
                    EventProjectionError::InvalidField {
                        event_type: event_type.clone(),
                        field: "catalog",
                    }
                })?;
                return Self::from_live(&event);
            }
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
            "web.search" => Self::WebSearch {
                turn: u64_field(value, &event_type, "turn")?,
                search: crate::web_search::search(value),
            },
            // A record with no previous folder is a session's starting folder, not a move.
            "session.directory_changed" if value.get("previous_cwd").is_none_or(Value::is_null) => {
                Self::Other { event_type }
            }
            "session.directory_changed" => Self::DirectoryChanged {
                previous: string_field(value, &event_type, "previous_cwd")?,
                cwd: string_field(value, &event_type, "cwd")?,
            },
            "rpc.workspace" => Self::WorkspaceReported {
                cwd: string_field(value, &event_type, "cwd")?,
                git_branch: value
                    .get("git_branch")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            },
            "agent.completed" => Self::RunCompleted(crate::transcript::RunTotals {
                outcome: string_field(value, &event_type, "outcome")?,
                duration_ms: optional_u64_field(value, &event_type, "duration_ms")?,
                tool_calls: u64_field_or(value, &event_type, "tool_calls", 0)?,
                output_tokens: optional_u64_field(value, &event_type, "output_tokens")?,
                model_ms: optional_u64_field(value, &event_type, "model_ms")?,
                mode: optional_string_field(value, &event_type, "mode")?,
                model: optional_string_field(value, &event_type, "model")?,
                effort: optional_string_field(value, &event_type, "effort")?,
            }),
            "message.completed" => Self::MessageCompleted {
                turn: u64_field(value, &event_type, "turn")?,
                content: string_field(value, &event_type, "content")?,
                cost: crate::web_search::message_cost(value),
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
                // A proxied call (on-demand MCP's `mcp_call`) asks to approve the tool it
                // runs; the panel and any session grant must name that tool, not the proxy,
                // and show that tool's own arguments, as its card does. Only the backend's
                // `target` makes a call proxied: a tool of the user's own named `mcp_call`
                // is shown as itself, whatever its arguments name.
                let target = optional_string_field(value, &event_type, "target")?;
                let name = string_field_ref(value, &event_type, "name")?;
                let arguments = object_field(value, &event_type, "arguments")?;
                let (raw_name, arguments) = match target.as_deref() {
                    Some(target) => (
                        target,
                        proxied_arguments(name, arguments, target).unwrap_or(arguments),
                    ),
                    None => (name, arguments),
                };
                // Sessions saved before this field existed only hold grantable tools.
                let grants_allowed = bool_field_or(value, &event_type, "grants_allowed", true)?;
                let session_only_yolo =
                    bool_field_or(value, &event_type, "session_only_yolo", false)?;
                Self::ToolApprovalRequested(PendingApproval {
                    call_id: string_field(value, &event_type, "call_id")?,
                    detail_source: project_tool_detail_source(
                        raw_name,
                        arguments
                            .as_object()
                            .expect("object_field returned an object"),
                    ),
                    // A confirmation shows exactly what it confirms, so its arguments
                    // are kept whole (the backend validated and bounded them before
                    // asking); one too large to keep is dropped and cannot be confirmed.
                    arguments: if grants_allowed {
                        bounded_tool_arguments(raw_name, arguments)
                    } else {
                        complete_confirmation_arguments(arguments)
                    },
                    name: bounded_tool_name(raw_name),
                    safety: bounded_display_string_field(value, &event_type, "safety", 128)?,
                    grants_allowed,
                    session_only_yolo,
                })
            }
            // A proxied call's target is resolved before any approval; its card shows
            // that tool from here on. Every other start changes nothing the TUI shows.
            "tool.execution.started" => {
                let call_id = bounded_identity(&string_field(value, &event_type, "call_id")?);
                let name = string_field_ref(value, &event_type, "name")?;
                let arguments = object_field(value, &event_type, "arguments")?;
                let resolved =
                    optional_string_field(value, &event_type, "target")?.and_then(|target| {
                        let inner = proxied_arguments(name, arguments, &target)?;
                        Some(ToolCallInput {
                            call_id,
                            detail_source: project_tool_detail_source(
                                &target,
                                inner.as_object().expect("proxied arguments are an object"),
                            ),
                            arguments: bounded_tool_arguments(&target, inner),
                            name: bounded_tool_name(&target),
                        })
                    });
                match resolved {
                    Some(resolved) => Self::ToolTargetResolved(resolved),
                    None => Self::Other { event_type },
                }
            }
            "tool.approval.resolved" => Self::ToolApprovalResolved {
                call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
                name: bounded_tool_name(target_or_name(value, &event_type)?),
                approved: bool_field(value, &event_type, "approved")?,
                reason: optional_bounded_display_string_field(value, &event_type, "reason", 512)?,
            },
            "tool.execution.ended" => Self::ToolExecutionEnded {
                call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
            },
            // A script's calls attach to the script's own card, by its call id.
            "tool.nested.started" => {
                let name = string_field_ref(value, &event_type, "name")?;
                let target = optional_string_field(value, &event_type, "target")?;
                Self::ScriptCallStarted {
                    parent_call_id: bounded_identity(&string_field(
                        value,
                        &event_type,
                        "parent_call_id",
                    )?),
                    call_id: string_field(value, &event_type, "call_id")?,
                    tool: target.clone().unwrap_or_else(|| name.to_owned()),
                    arguments: crate::script_card::call_arguments(
                        name,
                        target.as_deref(),
                        value.get("arguments"),
                    ),
                }
            }
            "tool.nested.ended" => Self::ScriptCallEnded {
                parent_call_id: bounded_identity(&string_field(
                    value,
                    &event_type,
                    "parent_call_id",
                )?),
                call: value
                    .get("call")
                    .and_then(crate::script_card::ScriptCall::from_record)
                    .ok_or_else(|| EventProjectionError::InvalidField {
                        event_type: event_type.clone(),
                        field: "call",
                    })?,
            },
            "tool.execution.progress" => Self::ToolExecutionProgress {
                call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
                progress: ToolProgressInput {
                    sequence: u64_field(value, &event_type, "sequence")?,
                    stdout_tail: string_field(value, &event_type, "stdout_tail")?,
                    stderr_tail: string_field(value, &event_type, "stderr_tail")?,
                    stdout_bytes: u64_field(value, &event_type, "stdout_bytes")?,
                    stderr_bytes: u64_field(value, &event_type, "stderr_bytes")?,
                },
            },
            "tool.result" => {
                let raw_name = target_or_name(value, &event_type)?;
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
                    nested: crate::script_card::ScriptResult::from_fields(
                        value.get("nested_calls"),
                        value.get("nested_calls_omitted"),
                    ),
                    images: composer_images::reported_image_lines(value.get("images")),
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
                    duration_ms: optional_u64_field(value, &event_type, "duration_ms")?,
                    started_at_ms: optional_timestamp_ms_field(value, &event_type, "started_at")?,
                    process_started_at_ms: optional_timestamp_ms_field(
                        value,
                        &event_type,
                        "process_started_at",
                    )?,
                    process_ended_at_ms: optional_timestamp_ms_field(
                        value,
                        &event_type,
                        "process_ended_at",
                    )?,
                    edit_locations: parse_edit_locations(value.get("edit_locations")).map_err(
                        |()| EventProjectionError::InvalidField {
                            event_type: event_type.clone(),
                            field: "edit_locations",
                        },
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
                let image_counts = |field, message_count| {
                    queued_images(value, &event_type, field, message_count)
                        .map(|images| images.iter().map(ComposerImages::len).collect::<Vec<_>>())
                };
                let steering_image_counts = image_counts("steering_images", steering.len());
                let follow_up_image_counts = image_counts("follow_up_images", follow_up.len());
                if value.get("steering_mode").is_some()
                    || value.get("token").is_some()
                    || value.get("command_id").is_some()
                {
                    Self::QueueSnapshot(super::queue_management::Snapshot {
                        steering_image_counts: steering_image_counts?,
                        follow_up_image_counts: follow_up_image_counts?,
                        steering: queue_strings(steering),
                        follow_up: queue_strings(follow_up),
                        steering_mode: queue_mode(value, &event_type, "steering_mode")?,
                        follow_up_mode: queue_mode(value, &event_type, "follow_up_mode")?,
                        token: optional_exact_string_field(value, &event_type, "token", 128)?,
                        command_id: optional_exact_string_field(
                            value,
                            &event_type,
                            "command_id",
                            256,
                        )?,
                    })
                } else {
                    Self::QueueUpdated {
                        steering_image_counts: steering_image_counts?,
                        follow_up_image_counts: follow_up_image_counts?,
                        steering: queue_strings(steering),
                        follow_up: queue_strings(follow_up),
                    }
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
                    steering_images: queued_images(
                        value,
                        &event_type,
                        "steering_images",
                        steering.len(),
                    )?,
                    follow_up_images: queued_images(
                        value,
                        &event_type,
                        "follow_up_images",
                        follow_up.len(),
                    )?,
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
                    images: composer_images::reported_image_lines(value.get("images")),
                }
            }
            "rpc.sessions" => Self::SessionsReported {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                sessions: session_summaries(value, &event_type)?,
                navigation: CatalogNavigation {
                    next_cursor: optional_exact_string_field(
                        value,
                        &event_type,
                        "next_cursor",
                        4096,
                    )?,
                    previous_cursor: optional_exact_string_field(
                        value,
                        &event_type,
                        "previous_cursor",
                        4096,
                    )?,
                },
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
                let source = array_field(value, &event_type, "messages")?;
                let started = std::time::Instant::now();
                let projected = project_rpc_message_page_with_origins(
                    source,
                    bool_field(value, &event_type, "truncated")?,
                );
                crate::hydration_profile::record(
                    "rust.page_projection",
                    started.elapsed(),
                    source.len(),
                );
                match projected {
                    Ok(page) => Self::MessagesReported {
                        command_id,
                        messages: SessionMessages {
                            source_messages: {
                                let started = std::time::Instant::now();
                                let retained = source.into();
                                crate::hydration_profile::record(
                                    "rust.page_clone",
                                    started.elapsed(),
                                    source.len(),
                                );
                                retained
                            },
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
            "rpc.connection_catalog" => Self::ConnectionCatalogReported {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                catalog: connection_catalog(value, &event_type)?,
            },
            "rpc.device_code" => Self::DeviceCodeReported {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                challenge: DeviceCodeChallenge {
                    provider: exact_string_field(value, &event_type, "provider", 128)?,
                    verification_uri: exact_string_field(
                        value,
                        &event_type,
                        "verification_uri",
                        512,
                    )?,
                    user_code: exact_string_field(value, &event_type, "user_code", 128)?,
                },
            },
            "rpc.device_code.progress" => Self::DeviceCodeProgress {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                progress: DeviceCodeProgress {
                    provider: exact_string_field(value, &event_type, "provider", 128)?,
                    attempt: bounded_device_attempt(value, &event_type)?,
                },
            },
            "rpc.command.started"
                if value.get("command_type").and_then(Value::as_str) == Some("process_wake") =>
            {
                Self::WakeStarted {
                    command_id: exact_string_field(value, &event_type, "command_id", 256)?,
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
        if let Some(entry_id) =
            optional_exact_string_field(value, "live message", "message_entry_id", 4096)?
        {
            let tool_call_ids = value
                .get("tool_calls")
                .and_then(Value::as_array)
                .map(|calls| {
                    calls
                        .iter()
                        .take(128)
                        .filter_map(|call| {
                            call.get("call_id")
                                .and_then(Value::as_str)
                                .map(bounded_identity)
                        })
                        .collect()
                })
                .unwrap_or_default();
            return Ok(Self::MessageOrigin {
                entry_id,
                tool_call_ids,
                event: Box::new(projected),
            });
        }
        Ok(projected)
    }
}

fn connection_catalog(
    value: &Value,
    event_type: &str,
) -> Result<ConnectionCatalogSnapshot, EventProjectionError> {
    serde_json::from_value(value["catalog"].clone()).map_err(|_| {
        EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "catalog",
        }
    })
}

fn bounded_device_attempt(value: &Value, event_type: &str) -> Result<u32, EventProjectionError> {
    let attempt = u64_field(value, event_type, "attempt")?;
    if !(1..=10_000).contains(&attempt) {
        return Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "attempt",
        });
    }
    Ok(attempt as u32)
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
        selected_images: if forked {
            stored_images(value.get("selected_images"), event_type, "selected_images")?
        } else {
            ComposerImages::default()
        },
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
        editor_images: stored_images(value.get("editor_images"), event_type, "editor_images")?,
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

fn queue_mode(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<wisp_protocol::commands::QueueMode, EventProjectionError> {
    use wisp_protocol::commands::QueueMode;
    match value.get(field).and_then(Value::as_str) {
        Some("all") => Ok(QueueMode::All),
        Some("one_at_a_time") | None => Ok(QueueMode::OneAtATime),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.into(),
            field,
        }),
    }
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

/// One message's stored images, from an array of `ImagePart` references.
fn stored_images(
    value: Option<&Value>,
    event_type: &str,
    field: &'static str,
) -> Result<ComposerImages, EventProjectionError> {
    let invalid = || EventProjectionError::InvalidField {
        event_type: event_type.to_owned(),
        field,
    };
    let Some(value) = value else {
        return Ok(ComposerImages::default());
    };
    // A stored message may carry more images than one command accepts; the
    // composer's limit applies when they are restored, not here.
    value
        .as_array()
        .ok_or_else(invalid)?
        .iter()
        .map(|part| AttachedImage::stored(part).ok_or_else(invalid))
        .collect()
}

/// Each queued message's stored images, which the backend sends either empty or
/// aligned one entry per message.
fn queued_images(
    value: &Value,
    event_type: &str,
    field: &'static str,
    message_count: usize,
) -> Result<Vec<ComposerImages>, EventProjectionError> {
    let Some(images) = value.get(field) else {
        return Ok(Vec::new());
    };
    let entries = images
        .as_array()
        .filter(|entries| entries.is_empty() || entries.len() == message_count)
        .ok_or_else(|| EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        })?;
    entries
        .iter()
        .map(|entry| stored_images(Some(entry), event_type, field))
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

/// The tool a tool lifecycle event's call ran: the `target` a proxied call (on-demand
/// MCP's `mcp_call`) names, otherwise the tool the model called.
fn target_or_name<'a>(value: &'a Value, event_type: &str) -> Result<&'a str, EventProjectionError> {
    let name = string_field_ref(value, event_type, "name")?;
    match value.get("target") {
        None | Some(Value::Null) => Ok(name),
        Some(Value::String(target)) => Ok(target),
        Some(_) => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "target",
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

/// The running processes a `get_state` reply lists; an absent list is empty.
fn background_processes(
    value: &Value,
    event_type: &str,
) -> Result<Vec<super::background_processes::BackgroundProcess>, EventProjectionError> {
    let Some(entries) = value.as_array() else {
        return if value.is_null() {
            Ok(Vec::new())
        } else {
            Err(EventProjectionError::InvalidField {
                event_type: event_type.to_owned(),
                field: "state.background_processes",
            })
        };
    };
    entries
        .iter()
        .map(|entry| {
            Ok(super::background_processes::BackgroundProcess {
                process_id: exact_string_field(entry, event_type, "process_id", 128)?,
                command: string_field(entry, event_type, "command")?,
                started_at_ms: optional_i64_field(entry, event_type, "started_at_ms")?,
            })
        })
        .collect()
}

/// How the card of a process that stopped on its own reads: a non-zero exit fails.
fn ended_process_state(
    value: &Value,
    event_type: &str,
    exit_code: Option<i32>,
) -> Result<crate::tool_cards::ProcessDisplayState, EventProjectionError> {
    use crate::tool_cards::ProcessDisplayState;
    match string_field_ref(value, event_type, "state")? {
        "completed" if exit_code == Some(0) => Ok(ProcessDisplayState::Completed),
        "completed" | "failed" => Ok(ProcessDisplayState::Failed),
        "timed_out" => Ok(ProcessDisplayState::TimedOut),
        "cancelled" => Ok(ProcessDisplayState::Cancelled),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "state",
        }),
    }
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

fn optional_u64_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<Option<u64>, EventProjectionError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_u64()
            .map(Some)
            .ok_or_else(|| EventProjectionError::InvalidField {
                event_type: event_type.to_owned(),
                field,
            }),
    }
}

/// Read an RFC 3339 time as Unix milliseconds.
fn optional_timestamp_ms_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
) -> Result<Option<i64>, EventProjectionError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_str()
            .and_then(crate::tool_cards::timestamp_ms)
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

/// Largest confirmation argument object kept whole for display.
const CONFIRMATION_ARGUMENTS_MAX_BYTES: usize = 64 * 1024;

fn complete_confirmation_arguments(arguments: &Value) -> Value {
    match serde_json::to_vec(arguments) {
        Ok(encoded) if encoded.len() <= CONFIRMATION_ARGUMENTS_MAX_BYTES => arguments.clone(),
        _ => Value::Null,
    }
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
