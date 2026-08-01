"""Compatibility imports for the RPC coordinator.

The transport-independent coordinator lives in :mod:`wisp.rpc.coordinator`.
"""

# ruff: noqa: F401
from wisp.rpc.coordinator import (
    _MAX_QUEUED_RPC_COMMANDS,
    RpcCoordinator,
    _RpcCancelResult,
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcDispatchResult,
    _RpcInputClosed,
    _RpcInputCommand,
    _RpcPromptReady,
    _RpcRunningCommand,
    _RpcSessionState,
)

__all__ = [
    "RpcCoordinator",
    "_MAX_QUEUED_RPC_COMMANDS",
    "_RpcCancelResult",
    "_RpcCommandCompleted",
    "_RpcControlEvent",
    "_RpcDispatchResult",
    "_RpcInputClosed",
    "_RpcInputCommand",
    "_RpcPromptReady",
    "_RpcRunningCommand",
    "_RpcSessionState",
]
