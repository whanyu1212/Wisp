"""Non-session RPC command handlers.

Modules here handle configuration, provider connections, control commands,
catalog inspection, and project-file discovery. Import from the defining module
(for example ``wisp.rpc.handlers.control``); this package intentionally
re-exports nothing so importing it stays cheap and cycle-free with
``wisp.rpc.execution``.
"""
