# Wisp

![Wisp, a coding agent that stays in sync with you](https://raw.githubusercontent.com/whanyu1212/Wisp/main/assets/wisp-banner-v2.png)

Wisp is a coding agent that stays in sync with you. You can redirect it while it works, approve changes before they happen, and inspect the transcript afterward.

Its TUI, print mode, JSONL RPC process, and Python SDK share one typed runtime. Sessions, approvals, cancellation, and event ordering behave consistently across those interfaces, within the controls each input model can support.

## Choose a starting point

- **[Build a coding agent](crafting-agents/index.md):** learn the model/tool loop,
  build runnable checkpoints, and examine Wisp's engineering choices and trade-offs.
- **[Use Wisp](guide/quickstart.md):** install the application and start working in
  your repository.
- **[Understand Wisp's architecture](architecture/index.md):** find the runtime
  boundaries and implementation entry points.

## Install Wisp

```bash
uv tool install --python 3.12 wisp-ai
cd path/to/project
wisp
```

> [!TIP]
> **Wisp 0.2.3 is available**
>
> 0.2 makes the Rust terminal UI the interactive interface, adds MCP server management and the
> providers' own web search, and never saves trust or YOLO for your home folder. Read the
> [release notes](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md) and, if you are coming
> from 0.1, the [upgrade guide](./guide/upgrading.md).

## Start reading

Begin with the [Introduction](./guide/index.md) and [Quickstart](./guide/quickstart.md). Then read [how Wisp stays in sync](./guide/staying-in-sync.md) or [embed Wisp with the Python SDK](./guide/sdk.md).

The later parts of this book cover the public reference, Wisp's internal architecture, and contributor workflows.
