use std::collections::VecDeque;
use std::sync::Arc;

use super::{
    BackendEvent, BrowserAddresses, BrowserViewportInput, CatalogNavigation, MessageContentKind,
    PendingApproval, QUEUE_CONTENT_BYTES_LIMIT, QUEUE_MESSAGE_LIMIT, QueueRemovalOperation,
    SESSION_CATALOG_LIMIT, SESSION_ENTRY_COUNT_MAX, SESSION_ID_MAX_BYTES, SESSION_LABEL_MAX_BYTES,
    SESSION_PATH_MAX_BYTES, SESSION_TREE_PAGE_LIMIT, SESSION_UPDATED_AT_MAX_BYTES,
    SessionDerivation, SessionIdentity, SessionMessages, SessionNameChange, SessionSummary,
    SessionTreeNavigation, SessionTreeNode, SessionTreeNodeKind, SessionTreePage,
    SessionTreeUnrevert,
};
use crate::composer_images::{self, AttachedImage, ComposerImages};
use crate::history::project_rpc_message_page_with_origins;
use crate::tool_cards::{
    BoundedText, TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES, ToolCallInput, ToolProgressInput,
    ToolResultInput, bounded_identity, bounded_tool_arguments, bounded_tool_arguments_map,
    bounded_tool_name, proxied_arguments, proxied_arguments_map,
};
use crate::tool_detail::{
    capture_write_before, capture_write_before_str, edit_locations_from_wire, parse_edit_locations,
    project_tool_detail_source,
};
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
    value: &(impl serde::Serialize + ?Sized),
    project: impl FnOnce() -> Option<T>,
) -> Result<Arc<T>, String> {
    if serde_json::to_writer(DiscoveryBudget(DISCOVERY_REPORT_MAX_BYTES), value).is_err() {
        return Err("Discovery report exceeds the 1 MiB display limit. Press r to retry.".into());
    }
    project()
        .map(Arc::new)
        .ok_or_else(|| "Discovery report could not be displayed. Press r to retry.".into())
}

fn bounded_diagnostic(message: &str) -> BackendEvent {
    BackendEvent::Diagnostic(super::bounded_session_text(
        message,
        super::SESSION_NOTICE_MAX_BYTES,
    ))
}

impl BackendEvent {
    /// Project an already validated live event into reducer-owned semantics.
    pub fn from_live(event: &WispCurrentLiveEventOutput) -> Result<Self, EventProjectionError> {
        use wisp_protocol::events::wire::WispCurrentLiveEventOutput as WireEvent;
        match event.payload() {
            WireEvent::ErrorEvent(payload) => Ok(bounded_diagnostic(&payload.message)),
            WireEvent::WarningEvent(payload) => Ok(bounded_diagnostic(&payload.message)),
            WireEvent::ProjectFilesInvalidated(payload) => {
                Ok(Self::ProjectFilesInvalidated(payload.generation.get()))
            }
            WireEvent::RpcProjectFilesReported(payload) => {
                let command_id =
                    exact_str(&payload.command_id, "rpc.project_files", "command_id", 256)?;
                let snapshot = discovery_report(payload, || event.project_files(&command_id))
                    .map_err(|_| {
                        "Project discovery report exceeds the 1 MiB display limit or could not be decoded."
                            .into()
                    });
                Ok(Self::ProjectFilesReported {
                    command_id,
                    directory: payload.directory.as_deref().cloned(),
                    in_progress: payload.in_progress,
                    snapshot,
                })
            }
            WireEvent::RpcSkillsReported(payload) => {
                let command_id = exact_str(&payload.command_id, "rpc.skills", "command_id", 256)?;
                let catalog =
                    discovery_report(&payload.catalog, || event.skill_catalog(&command_id));
                Ok(Self::SkillCatalogReported {
                    command_id,
                    catalog,
                })
            }
            WireEvent::SkillCatalogUpdated(payload) => Ok(Self::SkillCatalogUpdated(
                discovery_report(&payload.catalog, || event.updated_skill_catalog()),
            )),
            WireEvent::RpcMcpStatusReported(payload) => {
                let command_id = exact_str(&payload.command_id, "rpc.mcp", "command_id", 256)?;
                let status = discovery_report(&payload.status, || event.mcp_status(&command_id));
                Ok(Self::McpStatusReported { command_id, status })
            }
            WireEvent::McpStatusUpdated(payload) => Ok(Self::McpStatusUpdated(discovery_report(
                &payload.status,
                || event.updated_mcp_status(),
            ))),
            WireEvent::RpcMcpLoginStarted(payload) => Ok(Self::McpLoginStarted {
                command_id: exact_str(&payload.command_id, "rpc.mcp.login", "command_id", 256)?,
                name: exact_str(&payload.name, "rpc.mcp.login", "name", 256)?,
                authorization_url: exact_str(
                    &payload.authorization_url,
                    "rpc.mcp.login",
                    "authorization_url",
                    8_192 * 4,
                )?,
            }),
            WireEvent::RpcMcpSnippetPreviewed(payload) => {
                let command_id =
                    exact_str(&payload.command_id, "rpc.mcp.preview", "command_id", 256)?;
                let servers =
                    discovery_report(&payload.servers, || event.mcp_snippet_preview(&command_id));
                Ok(Self::McpSnippetPreviewed {
                    command_id,
                    servers,
                })
            }
            WireEvent::SessionStatsReported(payload) => {
                let command_id =
                    exact_str(&payload.command_id, "session.stats", "command_id", 256)?;
                let mut stats = event.session_stats(&command_id).ok_or_else(|| {
                    EventProjectionError::InvalidField {
                        event_type: "session.stats".into(),
                        field: "stats",
                    }
                })?;
                if stats
                    .session_id
                    .as_ref()
                    .is_some_and(|id| id.len() > SESSION_ID_MAX_BYTES)
                {
                    return Err(EventProjectionError::OversizedField {
                        event_type: "session.stats".into(),
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
                        event_type: "session.stats".into(),
                        field: "stats.cost.known_usd",
                        limit: 128,
                    });
                }
                Ok(Self::SessionStatsReported {
                    command_id,
                    stats: Box::new(stats),
                })
            }
            WireEvent::ContextEstimated(payload) => {
                let mut estimate = event.context_estimated().ok_or_else(|| {
                    EventProjectionError::InvalidField {
                        event_type: "context.estimated".into(),
                        field: "budget",
                    }
                })?;
                estimate.provider =
                    exact_str(&payload.provider, "context.estimated", "provider", 256)?;
                if estimate
                    .model
                    .as_ref()
                    .is_some_and(|model| model.len() > 1024)
                {
                    return Err(EventProjectionError::OversizedField {
                        event_type: "context.estimated".into(),
                        field: "model",
                        limit: 1024,
                    });
                }
                Ok(Self::ContextEstimated(estimate))
            }
            WireEvent::CompactionStarted(payload) => {
                let mut started = event.compaction_started().ok_or_else(|| {
                    EventProjectionError::InvalidField {
                        event_type: "compaction.started".into(),
                        field: "compaction",
                    }
                })?;
                started.session_id = exact_str(
                    &payload.session_id,
                    "compaction.started",
                    "session_id",
                    SESSION_ID_MAX_BYTES,
                )?;
                Ok(Self::CompactionStarted(started))
            }
            WireEvent::CompactionCompleted(payload) => {
                let mut completed = event.compaction_completed().ok_or_else(|| {
                    EventProjectionError::InvalidField {
                        event_type: "compaction.completed".into(),
                        field: "compaction",
                    }
                })?;
                completed.session_id = exact_str(
                    &payload.session_id,
                    "compaction.completed",
                    "session_id",
                    SESSION_ID_MAX_BYTES,
                )?;
                completed.error = completed
                    .error
                    .as_ref()
                    .map(|error| super::bounded_session_text(error, 1024));
                Ok(Self::CompactionCompleted(completed))
            }
            WireEvent::RpcBrowserAddressesReported(payload) => {
                Ok(Self::BrowserAddressesReported(BrowserAddresses {
                    trusted: payload.trusted,
                    addresses: payload
                        .addresses
                        .iter()
                        .map(|address| address.as_str().to_owned())
                        .collect(),
                }))
            }
            WireEvent::BrowserViewportUpdated(_) => {
                let update = event.browser_viewport_updated().ok_or_else(|| {
                    EventProjectionError::InvalidField {
                        event_type: "browser.viewport.updated".into(),
                        field: "browser.viewport.updated",
                    }
                })?;
                let live = update.live_png.is_some() || update.window_mode;
                let live_image = update.live_png.as_deref().and_then(|png| {
                    composer_images::transcript_image_from_live_png(
                        png,
                        update.viewport_width.zip(update.viewport_height),
                    )
                });
                if update.live_png.is_some() && live_image.is_none() {
                    return Err(EventProjectionError::InvalidField {
                        event_type: "browser.viewport.updated".into(),
                        field: "live_png",
                    });
                }
                let image = live_image.or_else(|| {
                    update.image.as_ref().and_then(|img| {
                        composer_images::transcript_image_from_stored(
                            &img.media_type,
                            &img.sha256,
                            img.byte_size,
                            img.width,
                            img.height,
                        )
                    })
                });
                Ok(Self::BrowserViewportUpdated(Box::new(
                    BrowserViewportInput {
                        live,
                        window_mode: update.window_mode,
                        live_device_scale: update.live_device_scale,
                        image,
                        url: update.url,
                        title: update.title,
                        viewport_width: update.viewport_width,
                        viewport_height: update.viewport_height,
                        source_tool: update.source_tool,
                        timestamp: update.timestamp,
                    },
                )))
            }
            WireEvent::RpcCommandsReported(payload) => {
                let command_id = exact_str(&payload.command_id, "rpc.commands", "command_id", 256)?;
                let mut catalog = event.command_catalog(&command_id).ok_or(
                    EventProjectionError::InvalidField {
                        event_type: "rpc.commands".into(),
                        field: "commands",
                    },
                )?;
                catalog.sort_by(|a, b| (a.order, &a.name).cmp(&(b.order, &b.name)));
                for descriptor in &mut catalog {
                    descriptor.description =
                        super::bounded_session_text(&descriptor.description, 1024);
                }
                Ok(Self::CommandCatalogReported {
                    command_id,
                    catalog: catalog.into(),
                })
            }
            WireEvent::RpcModelCatalogReported(payload) => {
                let command_id =
                    exact_str(&payload.command_id, "rpc.model_catalog", "command_id", 256)?;
                let catalog =
                    event
                        .model_catalog(&command_id)
                        .ok_or(EventProjectionError::InvalidField {
                            event_type: "rpc.model_catalog".into(),
                            field: "catalog",
                        })?;
                Ok(Self::ModelCatalogReported {
                    command_id,
                    catalog,
                })
            }
            WireEvent::RpcConnectionCatalogReported(payload) => {
                let command_id = exact_str(
                    &payload.command_id,
                    "rpc.connection_catalog",
                    "command_id",
                    256,
                )?;
                let catalog = event.connection_catalog(&command_id).ok_or(
                    EventProjectionError::InvalidField {
                        event_type: "rpc.connection_catalog".into(),
                        field: "command_id",
                    },
                )?;
                Ok(Self::ConnectionCatalogReported {
                    command_id,
                    catalog,
                })
            }
            WireEvent::RpcDeviceCodeReported(payload) => {
                let command_id =
                    exact_str(&payload.command_id, "rpc.device_code", "command_id", 256)?;
                let challenge =
                    event
                        .device_code(&command_id)
                        .ok_or(EventProjectionError::InvalidField {
                            event_type: "rpc.device_code".into(),
                            field: "command_id",
                        })?;
                Ok(Self::DeviceCodeReported {
                    command_id,
                    challenge,
                })
            }
            WireEvent::RpcDeviceCodeProgressReported(payload) => {
                let command_id = exact_str(
                    &payload.command_id,
                    "rpc.device_code.progress",
                    "command_id",
                    256,
                )?;
                let progress = event.device_code_progress(&command_id).ok_or(
                    EventProjectionError::InvalidField {
                        event_type: "rpc.device_code.progress".into(),
                        field: "command_id",
                    },
                )?;
                Ok(Self::DeviceCodeProgress {
                    command_id,
                    progress,
                })
            }
            WireEvent::RpcStateReported(payload) => {
                let command_id = exact_str(&payload.command_id, "rpc.state", "command_id", 256)?;
                let background_processes =
                    wire_background_processes(&payload.state.background_processes, "rpc.state")?;
                let mode = match payload.state.mode {
                    wisp_protocol::events::wire::AgentMode::Build => {
                        wisp_protocol::commands::AgentMode::Build
                    }
                    wisp_protocol::events::wire::AgentMode::Plan => {
                        wisp_protocol::commands::AgentMode::Plan
                    }
                };
                Ok(Self::StateReported {
                    command_id,
                    mode,
                    web_search: payload.state.web_search,
                    background_processes,
                })
            }
            WireEvent::RpcProcessesReported(payload) => {
                let command_id =
                    exact_str(&payload.command_id, "rpc.processes", "command_id", 256)?;
                let processes = payload
                    .processes
                    .iter()
                    .map(|process| wire_process_report(process, "rpc.processes"))
                    .collect::<Result<_, _>>()?;
                Ok(Self::ProcessesReported {
                    command_id,
                    processes,
                })
            }
            WireEvent::RpcProcessOutputReported(payload) => {
                let command_id =
                    exact_str(&payload.command_id, "rpc.process_output", "command_id", 256)?;
                let process = wire_process_report(&payload.process, "rpc.process_output")?;
                let chunks = payload
                    .chunks
                    .iter()
                    .map(|chunk| {
                        let stream = match chunk.stream {
                            wisp_protocol::events::wire::RpcProcessOutputChunkStream::Stdout => {
                                super::background_processes::OutputStream::Stdout
                            }
                            wisp_protocol::events::wire::RpcProcessOutputChunkStream::Stderr => {
                                super::background_processes::OutputStream::Stderr
                            }
                        };
                        (stream, chunk.text.clone())
                    })
                    .collect();
                Ok(Self::ProcessOutputReported {
                    command_id,
                    output: std::sync::Arc::new(super::background_processes::ProcessOutput {
                        process,
                        chunks,
                        omitted_bytes: payload.omitted_bytes,
                    }),
                })
            }
            WireEvent::ProcessEnded(payload) => {
                let exit_code = wire_exit_code(payload.exit_code, "process.ended")?;
                let state = match payload.state {
                    wisp_protocol::events::wire::ProcessEndedState::Completed
                        if exit_code == Some(0) =>
                    {
                        crate::tool_cards::ProcessDisplayState::Completed
                    }
                    wisp_protocol::events::wire::ProcessEndedState::Completed
                    | wisp_protocol::events::wire::ProcessEndedState::Failed => {
                        crate::tool_cards::ProcessDisplayState::Failed
                    }
                    wisp_protocol::events::wire::ProcessEndedState::TimedOut => {
                        crate::tool_cards::ProcessDisplayState::TimedOut
                    }
                    wisp_protocol::events::wire::ProcessEndedState::Cancelled => {
                        crate::tool_cards::ProcessDisplayState::Cancelled
                    }
                };
                Ok(Self::ProcessEnded {
                    process_id: exact_str(&payload.process_id, "process.ended", "process_id", 128)?,
                    command: (*payload.command).clone(),
                    state,
                    exit_code,
                    duration_ms: payload.duration_ms,
                })
            }
            WireEvent::AgentStarted(payload) => wrap_message_origin(
                Self::Other {
                    event_type: "agent.started".into(),
                },
                payload.message_entry_id.as_deref().map(String::as_str),
                Vec::new(),
            ),
            WireEvent::SkillInvoked(payload) => wrap_message_origin(
                Self::Other {
                    event_type: "skill.invoked".into(),
                },
                Some(&payload.message_entry_id),
                Vec::new(),
            ),
            WireEvent::MessageStarted(payload) => {
                let turn = u64::try_from(payload.turn).map_err(|_| {
                    EventProjectionError::InvalidField {
                        event_type: "message.started".into(),
                        field: "turn",
                    }
                })?;
                Ok(Self::MessageStarted { turn })
            }
            WireEvent::MessageDelta(payload) => {
                let turn = u64::try_from(payload.turn).map_err(|_| {
                    EventProjectionError::InvalidField {
                        event_type: "message.delta".into(),
                        field: "turn",
                    }
                })?;
                let content_kind = match payload.content_kind {
                    wisp_protocol::events::wire::MessageDeltaContentKind::Text => {
                        MessageContentKind::Text
                    }
                    wisp_protocol::events::wire::MessageDeltaContentKind::Thinking => {
                        MessageContentKind::Thinking
                    }
                };
                Ok(Self::MessageDelta {
                    turn,
                    delta: payload.delta.clone(),
                    content_kind,
                })
            }
            WireEvent::MessageCompleted(payload) => {
                let turn = u64::try_from(payload.turn).map_err(|_| {
                    EventProjectionError::InvalidField {
                        event_type: "message.completed".into(),
                        field: "turn",
                    }
                })?;
                let projected = Self::MessageCompleted {
                    turn,
                    content: payload.content.clone(),
                    cost: crate::web_search::message_cost_from_wire(payload),
                    output_tokens: payload.usage.as_ref().map(|usage| usage.output_tokens),
                };
                let tool_call_ids = payload
                    .tool_calls
                    .iter()
                    .take(128)
                    .map(|call| bounded_identity(&call.call_id))
                    .collect();
                wrap_message_origin(
                    projected,
                    payload.message_entry_id.as_deref().map(String::as_str),
                    tool_call_ids,
                )
            }
            WireEvent::AgentCompleted(payload) => {
                Ok(Self::RunCompleted(crate::transcript::RunTotals {
                    outcome: payload.outcome.to_string(),
                    duration_ms: payload.duration_ms,
                    tool_calls: payload.tool_calls,
                    output_tokens: payload.output_tokens,
                    model_ms: payload.model_ms,
                    mode: payload.mode.as_ref().map(|mode| match mode {
                        wisp_protocol::events::wire::AgentMode::Build => "build".to_owned(),
                        wisp_protocol::events::wire::AgentMode::Plan => "plan".to_owned(),
                    }),
                    model: payload.model.clone(),
                    effort: payload.effort.clone(),
                }))
            }
            WireEvent::WebSearchPerformed(payload) => {
                let turn = u64::try_from(payload.turn).map_err(|_| {
                    EventProjectionError::InvalidField {
                        event_type: "web.search".into(),
                        field: "turn",
                    }
                })?;
                Ok(Self::WebSearch {
                    turn,
                    search: crate::web_search::search_from_wire(payload),
                })
            }
            WireEvent::SessionDirectoryChanged(payload) => {
                let Some(previous) = &payload.previous_cwd else {
                    return Ok(Self::Other {
                        event_type: "session.directory_changed".into(),
                    });
                };
                Ok(Self::DirectoryChanged {
                    previous: previous.clone(),
                    cwd: payload.cwd.clone(),
                })
            }
            WireEvent::RpcWorkspaceReported(payload) => Ok(Self::WorkspaceReported {
                cwd: payload.cwd.clone(),
                git_branch: payload.git_branch.clone(),
                session_images_dir: payload
                    .session_images_dir
                    .as_deref()
                    .filter(|path| !path.is_empty())
                    .map(std::path::PathBuf::from),
            }),
            WireEvent::TrustRequested(payload) => Ok(Self::TrustRequested {
                request_id: payload.request_id.clone(),
                project_path: payload.project_path.clone(),
            }),
            WireEvent::ProjectConfigApplied(payload) => Ok(Self::ProjectConfigApplied {
                provider: payload.provider.clone(),
                model: payload.model.clone(),
                effort: payload.effort.clone(),
            }),
            WireEvent::RpcCommandStarted(payload) => {
                if payload.command_type == "process_wake" {
                    return Ok(Self::WakeStarted {
                        command_id: exact_str(
                            &payload.command_id,
                            "rpc.command.started",
                            "command_id",
                            256,
                        )?,
                    });
                }
                Ok(Self::Other {
                    event_type: "rpc.command.started".into(),
                })
            }
            WireEvent::RpcCommandFinished(payload) => Ok(Self::CommandFinished {
                command_id: exact_str(
                    &payload.command_id,
                    "rpc.command.finished",
                    "command_id",
                    256,
                )?,
                command_type: BoundedText::head(&payload.command_type, 128, 8).text,
                ok: payload.ok,
                error: payload
                    .error
                    .as_deref()
                    .map(|error| BoundedText::head(error, 1024, 8).text),
            }),
            WireEvent::RpcShellProgress(payload) => Ok(Self::ShellProgress {
                command_id: exact_str(
                    &payload.command_id,
                    "rpc.shell.progress",
                    "command_id",
                    256,
                )?,
                stdout_tail: (*payload.stdout_tail).clone(),
                stderr_tail: (*payload.stderr_tail).clone(),
                stdout_bytes: payload.stdout_bytes,
                stderr_bytes: payload.stderr_bytes,
            }),
            WireEvent::RpcShellCompleted(payload) => Ok(Self::ShellCompleted {
                command_id: exact_str(
                    &payload.command_id,
                    "rpc.shell.completed",
                    "command_id",
                    256,
                )?,
                command: BoundedText::head(&payload.command, 32_768, 8).text,
                exit_code: payload.exit_code,
                output: (*payload.output).clone(),
                truncated: payload.truncated,
                timed_out: payload.timed_out,
                cancelled: payload.cancelled,
                duration_ms: payload.duration_ms,
                error: payload
                    .error
                    .as_deref()
                    .map(|error| BoundedText::head(error, 2_048, 8).text),
            }),
            WireEvent::ToolCallRequested(payload) => Ok(Self::ToolCall(ToolCallInput {
                call_id: bounded_identity(&payload.call_id),
                detail_source: project_tool_detail_source(&payload.name, &payload.arguments),
                arguments: bounded_tool_arguments_map(&payload.name, &payload.arguments),
                name: bounded_tool_name(&payload.name),
            })),
            WireEvent::ToolApprovalRequested(payload) => {
                Ok(project_live_approval_requested(payload))
            }
            WireEvent::ToolExecutionStarted(payload) => {
                let resolved = payload.target.as_deref().and_then(|target| {
                    let inner = proxied_arguments_map(&payload.name, &payload.arguments, target)?;
                    Some(ToolCallInput {
                        call_id: bounded_identity(&payload.call_id),
                        detail_source: project_tool_detail_source(target, inner),
                        arguments: bounded_tool_arguments_map(target, inner),
                        name: bounded_tool_name(target),
                    })
                });
                Ok(match resolved {
                    Some(resolved) => Self::ToolTargetResolved(resolved),
                    None => Self::Other {
                        event_type: "tool.execution.started".into(),
                    },
                })
            }
            WireEvent::ToolApprovalReviewed(payload) => Ok(Self::ToolApprovalReviewed {
                call_id: bounded_identity(&payload.call_id),
                note: format!(
                    "Command review ({}): {}",
                    payload.verdict,
                    BoundedText::head(&payload.reason, 500, 8).text,
                ),
            }),
            WireEvent::ToolApprovalResolved(payload) => {
                if payload.source.as_deref() == Some("auto") && payload.approved {
                    return Ok(Self::ToolAutoApproved {
                        call_id: bounded_identity(&payload.call_id),
                        note: payload
                            .reason
                            .as_deref()
                            .map(|reason| BoundedText::head(reason, 500, 8).text),
                    });
                }
                let raw_name = payload.target.as_deref().unwrap_or(&payload.name);
                Ok(Self::ToolApprovalResolved {
                    call_id: bounded_identity(&payload.call_id),
                    name: bounded_tool_name(raw_name),
                    approved: payload.approved,
                    reason: payload
                        .reason
                        .as_deref()
                        .map(|reason| BoundedText::head(reason, 512, 8).text),
                })
            }
            WireEvent::ToolExecutionEnded(payload) => wrap_message_origin(
                Self::ToolExecutionEnded {
                    call_id: bounded_identity(&payload.call_id),
                },
                payload.message_entry_id.as_deref().map(String::as_str),
                Vec::new(),
            ),
            WireEvent::ToolExecutionProgress(payload) => Ok(Self::ToolExecutionProgress {
                call_id: bounded_identity(&payload.call_id),
                progress: ToolProgressInput {
                    sequence: payload.sequence.get(),
                    stdout_tail: (*payload.stdout_tail).clone(),
                    stderr_tail: (*payload.stderr_tail).clone(),
                    stdout_bytes: payload.stdout_bytes,
                    stderr_bytes: payload.stderr_bytes,
                },
            }),
            WireEvent::ToolNestedCallStarted(payload) => {
                let target = payload.target.as_deref();
                Ok(Self::ScriptCallStarted {
                    parent_call_id: bounded_identity(&payload.parent_call_id),
                    call_id: payload.call_id.clone(),
                    tool: target.unwrap_or(&payload.name).to_owned(),
                    arguments: crate::script_card::call_arguments_map(
                        &payload.name,
                        target,
                        payload.arguments.as_ref(),
                    ),
                })
            }
            WireEvent::ToolNestedCallEnded(payload) => Ok(Self::ScriptCallEnded {
                parent_call_id: bounded_identity(&payload.parent_call_id),
                call: crate::script_card::ScriptCall::from_wire(&payload.call),
            }),
            WireEvent::ToolResultReady(payload) => wrap_message_origin(
                project_live_tool_result(payload)?,
                payload.message_entry_id.as_deref().map(String::as_str),
                Vec::new(),
            ),
            WireEvent::RpcSettingsReloaded(payload) => {
                let command_id = exact_str(
                    &payload.command_id,
                    "rpc.settings.reloaded",
                    "command_id",
                    256,
                )?;
                let map_names = |names: &[wisp_protocol::events::wire::ReloadSettingName]| {
                    names.iter().map(|name| (**name).clone()).collect()
                };
                let report = super::reload::ReloadReport {
                    applied: map_names(&payload.applied),
                    unchanged: map_names(&payload.unchanged),
                    overridden: map_names(&payload.overridden),
                    restart_required: map_names(&payload.restart_required),
                    state: super::reload::ReloadSelection {
                        provider: payload.state.provider.clone(),
                        model: payload.state.model.clone(),
                        effort: payload.state.effort.clone(),
                    },
                };
                Ok(Self::SettingsReloaded {
                    command_id,
                    report: Box::new(report),
                })
            }
            WireEvent::RpcSubagentsReported(payload) => {
                let command_id =
                    exact_str(&payload.command_id, "rpc.subagents", "command_id", 256)?;
                let subagents = discovery_report(&payload.subagents, || {
                    Some(subagents_state_from_wire(&payload.subagents))
                });
                Ok(Self::SubagentsReported {
                    command_id,
                    subagents,
                })
            }
            WireEvent::RpcCodemodeReported(payload) => {
                let command_id = exact_str(&payload.command_id, "rpc.codemode", "command_id", 256)?;
                let codemode = discovery_report(&payload.codemode, || {
                    Some(codemode_state_from_wire(&payload.codemode))
                });
                Ok(Self::CodemodeReported {
                    command_id,
                    codemode,
                })
            }
            WireEvent::RpcPermissionsReported(payload) => {
                let command_id =
                    exact_str(&payload.command_id, "rpc.permissions", "command_id", 256)?;
                let permissions = discovery_report(&payload.permissions, || {
                    Some(permission_state_from_wire(&payload.permissions))
                });
                Ok(Self::PermissionsReported {
                    command_id,
                    permissions,
                })
            }
            WireEvent::RpcGitDiffReported(payload) => {
                let command_id = exact_str(&payload.command_id, "rpc.git_diff", "command_id", 256)?;
                let snapshot =
                    discovery_report(payload, || Some(git_diff_snapshot_from_wire(payload)))
                        .map_err(|_| {
                            "Git diff exceeds the display limit or could not be decoded.".into()
                        });
                Ok(Self::GitDiffReported {
                    command_id,
                    snapshot,
                })
            }
            WireEvent::QueueUpdated(payload) => {
                let event_type = "queue.updated";
                validate_queue_str_slices(
                    event_type,
                    [payload.steering.as_slice(), payload.follow_up.as_slice()],
                )?;
                let steering_image_counts = queued_images_from_wire(
                    &payload.steering_images,
                    event_type,
                    "steering_images",
                    payload.steering.len(),
                )
                .map(|images| images.iter().map(ComposerImages::len).collect::<Vec<_>>());
                let follow_up_image_counts = queued_images_from_wire(
                    &payload.follow_up_images,
                    event_type,
                    "follow_up_images",
                    payload.follow_up.len(),
                )
                .map(|images| images.iter().map(ComposerImages::len).collect::<Vec<_>>());
                Ok(Self::QueueSnapshot(super::queue_management::Snapshot {
                    steering_image_counts: steering_image_counts?,
                    follow_up_image_counts: follow_up_image_counts?,
                    steering: payload.steering.clone(),
                    follow_up: payload.follow_up.clone(),
                    steering_mode: wire_steering_mode(payload.steering_mode),
                    follow_up_mode: wire_follow_up_mode(payload.follow_up_mode),
                    token: optional_exact_str(
                        payload.token.as_deref().map(String::as_str),
                        event_type,
                        "token",
                        128,
                    )?,
                    command_id: optional_exact_str(
                        payload.command_id.as_deref(),
                        event_type,
                        "command_id",
                        256,
                    )?,
                }))
            }
            WireEvent::QueueItemsRemoved(payload) => {
                let event_type = "queue.items.removed";
                validate_queue_str_slices(
                    event_type,
                    [payload.steering.as_slice(), payload.follow_up.as_slice()],
                )?;
                let operation = match payload.operation {
                    wisp_protocol::events::wire::QueueItemsRemovedOperation::Pop => {
                        QueueRemovalOperation::Pop
                    }
                    wisp_protocol::events::wire::QueueItemsRemovedOperation::Clear => {
                        QueueRemovalOperation::Clear
                    }
                };
                let kind = payload.kind.map(wire_removed_queue_kind);
                validate_queue_removal_counts(
                    event_type,
                    operation,
                    kind,
                    payload.steering.len(),
                    payload.follow_up.len(),
                )?;
                Ok(Self::QueueItemsRemoved {
                    command_id: exact_str(&payload.command_id, event_type, "command_id", 256)?,
                    operation,
                    kind,
                    steering_images: queued_images_from_wire(
                        &payload.steering_images,
                        event_type,
                        "steering_images",
                        payload.steering.len(),
                    )?,
                    follow_up_images: queued_images_from_wire(
                        &payload.follow_up_images,
                        event_type,
                        "follow_up_images",
                        payload.follow_up.len(),
                    )?,
                    steering: payload.steering.clone(),
                    follow_up: payload.follow_up.clone(),
                })
            }
            WireEvent::QueueMessageInjected(payload) => {
                let event_type = "queue.message.injected";
                validate_queue_payload(event_type, [payload.content.as_str()])?;
                let visible_content = payload
                    .skill_invocation
                    .as_ref()
                    .map_or_else(|| payload.content.clone(), |s| s.original_content.clone());
                validate_queue_payload(event_type, [visible_content.as_str()])?;
                wrap_message_origin(
                    Self::QueueMessageInjected {
                        kind: wire_injected_queue_kind(payload.kind),
                        content: visible_content,
                        images: composer_images::reported_image_lines_from_wire(&payload.images),
                        image_parts: composer_images::reported_transcript_images_from_wire(
                            &payload.images,
                        ),
                    },
                    payload.message_entry_id.as_deref().map(String::as_str),
                    Vec::new(),
                )
            }
            WireEvent::RpcSessionsReported(payload) => {
                let event_type = "rpc.sessions";
                if payload.sessions.len() > SESSION_CATALOG_LIMIT {
                    return Err(EventProjectionError::TooManySessions);
                }
                let sessions = payload
                    .sessions
                    .iter()
                    .map(|session| {
                        Ok(SessionSummary {
                            session_id: exact_str(
                                &session.session_id,
                                event_type,
                                "session_id",
                                SESSION_ID_MAX_BYTES,
                            )?,
                            session_path: exact_str(
                                &session.session_path,
                                event_type,
                                "session_path",
                                SESSION_PATH_MAX_BYTES,
                            )?,
                            name: session.name.as_deref().map(|name| {
                                BoundedText::head(name, SESSION_LABEL_MAX_BYTES, 8).text
                            }),
                            updated_at: BoundedText::head(
                                &session
                                    .updated_at
                                    .to_rfc3339_opts(chrono::SecondsFormat::AutoSi, true),
                                SESSION_UPDATED_AT_MAX_BYTES,
                                8,
                            )
                            .text,
                            entry_count: bounded_entry_count_u64(session.entry_count),
                        })
                    })
                    .collect::<Result<Vec<_>, EventProjectionError>>()?;
                Ok(Self::SessionsReported {
                    command_id: exact_str(&payload.command_id, event_type, "command_id", 256)?,
                    sessions,
                    navigation: CatalogNavigation {
                        next_cursor: optional_exact_str(
                            payload.next_cursor.as_deref(),
                            event_type,
                            "next_cursor",
                            4096,
                        )?,
                        previous_cursor: optional_exact_str(
                            payload.previous_cursor.as_deref(),
                            event_type,
                            "previous_cursor",
                            4096,
                        )?,
                    },
                    selected_session: optional_session_identity_from_strs(
                        payload.selected_session_id.as_deref().map(String::as_str),
                        payload.selected_session_path.as_deref(),
                        payload.selected_session_name.as_deref(),
                        event_type,
                        "selected_session_id",
                        "selected_session_path",
                    )?,
                })
            }
            WireEvent::RpcSessionSelected(payload) => {
                let event_type = "rpc.session.selected";
                Ok(Self::SessionSelected {
                    command_id: exact_str(&payload.command_id, event_type, "command_id", 256)?,
                    session: required_session_identity_from_strs(
                        &payload.session_id,
                        &payload.session_path,
                        payload.session_name.as_deref(),
                        event_type,
                        "session_id",
                        "session_path",
                    )?,
                })
            }
            WireEvent::RpcSessionNameChanged(payload) => {
                let event_type = "rpc.session.name_changed";
                Ok(Self::SessionNameChanged {
                    command_id: exact_str(&payload.command_id, event_type, "command_id", 256)?,
                    changed: SessionNameChange {
                        session: required_session_identity_from_strs(
                            &payload.session_id,
                            &payload.session_path,
                            payload.name.as_deref(),
                            event_type,
                            "session_id",
                            "session_path",
                        )?,
                        previous_name: payload
                            .previous_name
                            .as_deref()
                            .map(|name| BoundedText::head(name, SESSION_LABEL_MAX_BYTES, 8).text),
                        entry_count: bounded_entry_count_u64(payload.entry_count),
                    },
                })
            }
            WireEvent::RpcSessionCloned(payload) => {
                let event_type = "rpc.session.cloned";
                Ok(Self::SessionCloned {
                    command_id: exact_str(&payload.command_id, event_type, "command_id", 256)?,
                    derived: session_derivation_from_wire(
                        event_type,
                        &payload.source_session_id,
                        &payload.source_session_path,
                        payload.source_session_name.as_deref(),
                        payload.source_active_leaf_id.as_deref().map(String::as_str),
                        &payload.session_id,
                        &payload.session_path,
                        payload.session_name.as_deref(),
                        payload.active_leaf_id.as_deref().map(String::as_str),
                        payload.entry_count,
                        None,
                    )?,
                })
            }
            WireEvent::RpcSessionForked(payload) => {
                let event_type = "rpc.session.forked";
                Ok(Self::SessionForked {
                    command_id: exact_str(&payload.command_id, event_type, "command_id", 256)?,
                    derived: session_derivation_from_wire(
                        event_type,
                        &payload.source_session_id,
                        &payload.source_session_path,
                        payload.source_session_name.as_deref(),
                        payload.source_active_leaf_id.as_deref().map(String::as_str),
                        &payload.session_id,
                        &payload.session_path,
                        payload.session_name.as_deref(),
                        payload.active_leaf_id.as_deref().map(String::as_str),
                        payload.entry_count,
                        Some((
                            &payload.selected_entry_id,
                            &payload.selected_prompt,
                            &payload.selected_images,
                        )),
                    )?,
                })
            }
            WireEvent::RpcSessionTreeReported(payload) => Ok(Self::SessionTreeReported {
                command_id: exact_str(&payload.command_id, "rpc.session.tree", "command_id", 256)?,
                page: session_tree_page_from_wire(payload)?,
            }),
            WireEvent::RpcSessionTreeNavigated(payload) => Ok(Self::SessionTreeNavigated {
                command_id: exact_str(
                    &payload.command_id,
                    "rpc.session.tree.navigated",
                    "command_id",
                    256,
                )?,
                navigation: session_tree_navigation_from_wire(payload)?,
            }),
            WireEvent::RpcSessionTreeUnreverted(payload) => Ok(Self::SessionTreeUnreverted {
                command_id: exact_str(
                    &payload.command_id,
                    "rpc.session.tree.unreverted",
                    "command_id",
                    256,
                )?,
                unreverted: session_tree_unrevert_from_wire(payload)?,
            }),
            WireEvent::RpcStartupHistoryPreview(payload) => Ok(Self::StartupHistoryPreview {
                lines: payload.lines.iter().map(|line| (**line).clone()).collect(),
            }),
            WireEvent::RpcMessagesReported(payload) => {
                let event_type = "rpc.messages";
                let command_id = exact_str(&payload.command_id, event_type, "command_id", 256)?;
                let session = optional_session_identity_from_strs(
                    payload.session_id.as_deref(),
                    payload.session_path.as_deref(),
                    None,
                    event_type,
                    "session_id",
                    "session_path",
                )?;
                let source = match serde_json::to_value(&payload.messages)? {
                    Value::Array(items) => items,
                    _ => Vec::new(),
                };
                let started = std::time::Instant::now();
                let projected = project_rpc_message_page_with_origins(&source, payload.truncated);
                crate::hydration_profile::record(
                    "rust.page_projection",
                    started.elapsed(),
                    source.len(),
                );
                Ok(match projected {
                    Ok(page) => Self::MessagesReported {
                        command_id,
                        messages: SessionMessages {
                            source_messages: {
                                let started = std::time::Instant::now();
                                let retained = source.as_slice().into();
                                crate::hydration_profile::record(
                                    "rust.page_clone",
                                    started.elapsed(),
                                    source.len(),
                                );
                                retained
                            },
                            session,
                            active_leaf_id: optional_exact_str(
                                payload.active_leaf_id.as_deref(),
                                event_type,
                                "active_leaf_id",
                                SESSION_ID_MAX_BYTES,
                            )?,
                            truncated: payload.truncated,
                            next_before_entry_id: optional_exact_str(
                                payload.next_before_entry_id.as_deref(),
                                event_type,
                                "next_before_entry_id",
                                SESSION_ID_MAX_BYTES,
                            )?,
                            next_after_entry_id: optional_exact_str(
                                payload.next_after_entry_id.as_deref(),
                                event_type,
                                "next_after_entry_id",
                                SESSION_ID_MAX_BYTES,
                            )?,
                            exact_tool_result: source
                                .first()
                                .and_then(|message| message.get("entry_id"))
                                .and_then(Value::as_str)
                                .and_then(|entry_id| {
                                    crate::history::project_rpc_exact_tool_result(&source, entry_id)
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
                })
            }
            WireEvent::TurnStarted(_)
            | WireEvent::ProviderRetrying(_)
            | WireEvent::ContextPressure(_)
            | WireEvent::ContextOverflow(_)
            | WireEvent::TrustResolved(_)
            | WireEvent::TurnCompleted(_)
            | WireEvent::SessionSaved(_)
            | WireEvent::ModelProviderAutoSwitched(_) => Ok(Self::Other {
                event_type: event.event_type(),
            }),
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
            | "rpc.processes"
            | "rpc.process_output"
            | "rpc.commands"
            | "rpc.state"
            | "session.stats"
            | "context.estimated"
            | "compaction.started"
            | "compaction.completed"
            | "rpc.browser_addresses"
            | "browser.viewport.updated" => {
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
                session_images_dir: value
                    .get("session_images_dir")
                    .and_then(Value::as_str)
                    .filter(|path| !path.is_empty())
                    .map(std::path::PathBuf::from),
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
                output_tokens: value
                    .get("usage")
                    .and_then(|usage| usage.get("output_tokens"))
                    .and_then(Value::as_u64),
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
                let command_prefix = optional_string_field(value, &event_type, "command_prefix")?;
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
                    command_prefix,
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
            "tool.approval.reviewed" => Self::ToolApprovalReviewed {
                call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
                note: format!(
                    "Command review ({}): {}",
                    string_field_ref(value, &event_type, "verdict")?,
                    bounded_display_string_field(value, &event_type, "reason", 500)?,
                ),
            },
            "tool.approval.resolved"
                if value.get("source").and_then(Value::as_str) == Some("auto")
                    && bool_field(value, &event_type, "approved")? =>
            {
                Self::ToolAutoApproved {
                    call_id: bounded_identity(&string_field(value, &event_type, "call_id")?),
                    note: optional_bounded_display_string_field(value, &event_type, "reason", 500)?,
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
                    image_parts: composer_images::reported_transcript_images(value.get("images")),
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
                validate_queue_removal_counts(
                    &event_type,
                    operation,
                    kind,
                    steering.len(),
                    follow_up.len(),
                )?;
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
                    image_parts: composer_images::reported_transcript_images(value.get("images")),
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
            "rpc.startup.history_preview" => Self::StartupHistoryPreview {
                lines: serde_json::from_value(value["lines"].clone()).map_err(|_| {
                    EventProjectionError::InvalidField {
                        event_type: event_type.clone(),
                        field: "lines",
                    }
                })?,
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
            "rpc.shell.progress" => Self::ShellProgress {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                stdout_tail: string_field(value, &event_type, "stdout_tail")?,
                stderr_tail: string_field(value, &event_type, "stderr_tail")?,
                stdout_bytes: u64_field(value, &event_type, "stdout_bytes")?,
                stderr_bytes: u64_field(value, &event_type, "stderr_bytes")?,
            },
            "rpc.shell.completed" => Self::ShellCompleted {
                command_id: exact_string_field(value, &event_type, "command_id", 256)?,
                command: bounded_display_string_field(value, &event_type, "command", 32_768)?,
                exit_code: optional_i64_field(value, &event_type, "exit_code")?,
                output: string_field(value, &event_type, "output")?,
                truncated: bool_field_or(value, &event_type, "truncated", false)?,
                timed_out: bool_field_or(value, &event_type, "timed_out", false)?,
                cancelled: bool_field_or(value, &event_type, "cancelled", false)?,
                duration_ms: u64_field_or(value, &event_type, "duration_ms", 0)?,
                error: optional_bounded_display_string_field(value, &event_type, "error", 2_048)?,
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
    u64_field(value, event_type, "entry_count").map(bounded_entry_count_u64)
}

#[allow(clippy::too_many_arguments)]
fn finish_session_derivation(
    event_type: &str,
    source: SessionIdentity,
    source_active_leaf_id: Option<String>,
    session: SessionIdentity,
    active_leaf_id: Option<String>,
    entry_count: u32,
    selected_entry_id: Option<String>,
    selected_prompt: Option<String>,
    selected_images: ComposerImages,
) -> Result<SessionDerivation, EventProjectionError> {
    if source.session_id == session.session_id {
        return Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "session_id",
        });
    }
    Ok(SessionDerivation {
        source,
        source_active_leaf_id,
        session,
        active_leaf_id,
        entry_count,
        selected_entry_id,
        selected_prompt,
        selected_images,
    })
}

#[allow(clippy::too_many_arguments)]
fn build_session_tree_node(
    event_type: &str,
    entry_ids: &mut std::collections::BTreeSet<String>,
    entry_id: String,
    parent_id: Option<String>,
    created_at: String,
    kind: SessionTreeNodeKind,
    role: Option<String>,
    raw_preview: &str,
    preview_truncated: bool,
) -> Result<SessionTreeNode, EventProjectionError> {
    if !entry_ids.insert(entry_id.clone()) {
        return Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "nodes",
        });
    }
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
    let bounded_preview = BoundedText::head(raw_preview, SESSION_LABEL_MAX_BYTES, 8);
    Ok(SessionTreeNode {
        entry_id,
        parent_id,
        created_at,
        kind,
        role,
        preview: bounded_preview.text,
        preview_truncated: preview_truncated
            || bounded_preview.dropped_bytes > 0
            || bounded_preview.dropped_lines > 0,
    })
}

fn finish_session_tree_page(
    event_type: &str,
    session: Option<SessionIdentity>,
    active_leaf_id: Option<String>,
    total_node_count: u32,
    nodes: Vec<SessionTreeNode>,
    truncated: bool,
    next_after_entry_id: Option<String>,
) -> Result<SessionTreePage, EventProjectionError> {
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

#[allow(clippy::too_many_arguments)]
fn finish_session_tree_navigation(
    event_type: &str,
    session: SessionIdentity,
    selected_entry_id: String,
    previous_active_leaf_id: Option<String>,
    active_leaf_id: Option<String>,
    editor_text: Option<String>,
    editor_images: ComposerImages,
    changed: bool,
    entry_count: u32,
) -> Result<SessionTreeNavigation, EventProjectionError> {
    if changed == (previous_active_leaf_id == active_leaf_id) {
        return Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "changed",
        });
    }
    Ok(SessionTreeNavigation {
        session,
        selected_entry_id,
        previous_active_leaf_id,
        active_leaf_id,
        editor_text,
        editor_images,
        changed,
        entry_count,
    })
}

fn finish_session_tree_unrevert(
    event_type: &str,
    session: SessionIdentity,
    source_transition_id: String,
    previous_active_leaf_id: Option<String>,
    active_leaf_id: Option<String>,
    entry_count: u32,
) -> Result<SessionTreeUnrevert, EventProjectionError> {
    if previous_active_leaf_id == active_leaf_id {
        return Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "active_leaf_id",
        });
    }
    Ok(SessionTreeUnrevert {
        session,
        source_transition_id,
        previous_active_leaf_id,
        active_leaf_id,
        entry_count,
    })
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
    finish_session_derivation(
        event_type,
        source,
        optional_exact_string_field(
            value,
            event_type,
            "source_active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        session,
        optional_exact_string_field(value, event_type, "active_leaf_id", SESSION_ID_MAX_BYTES)?,
        bounded_entry_count(value, event_type)?,
        selected_entry_id,
        selected_prompt,
        if forked {
            stored_images(value.get("selected_images"), event_type, "selected_images")?
        } else {
            ComposerImages::default()
        },
    )
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
    let total_node_count =
        bounded_entry_count_u64(u64_field(value, event_type, "total_node_count")?);
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
            let raw_preview = string_field_ref(node, event_type, "preview")?;
            optional_exact_string_field(node, event_type, "operation_id", SESSION_ID_MAX_BYTES)?;
            build_session_tree_node(
                event_type,
                &mut entry_ids,
                entry_id,
                optional_exact_string_field(node, event_type, "parent_id", SESSION_ID_MAX_BYTES)?,
                bounded_display_string_field(
                    node,
                    event_type,
                    "created_at",
                    SESSION_UPDATED_AT_MAX_BYTES,
                )?,
                kind,
                role,
                raw_preview,
                bool_field(node, event_type, "preview_truncated")?,
            )
        })
        .collect::<Result<Vec<_>, _>>()?;
    finish_session_tree_page(
        event_type,
        session,
        active_leaf_id,
        total_node_count,
        nodes,
        truncated,
        next_after_entry_id,
    )
}

fn session_tree_navigation(
    value: &Value,
    event_type: &str,
) -> Result<SessionTreeNavigation, EventProjectionError> {
    finish_session_tree_navigation(
        event_type,
        required_session_identity(value, event_type, "session_id", "session_path", None)?,
        exact_string_field(value, event_type, "selected_entry_id", SESSION_ID_MAX_BYTES)?,
        optional_exact_string_field(
            value,
            event_type,
            "previous_active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        optional_exact_string_field(value, event_type, "active_leaf_id", SESSION_ID_MAX_BYTES)?,
        optional_exact_string_field(
            value,
            event_type,
            "editor_text",
            MAX_APPLICATION_FRAME_BYTES,
        )?,
        stored_images(value.get("editor_images"), event_type, "editor_images")?,
        bool_field(value, event_type, "changed")?,
        bounded_entry_count(value, event_type)?,
    )
}

fn session_tree_unrevert(
    value: &Value,
    event_type: &str,
) -> Result<SessionTreeUnrevert, EventProjectionError> {
    finish_session_tree_unrevert(
        event_type,
        required_session_identity(value, event_type, "session_id", "session_path", None)?,
        exact_string_field(
            value,
            event_type,
            "source_transition_id",
            SESSION_ID_MAX_BYTES,
        )?,
        optional_exact_string_field(
            value,
            event_type,
            "previous_active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        optional_exact_string_field(value, event_type, "active_leaf_id", SESSION_ID_MAX_BYTES)?,
        bounded_entry_count(value, event_type)?,
    )
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

fn exact_str(
    source: &str,
    event_type: &str,
    field: &'static str,
    limit: usize,
) -> Result<String, EventProjectionError> {
    if source.len() > limit {
        return Err(EventProjectionError::OversizedField {
            event_type: event_type.to_owned(),
            field,
            limit,
        });
    }
    Ok(source.to_owned())
}

fn exact_string_field(
    value: &Value,
    event_type: &str,
    field: &'static str,
    limit: usize,
) -> Result<String, EventProjectionError> {
    exact_str(
        string_field_ref(value, event_type, field)?,
        event_type,
        field,
        limit,
    )
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

fn check_queue_limits(
    event_type: &str,
    count: usize,
    bytes: usize,
) -> Result<(), EventProjectionError> {
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
    check_queue_limits(event_type, count, bytes)
}

fn validate_queue_str_slices<'a>(
    event_type: &str,
    arrays: impl IntoIterator<Item = &'a [String]>,
) -> Result<(), EventProjectionError> {
    let mut count = 0usize;
    let mut bytes = 0usize;
    for contents in arrays {
        for content in contents {
            count = count.saturating_add(1);
            bytes = bytes.saturating_add(content.len());
        }
    }
    check_queue_limits(event_type, count, bytes)
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
    check_queue_limits(event_type, count, bytes)
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

fn validate_queue_removal_counts(
    event_type: &str,
    operation: QueueRemovalOperation,
    kind: Option<QueueKind>,
    steering_len: usize,
    follow_up_len: usize,
) -> Result<(), EventProjectionError> {
    let valid = !matches!(operation, QueueRemovalOperation::Pop) || kind.is_some();
    let matching_kind = match kind {
        Some(QueueKind::Steering) => follow_up_len == 0,
        Some(QueueKind::FollowUp) => steering_len == 0,
        None => true,
    };
    let pop_size = !matches!(operation, QueueRemovalOperation::Pop)
        || steering_len.saturating_add(follow_up_len) <= 1;
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

fn wrap_message_origin(
    projected: BackendEvent,
    message_entry_id: Option<&str>,
    tool_call_ids: Vec<String>,
) -> Result<BackendEvent, EventProjectionError> {
    let Some(entry_id) = message_entry_id else {
        return Ok(projected);
    };
    let entry_id = exact_str(entry_id, "live message", "message_entry_id", 4096)?;
    Ok(BackendEvent::MessageOrigin {
        entry_id,
        tool_call_ids,
        event: Box::new(projected),
    })
}

fn wire_exit_code(
    exit_code: Option<i64>,
    event_type: &str,
) -> Result<Option<i32>, EventProjectionError> {
    exit_code
        .map(i32::try_from)
        .transpose()
        .map_err(|_| EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "exit_code",
        })
}

fn wire_background_processes(
    entries: &[wisp_protocol::events::wire::BackgroundProcess],
    event_type: &str,
) -> Result<Vec<super::background_processes::BackgroundProcess>, EventProjectionError> {
    entries
        .iter()
        .map(|entry| {
            let started_at_ms = i64::try_from(entry.started_at_ms).map_err(|_| {
                EventProjectionError::InvalidField {
                    event_type: event_type.to_owned(),
                    field: "started_at_ms",
                }
            })?;
            Ok(super::background_processes::BackgroundProcess {
                process_id: exact_str(&entry.process_id, event_type, "process_id", 128)?,
                command: (*entry.command).clone(),
                started_at_ms: Some(started_at_ms),
            })
        })
        .collect()
}

fn wire_process_report(
    process: &wisp_protocol::events::wire::RpcProcessSnapshot,
    event_type: &str,
) -> Result<super::background_processes::ProcessReport, EventProjectionError> {
    use crate::tool_cards::ProcessDisplayState;
    use wisp_protocol::events::wire::RpcProcessSnapshotState as WireState;

    let exit_code = wire_exit_code(process.exit_code, event_type)?;
    let started_at_ms =
        i64::try_from(process.started_at_ms).map_err(|_| EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: "started_at_ms",
        })?;
    let state = match process.state {
        WireState::Running => ProcessDisplayState::Running,
        WireState::Completed if exit_code == Some(0) => ProcessDisplayState::Completed,
        WireState::Completed | WireState::Failed => ProcessDisplayState::Failed,
        WireState::TimedOut => ProcessDisplayState::TimedOut,
        WireState::Cancelled => ProcessDisplayState::Cancelled,
    };
    Ok(super::background_processes::ProcessReport {
        process_id: exact_str(&process.process_id, event_type, "process_id", 128)?,
        command: (*process.command).clone(),
        state,
        exit_code,
        started_at_ms: Some(started_at_ms),
        stopped_by_user: process.stopped_by_user,
    })
}

fn project_live_approval_requested(
    payload: &wisp_protocol::events::wire::ToolApprovalRequested,
) -> BackendEvent {
    let (raw_name, arguments) = match payload.target.as_deref() {
        Some(target) => (
            target,
            proxied_arguments_map(&payload.name, &payload.arguments, target)
                .unwrap_or(&payload.arguments),
        ),
        None => (payload.name.as_str(), &payload.arguments),
    };
    let detail_source = project_tool_detail_source(raw_name, arguments);
    let arguments = if payload.grants_allowed {
        bounded_tool_arguments_map(raw_name, arguments)
    } else {
        complete_confirmation_arguments_map(arguments)
    };
    BackendEvent::ToolApprovalRequested(PendingApproval {
        call_id: payload.call_id.clone(),
        detail_source,
        arguments,
        name: bounded_tool_name(raw_name),
        safety: BoundedText::head(&payload.safety.to_string(), 128, 8).text,
        grants_allowed: payload.grants_allowed,
        session_only_yolo: payload.session_only_yolo,
        command_prefix: payload.command_prefix.clone(),
    })
}

fn optional_project_tail(value: Option<&str>) -> (Option<String>, u64) {
    match value {
        Some(text) => {
            let projected = project_text(text);
            (Some(projected.tail), projected.source_bytes)
        }
        None => (None, 0),
    }
}

fn project_live_tool_result(
    payload: &wisp_protocol::events::wire::ToolResultReady,
) -> Result<BackendEvent, EventProjectionError> {
    let raw_name = payload.target.as_deref().unwrap_or(&payload.name);
    let process_state = payload.process_state.as_ref().map(ToString::to_string);
    let cancelled = process_state.as_deref() == Some("cancelled");
    let failed = !cancelled
        && (payload.is_error
            || payload.exit_code.is_some_and(|code| code != 0)
            || matches!(process_state.as_deref(), Some("failed" | "timed_out")));
    let projected_output = project_text(&payload.output);
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
    let (stdout, stdout_source_bytes) = optional_project_tail(payload.stdout.as_deref());
    let (stderr, stderr_source_bytes) = optional_project_tail(payload.stderr.as_deref());
    let before_text = capture_write_before_str(raw_name, payload.before_text.as_deref())
        .ok()
        .flatten();
    let edit_locations = edit_locations_from_wire(&payload.edit_locations).map_err(|()| {
        EventProjectionError::InvalidField {
            event_type: "tool.result".into(),
            field: "edit_locations",
        }
    })?;
    Ok(BackendEvent::ToolResult(Box::new(ToolResultInput {
        nested: crate::script_card::ScriptResult::from_wire(
            &payload.nested_calls,
            payload.nested_calls_omitted,
        ),
        images: composer_images::reported_image_lines_from_wire(&payload.images),
        image_parts: composer_images::reported_transcript_images_from_wire(&payload.images),
        call_id: bounded_identity(&payload.call_id),
        name: bounded_tool_name(raw_name),
        output,
        output_tail,
        output_source_bytes,
        output_source_lines,
        output_projection_cut_mid_line,
        is_error: payload.is_error,
        failure_code: payload.failure_code.as_ref().map(ToString::to_string),
        retryable: payload.retryable,
        recovery_hint: payload
            .recovery_hint
            .as_deref()
            .map(|hint| BoundedText::head(hint, 512, 8).text),
        exit_code: payload.exit_code,
        output_has_exit_status: payload.output_has_exit_status,
        before_text,
        created: raw_name == "write" && payload.created,
        summary: payload
            .summary
            .as_deref()
            .map(|summary| BoundedText::head(summary, 512, 8).text),
        truncated: payload.truncated,
        process_id: payload.process_id.as_deref().map(bounded_identity),
        process_state,
        process_error: payload
            .process_error
            .as_deref()
            .map(|error| BoundedText::head(error, 512, 8).text),
        stdout,
        stdout_source_bytes,
        stderr,
        stderr_source_bytes,
        stdout_truncated: payload.stdout_truncated,
        stderr_truncated: payload.stderr_truncated,
        stdout_dropped_bytes: payload.stdout_dropped_bytes,
        stderr_dropped_bytes: payload.stderr_dropped_bytes,
        duration_ms: payload.duration_ms,
        started_at_ms: payload.started_at.map(|dt| dt.timestamp_millis()),
        process_started_at_ms: payload.process_started_at.map(|dt| dt.timestamp_millis()),
        process_ended_at_ms: payload.process_ended_at.map(|dt| dt.timestamp_millis()),
        edit_locations,
    })))
}

fn complete_confirmation_arguments_map(arguments: &serde_json::Map<String, Value>) -> Value {
    match serde_json::to_vec(arguments) {
        Ok(encoded) if encoded.len() <= CONFIRMATION_ARGUMENTS_MAX_BYTES => {
            Value::Object(arguments.clone())
        }
        _ => Value::Null,
    }
}

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

fn optional_exact_str(
    source: Option<&str>,
    event_type: &str,
    field: &'static str,
    limit: usize,
) -> Result<Option<String>, EventProjectionError> {
    source
        .map(|value| exact_str(value, event_type, field, limit))
        .transpose()
}

fn bounded_entry_count_u64(count: u64) -> u32 {
    count.min(u64::from(SESSION_ENTRY_COUNT_MAX)) as u32
}

fn wire_permission_mode(
    mode: wisp_protocol::events::wire::PermissionMode,
) -> wisp_protocol::commands::PermissionMode {
    use wisp_protocol::commands::PermissionMode as Target;
    use wisp_protocol::events::wire::PermissionMode as Wire;
    match mode {
        Wire::Ask => Target::Ask,
        Wire::Auto => Target::Auto,
        Wire::AutoCommands => Target::AutoCommands,
        Wire::Yolo => Target::Yolo,
    }
}

fn permission_state_from_wire(
    src: &wisp_protocol::events::wire::PermissionState,
) -> wisp_protocol::events::PermissionState {
    wisp_protocol::events::PermissionState {
        mode: wire_permission_mode(src.mode),
        saved_mode: src.saved_mode.map(wire_permission_mode),
        project_path: src.project_path.clone(),
        session_only_yolo: src.session_only_yolo,
        command_allow_rules: src.command_allow_rules.clone(),
    }
}

fn wire_codemode_setting(
    setting: wisp_protocol::events::wire::CodemodeSetting,
) -> wisp_protocol::commands::CodemodeSetting {
    use wisp_protocol::commands::CodemodeSetting as Target;
    use wisp_protocol::events::wire::CodemodeSetting as Wire;
    match setting {
        Wire::Automatic => Target::Automatic,
        Wire::Always => Target::Always,
        Wire::Never => Target::Never,
    }
}

fn wire_codemode_presentation(
    presentation: wisp_protocol::events::wire::CodemodePresentation,
) -> wisp_protocol::commands::CodemodePresentation {
    use wisp_protocol::commands::CodemodePresentation as Target;
    use wisp_protocol::events::wire::CodemodePresentation as Wire;
    match presentation {
        Wire::DirectAndCodemode => Target::DirectAndCodemode,
        Wire::CodemodeOnly => Target::CodemodeOnly,
    }
}

fn codemode_state_from_wire(
    src: &wisp_protocol::events::wire::CodemodeState,
) -> wisp_protocol::events::CodemodeState {
    wisp_protocol::events::CodemodeState {
        setting: wire_codemode_setting(src.setting),
        presentation: wire_codemode_presentation(src.presentation),
        effective_presentation: wire_codemode_presentation(src.effective_presentation),
        available: src.available,
        offered: src.offered,
        env_setting: src.env_setting.map(wire_codemode_setting),
        mcp_tools: src.mcp_tools,
        scriptable_mcp_tools: src.scriptable_mcp_tools,
    }
}

fn subagents_state_from_wire(
    src: &wisp_protocol::events::wire::SubagentsState,
) -> wisp_protocol::events::SubagentsState {
    wisp_protocol::events::SubagentsState {
        enabled: src.enabled,
        offered: src.offered,
        env_enabled: src.env_enabled,
        model: src.model.clone(),
        model_in_use: src.model_in_use.clone(),
        env_model: src.env_model.clone(),
        tools: src.tools.clone(),
        refused_tools: src.refused_tools.clone(),
        kinds: src
            .kinds
            .iter()
            .map(|kind| wisp_protocol::events::SubagentKind {
                name: kind.name.to_string(),
                model: kind.model.clone(),
                effort: kind.effort.clone(),
                instructions: kind.instructions.clone(),
            })
            .collect(),
        max_tool_rounds: src.max_tool_rounds,
        max_tasks: src.max_tasks,
        max_parallel: src.max_parallel,
        report_max_bytes: src.report_max_bytes,
    }
}

fn git_diff_snapshot_from_wire(
    src: &wisp_protocol::events::wire::RpcGitDiffReported,
) -> super::git_diff::GitDiffSnapshot {
    super::git_diff::GitDiffSnapshot {
        generation: src.generation.get(),
        head: (*src.head).clone(),
        files: src
            .files
            .iter()
            .map(|file| super::git_diff::GitDiffFile {
                path: (*file.path).clone(),
                status: file.status.to_string(),
                before: (*file.before).clone(),
                after: (*file.after).clone(),
                notice: (*file.notice).clone(),
                edits: file
                    .edits
                    .iter()
                    .map(|edit| super::git_diff::GitDiffEdit {
                        call_id: (*edit.call_id).clone(),
                        entry_id: (*edit.entry_id).clone(),
                        tool: edit.tool.to_string(),
                    })
                    .collect(),
            })
            .collect(),
        truncated: src.truncated,
    }
}

fn wire_steering_mode(
    mode: wisp_protocol::events::wire::QueueUpdatedSteeringMode,
) -> wisp_protocol::commands::QueueMode {
    use wisp_protocol::commands::QueueMode as Target;
    use wisp_protocol::events::wire::QueueUpdatedSteeringMode as Wire;
    match mode {
        Wire::All => Target::All,
        Wire::OneAtATime => Target::OneAtATime,
    }
}

fn wire_follow_up_mode(
    mode: wisp_protocol::events::wire::QueueUpdatedFollowUpMode,
) -> wisp_protocol::commands::QueueMode {
    use wisp_protocol::commands::QueueMode as Target;
    use wisp_protocol::events::wire::QueueUpdatedFollowUpMode as Wire;
    match mode {
        Wire::All => Target::All,
        Wire::OneAtATime => Target::OneAtATime,
    }
}

fn wire_removed_queue_kind(kind: wisp_protocol::events::wire::QueueItemsRemovedKind) -> QueueKind {
    use wisp_protocol::events::wire::QueueItemsRemovedKind as Wire;
    match kind {
        Wire::Steering => QueueKind::Steering,
        Wire::FollowUp => QueueKind::FollowUp,
    }
}

fn wire_injected_queue_kind(
    kind: wisp_protocol::events::wire::QueueMessageInjectedKind,
) -> QueueKind {
    use wisp_protocol::events::wire::QueueMessageInjectedKind as Wire;
    match kind {
        Wire::Steering => QueueKind::Steering,
        Wire::FollowUp => QueueKind::FollowUp,
    }
}

fn stored_images_from_wire(
    parts: &[wisp_protocol::events::wire::ImagePart],
    event_type: &str,
    field: &'static str,
) -> Result<ComposerImages, EventProjectionError> {
    parts
        .iter()
        .map(|part| {
            AttachedImage::stored_from_wire(part).ok_or_else(|| {
                EventProjectionError::InvalidField {
                    event_type: event_type.to_owned(),
                    field,
                }
            })
        })
        .collect()
}

fn queued_images_from_wire(
    entries: &[Vec<wisp_protocol::events::wire::ImagePart>],
    event_type: &str,
    field: &'static str,
    message_count: usize,
) -> Result<Vec<ComposerImages>, EventProjectionError> {
    if !entries.is_empty() && entries.len() != message_count {
        return Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field,
        });
    }
    entries
        .iter()
        .map(|parts| stored_images_from_wire(parts, event_type, field))
        .collect()
}

fn required_session_identity_from_strs(
    session_id: &str,
    session_path: &str,
    session_name: Option<&str>,
    event_type: &str,
    id_field: &'static str,
    path_field: &'static str,
) -> Result<SessionIdentity, EventProjectionError> {
    Ok(SessionIdentity {
        session_id: exact_str(session_id, event_type, id_field, SESSION_ID_MAX_BYTES)?,
        session_path: exact_str(session_path, event_type, path_field, SESSION_PATH_MAX_BYTES)?,
        session_name: session_name
            .map(|name| BoundedText::head(name, SESSION_LABEL_MAX_BYTES, 8).text),
    })
}

fn optional_session_identity_from_strs(
    session_id: Option<&str>,
    session_path: Option<&str>,
    session_name: Option<&str>,
    event_type: &str,
    id_field: &'static str,
    path_field: &'static str,
) -> Result<Option<SessionIdentity>, EventProjectionError> {
    match (session_id, session_path) {
        (None, None) => Ok(None),
        (Some(id), Some(path)) => required_session_identity_from_strs(
            id,
            path,
            session_name,
            event_type,
            id_field,
            path_field,
        )
        .map(Some),
        _ => Err(EventProjectionError::InvalidField {
            event_type: event_type.to_owned(),
            field: id_field,
        }),
    }
}

#[allow(clippy::too_many_arguments)]
fn session_derivation_from_wire(
    event_type: &str,
    source_session_id: &str,
    source_session_path: &str,
    source_session_name: Option<&str>,
    source_active_leaf_id: Option<&str>,
    session_id: &str,
    session_path: &str,
    session_name: Option<&str>,
    active_leaf_id: Option<&str>,
    entry_count: u64,
    fork: Option<(&str, &str, &[wisp_protocol::events::wire::ImagePart])>,
) -> Result<SessionDerivation, EventProjectionError> {
    let source = required_session_identity_from_strs(
        source_session_id,
        source_session_path,
        source_session_name,
        event_type,
        "source_session_id",
        "source_session_path",
    )?;
    let session = required_session_identity_from_strs(
        session_id,
        session_path,
        session_name,
        event_type,
        "session_id",
        "session_path",
    )?;
    let (selected_entry_id, selected_prompt, selected_images) = match fork {
        Some((entry_id, prompt, images)) => (
            Some(exact_str(
                entry_id,
                event_type,
                "selected_entry_id",
                SESSION_ID_MAX_BYTES,
            )?),
            Some(exact_str(
                prompt,
                event_type,
                "selected_prompt",
                MAX_APPLICATION_FRAME_BYTES,
            )?),
            stored_images_from_wire(images, event_type, "selected_images")?,
        ),
        None => (None, None, ComposerImages::default()),
    };
    finish_session_derivation(
        event_type,
        source,
        optional_exact_str(
            source_active_leaf_id,
            event_type,
            "source_active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        session,
        optional_exact_str(
            active_leaf_id,
            event_type,
            "active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        bounded_entry_count_u64(entry_count),
        selected_entry_id,
        selected_prompt,
        selected_images,
    )
}

fn session_tree_page_from_wire(
    payload: &wisp_protocol::events::wire::RpcSessionTreeReported,
) -> Result<SessionTreePage, EventProjectionError> {
    use wisp_protocol::events::wire::RpcSessionTreeNodeKind as WireNodeKind;
    let event_type = "rpc.session.tree";
    if payload.nodes.len() > SESSION_TREE_PAGE_LIMIT {
        return Err(EventProjectionError::TooManyTreeNodes);
    }
    let session = optional_session_identity_from_strs(
        payload.session_id.as_deref().map(String::as_str),
        payload.session_path.as_deref(),
        None,
        event_type,
        "session_id",
        "session_path",
    )?;
    let active_leaf_id = optional_exact_str(
        payload.active_leaf_id.as_deref().map(String::as_str),
        event_type,
        "active_leaf_id",
        SESSION_ID_MAX_BYTES,
    )?;
    let total_node_count = bounded_entry_count_u64(payload.total_node_count);
    let next_after_entry_id = optional_exact_str(
        payload.next_after_entry_id.as_deref().map(String::as_str),
        event_type,
        "next_after_entry_id",
        SESSION_ID_MAX_BYTES,
    )?;
    let mut entry_ids = std::collections::BTreeSet::new();
    let nodes = payload
        .nodes
        .iter()
        .map(|node| {
            let entry_id = exact_str(&node.entry_id, event_type, "entry_id", SESSION_ID_MAX_BYTES)?;
            let kind = match node.kind {
                WireNodeKind::Message => SessionTreeNodeKind::Message,
                WireNodeKind::Event => SessionTreeNodeKind::Event,
                WireNodeKind::Compaction => SessionTreeNodeKind::Compaction,
            };
            optional_exact_str(
                node.operation_id.as_deref(),
                event_type,
                "operation_id",
                SESSION_ID_MAX_BYTES,
            )?;
            build_session_tree_node(
                event_type,
                &mut entry_ids,
                entry_id,
                optional_exact_str(
                    node.parent_id.as_deref().map(String::as_str),
                    event_type,
                    "parent_id",
                    SESSION_ID_MAX_BYTES,
                )?,
                BoundedText::head(
                    &node
                        .created_at
                        .to_rfc3339_opts(chrono::SecondsFormat::AutoSi, true),
                    SESSION_UPDATED_AT_MAX_BYTES,
                    8,
                )
                .text,
                kind,
                node.role.as_ref().map(ToString::to_string),
                &node.preview,
                node.preview_truncated,
            )
        })
        .collect::<Result<Vec<_>, EventProjectionError>>()?;
    finish_session_tree_page(
        event_type,
        session,
        active_leaf_id,
        total_node_count,
        nodes,
        payload.truncated,
        next_after_entry_id,
    )
}

fn session_tree_navigation_from_wire(
    payload: &wisp_protocol::events::wire::RpcSessionTreeNavigated,
) -> Result<SessionTreeNavigation, EventProjectionError> {
    let event_type = "rpc.session.tree.navigated";
    finish_session_tree_navigation(
        event_type,
        required_session_identity_from_strs(
            &payload.session_id,
            &payload.session_path,
            None,
            event_type,
            "session_id",
            "session_path",
        )?,
        exact_str(
            &payload.selected_entry_id,
            event_type,
            "selected_entry_id",
            SESSION_ID_MAX_BYTES,
        )?,
        optional_exact_str(
            payload
                .previous_active_leaf_id
                .as_deref()
                .map(String::as_str),
            event_type,
            "previous_active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        optional_exact_str(
            payload.active_leaf_id.as_deref().map(String::as_str),
            event_type,
            "active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        optional_exact_str(
            payload.editor_text.as_deref(),
            event_type,
            "editor_text",
            MAX_APPLICATION_FRAME_BYTES,
        )?,
        stored_images_from_wire(&payload.editor_images, event_type, "editor_images")?,
        payload.changed,
        bounded_entry_count_u64(payload.entry_count),
    )
}

fn session_tree_unrevert_from_wire(
    payload: &wisp_protocol::events::wire::RpcSessionTreeUnreverted,
) -> Result<SessionTreeUnrevert, EventProjectionError> {
    let event_type = "rpc.session.tree.unreverted";
    finish_session_tree_unrevert(
        event_type,
        required_session_identity_from_strs(
            &payload.session_id,
            &payload.session_path,
            None,
            event_type,
            "session_id",
            "session_path",
        )?,
        exact_str(
            &payload.source_transition_id,
            event_type,
            "source_transition_id",
            SESSION_ID_MAX_BYTES,
        )?,
        optional_exact_str(
            payload
                .previous_active_leaf_id
                .as_deref()
                .map(String::as_str),
            event_type,
            "previous_active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        optional_exact_str(
            payload.active_leaf_id.as_deref().map(String::as_str),
            event_type,
            "active_leaf_id",
            SESSION_ID_MAX_BYTES,
        )?,
        bounded_entry_count_u64(payload.entry_count.get()),
    )
}
