"""Compatibility imports for transport-independent RPC command execution.

The implementation lives in :mod:`wisp.rpc.execution` so it can also power
in-process integrations without importing the CLI.
"""

from wisp.rpc.execution import RpcCommandExecutor, rpc_selected_session_state

__all__ = ["RpcCommandExecutor", "rpc_selected_session_state"]
