"""Compatibility imports for RPC configuration transitions.

The transport-independent implementation lives in :mod:`wisp.rpc.configuration`.
"""

from wisp.rpc.configuration import RpcProjectConfiguration, _ConfigOverrides, _RpcConfigureOverrides

__all__ = ["RpcProjectConfiguration", "_ConfigOverrides", "_RpcConfigureOverrides"]
