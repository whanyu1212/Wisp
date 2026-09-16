# Wisp

Wisp is a coding agent that stays in sync with you. You can redirect it while it works, approve changes before they happen, and inspect the transcript afterward.

Its TUI, print mode, JSONL RPC process, and Python SDK share one typed runtime. Sessions, approvals, cancellation, and event ordering behave consistently across those interfaces, within the controls each input model can support.

## Install Wisp

```bash
uv tool install "wisp-ai==0.1.0"
cd path/to/project
wisp
```

> [!TIP]
> **Wisp 0.1.0 is available**
>
> The first stable release includes live TUI steering, lazy provider startup, persistent sessions,
> explicit safety approvals, and typed CLI, RPC, and SDK interfaces. Read the
> [release notes](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md#010--2026-08-23) or
> [install from PyPI](https://pypi.org/project/wisp-ai/0.1.0/).

## Start reading

Begin with the [Introduction](./guide/index.md) and [Quickstart](./guide/quickstart.md). Then read [how Wisp stays in sync](./guide/staying-in-sync.md) or [embed Wisp with the Python SDK](./guide/sdk.md).

The later parts of this book cover the public reference, Wisp's internal architecture, and contributor workflows.
