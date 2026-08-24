---
layout: home

hero:
  name: Wisp
  text: An agent that stays in sync with you
  tagline: Redirect it while it works. A reliable harness — typed events, explicit approvals, and a transcript you can inspect — keeps you and the agent on the same page.
  image:
    src: /hero.png
    alt: Wisp — a glowing spectral companion encircled by an orbiting ring
  actions:
    - theme: brand
      text: Quickstart
      link: /guide/quickstart
    - theme: alt
      text: How it stays in sync
      link: /guide/staying-in-sync
    - theme: alt
      text: GitHub
      link: https://github.com/whanyu1212/Wisp

features:
  - title: Steer mid-flight
    details: Press Enter in the TUI—or use RPC and SDK controls—to redirect an active run without rewriting the transcript. Follow-ups wait their turn.
  - title: Cancel without losing the thread
    details: Cooperative cancellation stops the run cleanly and leaves the session resumable, not corrupted.
  - title: Approvals you control
    details: Mutating and command tools stop for approval. The decision stays outside the model's reach.
  - title: Nothing happens off-screen
    details: Every action is a typed event with an enforced order, persisted to an inspectable JSONL transcript.
---

## One runtime, with you in control

Wisp is a coding agent for interactive work, scripts, and integrations. Its TUI, print mode, JSONL
RPC process, and Python SDK share one typed runtime, including the same session, approval,
cancellation, and event contracts. Each interface exposes the controls its input model can support:
the fullscreen TUI, RPC, and SDK can steer live runs and queue follow-ups, while print/JSON modes
are one-shot.

```bash
uv tool install "wisp-ai==0.1.0"
cd path/to/project
wisp
```

::: tip Wisp 0.1.0 is available
The first stable release includes live TUI steering, lazy provider startup, persistent sessions,
explicit safety approvals, and typed CLI, RPC, and SDK interfaces. Read the
[release notes](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md#010--2026-08-23) or
[install from PyPI](https://pypi.org/project/wisp-ai/0.1.0/).
:::

[Run your first prompt](./guide/quickstart), read [how Wisp stays in sync](./guide/staying-in-sync),
or [embed Wisp with the Python SDK](./guide/sdk).
