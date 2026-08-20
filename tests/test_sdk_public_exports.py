"""Contract tests for the supported Python SDK import surface."""

from __future__ import annotations

import wisp.rpc as rpc_package
import wisp.sdk as sdk_package
from wisp.config import WispConfig
from wisp.events import (
    EVENT_SCHEMA_VERSION,
    KnownWispEvent,
    RpcCommandFinished,
    ToolApprovalRequested,
    TrustRequested,
    WispEvent,
    wisp_event_from_dict,
    wisp_event_from_json,
)
from wisp.rpc import (
    ApprovalCommand,
    ApprovalScope,
    CancelCommand,
    ClearQueueCommand,
    CloneSessionCommand,
    CompactCommand,
    ConfigureCommand,
    FollowUpCommand,
    ForkSessionCommand,
    GetCommandsCommand,
    GetMcpStatusCommand,
    GetMessagesCommand,
    GetQueueStateCommand,
    GetSessionsCommand,
    GetSessionStatsCommand,
    GetSessionTreeCommand,
    GetSkillsCommand,
    GetStateCommand,
    InitCommand,
    JsonlSubprocessRpcTransport,
    NavigateSessionTreeCommand,
    NewSessionCommand,
    PopQueueCommand,
    PromptCommand,
    RpcCommand,
    RpcController,
    RpcTransport,
    SelectSessionCommand,
    SetQueueModeCommand,
    SetSessionNameCommand,
    ShutdownCommand,
    SteerCommand,
    TrustCommand,
    UnrevertSessionTreeCommand,
)
from wisp.runtime import ExtensionAPI, WispRuntime
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.sessions import JsonlSession, JsonlSessionStore
from wisp.tools import Tool, ToolContext, ToolResult

_EXPECTED_SDK_EXPORTS = {
    "InProcessOptions",
    "InProcessWisp",
}
_EXPECTED_RPC_EXPORTS = {
    "ApprovalCommand",
    "ApprovalScope",
    "CancelCommand",
    "ClearQueueCommand",
    "CloneSessionCommand",
    "CompactCommand",
    "ConfigureCommand",
    "FollowUpCommand",
    "ForkSessionCommand",
    "GetCommandsCommand",
    "GetMcpStatusCommand",
    "GetMessagesCommand",
    "GetQueueStateCommand",
    "GetSessionTreeCommand",
    "GetSessionStatsCommand",
    "GetSessionsCommand",
    "GetSkillsCommand",
    "GetStateCommand",
    "InitCommand",
    "JsonlSubprocessRpcTransport",
    "NavigateSessionTreeCommand",
    "NewSessionCommand",
    "PopQueueCommand",
    "PromptCommand",
    "RpcCommand",
    "RpcController",
    "RpcTransport",
    "SelectSessionCommand",
    "SetSessionNameCommand",
    "SetQueueModeCommand",
    "ShutdownCommand",
    "SteerCommand",
    "TrustCommand",
    "UnrevertSessionTreeCommand",
}


def test_sdk_package_exports_are_explicit_and_complete() -> None:
    assert set(sdk_package.__all__) == _EXPECTED_SDK_EXPORTS
    assert InProcessWisp is sdk_package.InProcessWisp
    assert InProcessOptions is sdk_package.InProcessOptions


def test_rpc_package_exports_are_explicit_and_complete() -> None:
    assert set(rpc_package.__all__) == _EXPECTED_RPC_EXPORTS
    for name in _EXPECTED_RPC_EXPORTS:
        assert getattr(rpc_package, name) is not None


def test_documented_sdk_namespaces_import_supported_contracts() -> None:
    """Keep imports used by the SDK guide visible to static and runtime checks."""

    public_symbols = (
        WispConfig,
        EVENT_SCHEMA_VERSION,
        KnownWispEvent,
        RpcCommandFinished,
        ToolApprovalRequested,
        TrustRequested,
        WispEvent,
        wisp_event_from_dict,
        wisp_event_from_json,
        ApprovalCommand,
        ApprovalScope,
        CancelCommand,
        ClearQueueCommand,
        CloneSessionCommand,
        CompactCommand,
        ConfigureCommand,
        FollowUpCommand,
        ForkSessionCommand,
        GetCommandsCommand,
        GetMcpStatusCommand,
        GetMessagesCommand,
        GetQueueStateCommand,
        GetSessionsCommand,
        GetSessionStatsCommand,
        GetSessionTreeCommand,
        GetSkillsCommand,
        GetStateCommand,
        InitCommand,
        JsonlSubprocessRpcTransport,
        NavigateSessionTreeCommand,
        NewSessionCommand,
        PopQueueCommand,
        PromptCommand,
        RpcCommand,
        RpcController,
        RpcTransport,
        SelectSessionCommand,
        SetQueueModeCommand,
        SetSessionNameCommand,
        ShutdownCommand,
        SteerCommand,
        TrustCommand,
        UnrevertSessionTreeCommand,
        ExtensionAPI,
        WispRuntime,
        InProcessOptions,
        InProcessWisp,
        JsonlSession,
        JsonlSessionStore,
        Tool,
        ToolContext,
        ToolResult,
    )

    assert all(symbol is not None for symbol in public_symbols)
