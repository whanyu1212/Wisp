# Upgrading from Wisp 0.2 to 0.3

Wisp 0.3 brings a redesigned terminal conversation and sidebar, working-tree diff review,
context breakdowns, richer tool activity, and more concise default replies. It keeps Python
3.12+, native wheels for macOS arm64 and Linux glibc 2.28+ x86_64, and the pure-wheel fallback.

0.3.0 is being prepared; the installation commands below apply after it is published.
For a move from 0.1, also read the [0.2 upgrade guide](./upgrading).

## Upgrade

Exit Wisp and back up `~/.wisp/sessions`, or your configured session directory, before upgrading.
Keep that backup separate from the directory the upgraded application will write to.

```bash
uv tool install --force --python 3.12 "wisp-ai==0.3.0"
wisp --version       # wisp 0.3.0
wisp-tui --version   # wisp-tui 0.3.0, on a native-wheel platform
```

Settings, credentials, and trust decisions need no migration. Source-checkout users must rebuild
the Rust TUI from the same checkout as the Python package; see
[Development setup](../contributing/development#rust-tui-scaffold).

## Session compatibility

0.3.0 can open 0.2.4 sessions. Session entry v6, persisted event envelope v1, and compaction v4
remain the supported formats. Reading a session does not rewrite it, and missing new metadata
uses defaults.

Once 0.3.0 appends records, a session may contain tool duration, process timestamps, and the
working directory used for an edit. The strict 0.2.4 reader does not know these presentation
fields and may reject those entries. Completed-run events also have additional totals that an
older typed event reader cannot decode. Unchanged schema stamps do not promise backward
readability across package releases.

## RPC and Python integrations

Upgrade external clients and the backend together. The TUI and Python subprocess transport
reject a different backend release at handshake. Use the 0.3.0 `schemas/live-rpc/` bundle; do not
pair a 0.2.4 frontend with a 0.3.0 backend.

This release adds `get_git_diff` / `RpcController.get_git_diff()`, `rpc.git_diff`, and
`rpc.workspace`. Context stats include a source breakdown, and tool/run events and history
snapshots include timing and completion metadata. Existing documented SDK entry points remain;
the [changelog](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md) describes the additions.

## Going back to 0.2.4

Exit Wisp and preserve the session directory written by 0.3.0 separately. Reinstall:

```bash
uv tool install --force --python 3.12 "wisp-ai==0.2.4"
wisp --version
wisp-tui --version
```

Use the untouched pre-upgrade session backup or start new sessions. Keep the 0.3.0-written
sessions available for a later return to 0.3.0. Do not delete or hand-edit JSONL records to make
an older reader accept them. Settings, credentials, and trust formats do not require a downgrade.
