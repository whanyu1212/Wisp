"""Typed events emitted by the Wisp agent core.

This package is the public import surface for every event model: import from
``wisp.events`` rather than from the lifecycle submodules. The submodules group
models by the runtime stage that produces them:

- ``_base``: the ``WispEvent`` envelope and shared literal vocabularies
- ``run``: agent run, turn, and streamed assistant-message events
- ``usage``: token usage, cost, context budget, and compaction events
- ``tools``: tool call, approval, trust, and tool-result events
- ``sessions``: persisted-session, transcript, session-tree, and queue events
- ``rpc``: RPC command lifecycle, backend state, catalog, and project-file reports
- ``errors``: the terminal ``ErrorEvent``

``KnownWispEvent`` is the discriminated union the live RPC protocol bundle is
generated from; its member order is part of the generated schema and must stay
stable.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, TypeAdapter

from wisp.events._base import (
    CompactionReason,
    FinishReason,
    JsonObject,
    ManagedProcessState,
    MessageRole,
    QueueKind,
    QueueMode,
    RetryReason,
    RunOutcome,
    ToolCallSnapshot,
    ToolPresentationStatus,
    WispEvent,
    _MessageOriginEvent,
    utc_now,
)
from wisp.events.errors import ErrorEvent
from wisp.events.rpc import (
    MAX_RPC_CONNECTION_LABEL_CHARS,
    MAX_RPC_CONNECTION_METHODS,
    MAX_RPC_CONNECTION_PROVIDERS,
    MAX_RPC_DEVICE_CODE_ATTEMPTS,
    MAX_RPC_DEVICE_CODE_CHARS,
    MAX_RPC_DEVICE_CODE_URI_CHARS,
    MAX_RPC_MODEL_CATALOG_EFFORT_LEVELS,
    MAX_RPC_MODEL_CATALOG_MODELS,
    MAX_RPC_MODEL_CATALOG_MODELS_PER_PROVIDER,
    MAX_RPC_MODEL_CATALOG_PROVIDERS,
    MAX_RPC_MODEL_DISPLAY_CHARS,
    MAX_RPC_MODEL_EFFORT_CHARS,
    MAX_RPC_MODEL_ID_CHARS,
    MAX_RPC_OAUTH_EXPIRY_CHARS,
    MAX_RPC_PROVIDER_ID_CHARS,
    CodingSessionState,
    PermissionState,
    ProjectFilesInvalidated,
    RpcCommandArgument,
    RpcCommandDescriptor,
    RpcCommandFinished,
    RpcCommandsReported,
    RpcCommandStarted,
    RpcConnectionCatalogReported,
    RpcConnectionCatalogSnapshot,
    RpcConnectionMethodSnapshot,
    RpcConnectionProviderSnapshot,
    RpcDeviceCodeProgressReported,
    RpcDeviceCodeReported,
    RpcMcpServerSnapshot,
    RpcMcpStatusReported,
    RpcMcpStatusSnapshot,
    RpcModelCatalogEntry,
    RpcModelCatalogReported,
    RpcModelCatalogSnapshot,
    RpcModelEffort,
    RpcModelId,
    RpcModelProviderSnapshot,
    RpcModelSelectionSnapshot,
    RpcPermissionsReported,
    RpcProjectFile,
    RpcProjectFilesReported,
    RpcProviderId,
    RpcSkillCatalogEntry,
    RpcSkillCatalogSnapshot,
    RpcSkillDiagnostic,
    RpcSkillsReported,
    RpcStateReported,
    RpcStateSnapshot,
    SkillCatalogUpdated,
)
from wisp.events.run import (
    AgentCompleted,
    AgentStarted,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ModelProviderAutoSwitched,
    ProviderRetrying,
    TurnCompleted,
    TurnStarted,
)
from wisp.events.sessions import (
    QueueItemsRemoved,
    QueueMessageInjected,
    QueueUpdated,
    RpcMessageSnapshot,
    RpcMessagesReported,
    RpcMessageToolCallSnapshot,
    RpcMessageToolResultSnapshot,
    RpcSessionCloned,
    RpcSessionForked,
    RpcSessionNameChanged,
    RpcSessionSelected,
    RpcSessionsReported,
    RpcSessionSummary,
    RpcSessionTreeNavigated,
    RpcSessionTreeNode,
    RpcSessionTreeReported,
    RpcSessionTreeUnreverted,
    RpcSkillInvocationSnapshot,
    SessionSaved,
    SkillInvoked,
    _RpcSessionDerived,
)
from wisp.events.tools import (
    ProjectConfigApplied,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    TrustRequested,
    TrustResolved,
    _ToolResultEvent,
)
from wisp.events.usage import (
    BillableTokenUsage,
    CompactionCompleted,
    CompactionPolicyStatus,
    CompactionStarted,
    ContextAccountingMethod,
    ContextBudget,
    ContextEstimate,
    ContextEstimated,
    ContextObservation,
    ContextOverflow,
    ContextPressure,
    CostUnavailableReason,
    SessionCostSummary,
    SessionStats,
    SessionStatsReported,
    TokenUsage,
    UsageCost,
    UsageCostRates,
)

__all__ = [
    "MAX_RPC_CONNECTION_LABEL_CHARS",
    "MAX_RPC_CONNECTION_METHODS",
    "MAX_RPC_CONNECTION_PROVIDERS",
    "MAX_RPC_DEVICE_CODE_ATTEMPTS",
    "MAX_RPC_DEVICE_CODE_CHARS",
    "MAX_RPC_DEVICE_CODE_URI_CHARS",
    "MAX_RPC_MODEL_CATALOG_EFFORT_LEVELS",
    "MAX_RPC_MODEL_CATALOG_MODELS",
    "MAX_RPC_MODEL_CATALOG_MODELS_PER_PROVIDER",
    "MAX_RPC_MODEL_CATALOG_PROVIDERS",
    "MAX_RPC_MODEL_DISPLAY_CHARS",
    "MAX_RPC_MODEL_EFFORT_CHARS",
    "MAX_RPC_MODEL_ID_CHARS",
    "MAX_RPC_OAUTH_EXPIRY_CHARS",
    "MAX_RPC_PROVIDER_ID_CHARS",
    "AgentCompleted",
    "AgentStarted",
    "BillableTokenUsage",
    "CodingSessionState",
    "CompactionCompleted",
    "CompactionPolicyStatus",
    "CompactionReason",
    "CompactionStarted",
    "ContextAccountingMethod",
    "ContextBudget",
    "ContextEstimate",
    "ContextEstimated",
    "ContextObservation",
    "ContextOverflow",
    "ContextPressure",
    "CostUnavailableReason",
    "ErrorEvent",
    "FinishReason",
    "JsonObject",
    "JsonObjectAdapter",
    "KnownWispEvent",
    "KnownWispEventAdapter",
    "ManagedProcessState",
    "MessageCompleted",
    "MessageDelta",
    "MessageRole",
    "MessageStarted",
    "ModelProviderAutoSwitched",
    "PermissionState",
    "ProjectConfigApplied",
    "ProjectFilesInvalidated",
    "ProviderRetrying",
    "QueueItemsRemoved",
    "QueueKind",
    "QueueMessageInjected",
    "QueueMode",
    "QueueUpdated",
    "RetryReason",
    "RpcCommandArgument",
    "RpcCommandDescriptor",
    "RpcCommandFinished",
    "RpcCommandStarted",
    "RpcCommandsReported",
    "RpcConnectionCatalogReported",
    "RpcConnectionCatalogSnapshot",
    "RpcConnectionMethodSnapshot",
    "RpcConnectionProviderSnapshot",
    "RpcDeviceCodeProgressReported",
    "RpcDeviceCodeReported",
    "RpcMcpServerSnapshot",
    "RpcMcpStatusReported",
    "RpcMcpStatusSnapshot",
    "RpcMessageSnapshot",
    "RpcMessageToolCallSnapshot",
    "RpcMessageToolResultSnapshot",
    "RpcMessagesReported",
    "RpcModelCatalogEntry",
    "RpcModelCatalogReported",
    "RpcModelCatalogSnapshot",
    "RpcModelEffort",
    "RpcModelId",
    "RpcModelProviderSnapshot",
    "RpcModelSelectionSnapshot",
    "RpcPermissionsReported",
    "RpcProjectFile",
    "RpcProjectFilesReported",
    "RpcProviderId",
    "RpcSessionCloned",
    "RpcSessionForked",
    "RpcSessionNameChanged",
    "RpcSessionSelected",
    "RpcSessionSummary",
    "RpcSessionTreeNavigated",
    "RpcSessionTreeNode",
    "RpcSessionTreeReported",
    "RpcSessionTreeUnreverted",
    "RpcSessionsReported",
    "RpcSkillCatalogEntry",
    "RpcSkillCatalogSnapshot",
    "RpcSkillDiagnostic",
    "RpcSkillInvocationSnapshot",
    "RpcSkillsReported",
    "RpcStateReported",
    "RpcStateSnapshot",
    "RunOutcome",
    "SessionCostSummary",
    "SessionSaved",
    "SessionStats",
    "SessionStatsReported",
    "SkillCatalogUpdated",
    "SkillInvoked",
    "TokenUsage",
    "ToolApprovalRequested",
    "ToolApprovalResolved",
    "ToolCallRequested",
    "ToolCallSnapshot",
    "ToolExecutionEnded",
    "ToolExecutionStarted",
    "ToolPresentationStatus",
    "ToolResultReady",
    "TrustRequested",
    "TrustResolved",
    "TurnCompleted",
    "TurnStarted",
    "UsageCost",
    "UsageCostRates",
    "WispEvent",
    "_MessageOriginEvent",
    "_RpcSessionDerived",
    "_ToolResultEvent",
    "utc_now",
    "wisp_event_from_dict",
    "wisp_event_from_json",
]


type KnownWispEvent = Annotated[
    AgentStarted
    | TurnStarted
    | ProviderRetrying
    | MessageStarted
    | MessageDelta
    | MessageCompleted
    | ContextEstimated
    | ContextPressure
    | ContextOverflow
    | ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | TrustRequested
    | TrustResolved
    | ProjectConfigApplied
    | ToolExecutionEnded
    | ToolResultReady
    | TurnCompleted
    | SessionSaved
    | CompactionStarted
    | CompactionCompleted
    | AgentCompleted
    | RpcCommandStarted
    | RpcCommandFinished
    | SessionStatsReported
    | RpcStateReported
    | RpcPermissionsReported
    | RpcCommandsReported
    | RpcModelCatalogReported
    | RpcConnectionCatalogReported
    | RpcDeviceCodeReported
    | RpcDeviceCodeProgressReported
    | RpcSkillsReported
    | RpcProjectFilesReported
    | ProjectFilesInvalidated
    | RpcMcpStatusReported
    | SkillCatalogUpdated
    | RpcMessagesReported
    | RpcSessionsReported
    | RpcSessionSelected
    | RpcSessionCloned
    | RpcSessionForked
    | RpcSessionNameChanged
    | RpcSessionTreeReported
    | RpcSessionTreeNavigated
    | RpcSessionTreeUnreverted
    | QueueUpdated
    | QueueItemsRemoved
    | QueueMessageInjected
    | SkillInvoked
    | ModelProviderAutoSwitched
    | ErrorEvent,
    Field(discriminator="type"),
]
KnownWispEventAdapter: TypeAdapter[KnownWispEvent] = TypeAdapter(KnownWispEvent)
JsonObjectAdapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def wisp_event_from_json(line: str) -> KnownWispEvent:
    """Parse one JSONL event line into a typed Wisp event."""

    return KnownWispEventAdapter.validate_json(line)


def wisp_event_from_dict(data: JsonObject) -> KnownWispEvent:
    """Parse one event dictionary into a typed Wisp event."""

    return KnownWispEventAdapter.validate_python(data)
