# Crafting Coding Agents

> *Building resilient, interactive coding agents from first principles — using Wisp as a production case study.*

---

There is a vast chasm between a 30-line LLM script that calls a function and a production-grade coding agent that developers trust inside their repositories.

A toy demo sends a prompt to an API, parses a single JSON tool call, prints the output, and exits. But real software engineering does not happen in a vacuum. In the real world:
- The user changes their mind mid-generation and needs to steer or cancel the agent.
- Commands hang, produce gigabytes of terminal flood, or fail halfway through an edit.
- Context windows fill up, requiring surgical token estimation and structured compaction.
- The agent crashes or gets disconnected, requiring lossless session hydration and replay.
- Different interfaces (TUIs, CLI pipes, background RPC servers, programmatic SDKs) all need consistent session, event, and safety behaviors.

This book is a deep, first-principles guide to **crafting coding agents from scratch**.

## The Pedagogical Approach

Every concept in this series follows a four-beat rhythm:

```mermaid
flowchart LR
  Concept["1. First Principles"] --> Scratch["2. From Scratch"]
  Scratch --> Realities["3. Production Realities"]
  Realities --> CaseStudy["4. Case Study: Wisp"]
```

1. **First Principles**: What problem are we actually trying to solve? Why does this layer exist in an agent architecture?
2. **From Scratch**: We implement the minimal, working mechanism in self-contained Python code (~30 to 80 lines) so you understand the raw physics before adding abstractions.
3. **Production Realities**: We analyze where naive implementations collapse under real developer workflows—race conditions, partial JSON streaming, error loops, and cancellation traps.
4. **Case Study: How Wisp Does It**: We dive into Wisp's codebase to examine its real-world implementation, exploring the architectural trade-offs it makes, where it shines, and where other architectures make different choices.

## The Creed: "May or May Not Be the Best Way"

Building agents is not a solved science; it is an evolving systems engineering discipline. 

Throughout this book, Wisp's architecture is presented not as dogmatic gospel, but as a **living case study**. Every design decision—such as decoupling the pure streaming loop from durable persistence, adopting cooperative request boundaries over aggressive task cancellation, or bridging a Python runtime with a native Rust TUI—has concrete benefits and concrete costs. We will examine both with rigorous intellectual honesty.

## The Curriculum

| Chapter | Core Concept | What You Will Build |
| :--- | :--- | :--- |
| **[1. The Core Loop](./01-core-loop.md)** | The Model-Tool Cycle | A streaming, provider-neutral agent turn loop |
| **2. Giving the Model Hands** | Tools, Filesystem & Safety | Safe tool execution, regex edits, and permission gates |
| **3. Context Windows & Compaction** | Token Budgets & Memory | Budget estimation and structured compaction snapshots |
| **4. Staying in Sync** | Steering & Interruption | Cooperative turn boundaries and priority queues |
| **5. Resilient State** | Persistence & Replay | Append-only JSONL event sourcing and transcript hydration |
| **6. Decoupling the Engine** | Interfaces & Frontends | An RPC command host driving CLI, SDK, and native TUIs |

---

Let's begin at the foundational layer: **[Chapter 1: The Core Loop — The Model-Tool Cycle](./01-core-loop.md)**.
