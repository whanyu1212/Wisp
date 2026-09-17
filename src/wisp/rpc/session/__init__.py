"""Session-scoped RPC command handlers and transient session state.

Modules here run, mutate, and read the active coding session on behalf of the
``RpcCommandExecutor``. Import from the defining module (for example
``wisp.rpc.session.run``); this package intentionally re-exports nothing so
importing it stays cheap and cycle-free with ``wisp.rpc.execution``.
"""
