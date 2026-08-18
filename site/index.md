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
    details: RPC and SDK clients can queue a correction while the agent is running. Steering redirects the active run without rewriting the transcript; follow-ups wait their turn.
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
RPC and SDK clients can steer live runs, the TUI queues interactive follow-ups, and print/JSON modes
are one-shot.

```bash
uv tool install "wisp-ai==0.1.0rc2"
cd path/to/project
wisp
```

[Run your first prompt](./guide/quickstart) or read [how Wisp stays in sync](./guide/staying-in-sync).
