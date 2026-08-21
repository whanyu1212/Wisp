---
title: SDK capability audit
---

# SDK capability audit

This audit compares Wisp's supported Python embedding surface with a fixed Pi SDK reference. It is a
capability and developer-experience comparison, not a promise of source, binary, wire, or behavioral
compatibility with Pi.

## Pinned reference

The Pi side is pinned to:

- repository: [`earendil-works/pi`](https://github.com/earendil-works/pi)
- release: [`v0.84.2`](https://github.com/earendil-works/pi/releases/tag/v0.84.2)
- commit: [`914cf1472e715297caa30db4b9535d534a9eb718`](https://github.com/earendil-works/pi/commit/914cf1472e715297caa30db4b9535d534a9eb718)
- package: [`@earendil-works/pi-coding-agent` 0.84.2](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/coding-agent/package.json)
- evidence: the pinned [SDK guide](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/coding-agent/docs/sdk.md)
  and [SDK examples](https://github.com/earendil-works/pi/tree/914cf1472e715297caa30db4b9535d534a9eb718/packages/coding-agent/examples/sdk)

The Wisp side was audited from `main` at
[`f98260a4d4cf98a4c8dbb4aaafcf813bb0bae567`](https://github.com/whanyu1212/Wisp/commit/f98260a4d4cf98a4c8dbb4aaafcf813bb0bae567),
after the public surface, examples, guide, and compatibility policy landed. Later changes to either
project do not silently change this result; refresh the pin and every row together for a new audit.

## Reading the matrix

| Disposition | Meaning |
|---|---|
| **Shipped** | Wisp exposes the capability through its supported public SDK or shared RPC contract. |
| **Partial** | Wisp ships a useful subset, while a named roadmap issue owns the remaining public contract. |
| **Planned** | The capability is intentionally not presented as shipped; the linked issue owns it. |
| **Open decision** | The linked issue owns an evidence-based decision; its outcome is not promised. |
| **Intentional difference** | Wisp deliberately uses a different contract and has no parity requirement. |

Pi names describe the pinned TypeScript SDK only. Wisp names and links are authoritative for Wisp.

## Capability matrix

| Capability | Pi v0.84.2 reference | Wisp disposition | Wisp contract or owner |
|---|---|---|---|
| In-process startup | `createAgentSession()` creates an `AgentSession`. | **Shipped** | [`InProcessWisp.start()` and `from_environment()`](./sdk#wisp-sdk) start the shared Python runtime. Wisp currently requires AnyIO's asyncio backend. |
| Awaitable command completion and event fan-out | `prompt()` waits for the accepted run; `subscribe()` supports independent listeners. | **Planned** | Wisp command methods currently return command IDs and one ordered `events()` iterator carries results. Awaitable results and independent subscriptions belong to [#400](https://github.com/whanyu1212/Wisp/issues/400). |
| Direct state and settled lifecycle | `AgentSession` exposes model, messages, streaming state, and `agent.waitForIdle()`. | **Planned** | Wisp currently reports typed snapshots through correlated commands. Direct snapshots and settled/idle primitives belong to [#401](https://github.com/whanyu1212/Wisp/issues/401). |
| Typed lifecycle events | `AgentSessionEvent` callbacks cover messages, turns, tools, queues, compaction, and retries. | **Shipped** | Wisp emits frozen, typed, versioned [`WispEvent` models](./sdk#wisp-events) across SDK and JSONL RPC. The protocol-first schema and compatibility rules are an intentional Wisp contract. |
| Steering, follow-up, cancellation, and compaction | `steer()`, `followUp()`, `abort()`, and `compact()` control an active session. | **Shipped** | [`RpcController`](./sdk#live-queues-and-cancellation) exposes steering, follow-up, queue policy, targeted cancellation, and compaction through shared commands and events. |
| Tool selection and caller-owned composition | Built-ins can be selected; `customTools` and inline extensions are accepted at session creation. | **Partial** | Wisp ships tool contracts, safe visibility controls, and static extension composition, but `InProcessWisp` cannot yet accept a caller-built runtime or arbitrary provider. That public injection boundary belongs to [#402](https://github.com/whanyu1212/Wisp/issues/402). |
| Prompt, skill, context, and template overrides | `DefaultResourceLoader` supports typed overrides and reload for these resources. | **Planned** | Wisp discovers project instructions and skills today; typed caller-supplied resource overrides belong to [#403](https://github.com/whanyu1212/Wisp/issues/403). |
| Persistent sessions and tree operations | `SessionManager` stores parent-linked JSONL history and exposes traversal and branching. | **Shipped** | Wisp's [append-only sessions](../guide/sessions) support listing, resume/select, naming, clone, fork, tree navigation, transcript paging, and direct typed storage access. The formats are not interchangeable with Pi. |
| In-memory sessions and generalized session replacement | `SessionManager.inMemory()` and `AgentSessionRuntime` support replacement and cwd-bound rebuilds. | **Planned** | Wisp supports persistent session selection and derivation, but true in-memory sessions and an atomic caller-facing replacement runtime belong to [#404](https://github.com/whanyu1212/Wisp/issues/404). |
| Model, authentication, and settings management | `ModelRuntime` and `SettingsManager` provide application-facing management APIs. | **Partial** | Wisp can configure provider, model, effort, mode, and compaction on the active host and can start from explicit or discovered settings. Cohesive model, credential, and settings management belongs to [#405](https://github.com/whanyu1212/Wisp/issues/405). |
| Cleanup, health, restart, and recovery | `dispose()` cleans up a session; runtime replacement failures are caller-visible. | **Partial** | Wisp has bounded `aclose()` and subprocess cleanup. Public health, restart, recovery, diagnostics, and long-running observability primitives belong to [#406](https://github.com/whanyu1212/Wisp/issues/406). |
| Process-isolated integration | Pi exposes RPC mode as an alternative to its in-process TypeScript SDK. | **Shipped** | Wisp intentionally keeps [`JsonlSubprocessRpcTransport`](./sdk#jsonlsubprocessrpctransport) as the process-isolated, language-neutral boundary using the same commands and events as the Python SDK. |
| Trust and unsafe-tool approval | The pinned Pi SDK emphasizes direct tool/resource composition. | **Intentional difference** | Wisp keeps project trust, protected paths, tool safety classes, and re-entrant approval requests in the shared runtime. SDK convenience will not bypass these boundaries. |
| Distribution boundary | Pi publishes separate coding-agent, agent-core, AI, protocol, and client packages. | **Open decision** | Wisp currently publishes only `wisp-ai`. Evidence-based evaluation of a lightweight client distribution and package boundaries belongs to [#409](https://github.com/whanyu1212/Wisp/issues/409). |
| Guide and executable examples | Pi ships an SDK guide and 13 focused TypeScript examples. | **Shipped** | Wisp ships a [Python SDK guide](../guide/sdk), [API reference](./sdk), compatibility policy, and deterministic offline examples for its current public surface. Planned APIs are linked rather than demonstrated as available. |

## Conclusions

Wisp already covers the core embedding workflow: typed in-process startup, streamed lifecycle
consumption, safe live control, persistent sessions, process-isolated RPC, deterministic examples,
and explicit compatibility guarantees. It intentionally differs from Pi by making the versioned
command/event protocol and Wisp's trust and approval policy common to every interface.

The remaining developer-experience gaps are not hidden parity claims. They are assigned to
[#400](https://github.com/whanyu1212/Wisp/issues/400) through
[#406](https://github.com/whanyu1212/Wisp/issues/406), with distribution boundaries assigned to
[#409](https://github.com/whanyu1212/Wisp/issues/409). Those issues may change Wisp's future public
surface; they are not prerequisites for treating the documentation and compatibility work in
[#407](https://github.com/whanyu1212/Wisp/issues/407) as complete.

A future audit should select a new immutable Pi release, review every row against then-current Wisp
behavior, update evidence and dispositions, and run the documentation synchronization tests. It
must not infer compatibility merely because the two projects expose similarly named capabilities.
