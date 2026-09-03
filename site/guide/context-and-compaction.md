---
title: Context & compaction
---

# Context & compaction

Each turn sends an ordered system-instruction stack before the user prompt: Wisp's core coding
policy, bounded project context, guidance for exposed tools and skills when present, an instruction
boundary, and the active mode policy. The boundary makes project, tool, and skill content subordinate
to Wisp's core policy, the user's request, and runtime-enforced permissions.

## Project context

Project context includes the working directory, Git branch and capped status summary, detected root
files, exposed tools, and trusted project instructions.

Context files load from the trusted context root down to the working directory, parent instructions
first. Nearer files may refine earlier project guidance but cannot override higher-priority
instructions. In each directory Wisp uses the first Pi-compatible match: `AGENTS.md`, `AGENTS.MD`,
`CLAUDE.md`, `CLAUDE.MD`. Symlinked, protected, or out-of-scope files are skipped.

Project instructions are bounded separately from the tool list, so a large instruction file cannot
hide the available tools.

Project context is trust-gated — in untrusted projects Wisp reads no local instruction files or
settings. This is stricter than Pi, and keeps project guidance inside the same boundary as project
settings and future extensions. See [Tools & safety](./tools-and-safety#project-trust).

## Accounting

Before each request Wisp emits `context.estimated`, a deterministic approximation of the system
prompt, active messages, pending tool results, and tool schemas using the Unicode-aware
`utf8_bytes_div_4_v2` method (a conservative `ceil(len(utf8_bytes) / 4)` heuristic computed over
JSON-serialized payloads). When the catalog provides a context window, the event also reports the reserve, estimated
percentage, remaining budget, and whether the estimate crossed it. Unknown models remain permissive.

Provider-reported `usage.total_tokens` is kept separately as the authoritative observation for a
completed request. Session statistics sum provider totals exactly as reported and never reconstruct
totals from input/output categories.

In the TUI this is the difference between `context 53%` (a provider observation) and `context ~53%`
(an estimate).

## Compaction

`/compact [instructions]` replaces older provider-visible turns with a structured checkpoint while
retaining the latest complete user turn verbatim. The summary request uses the active provider,
model, and effort without tools. If the model cannot produce a complete summary, compaction fails
without changing replay.

Compaction is **append-only** and lossy only at replay time: original messages stay in the JSONL
audit log while later prompts receive the checkpoint plus retained recent context. Wisp never splits
a tool call from its result.

### Automatic compaction

Automatic threshold compaction is enabled by default and runs after a completed prompt when active
context exceeds the reserved input budget. It triggers only when usage is strictly greater than
`context_window - context_reserve_tokens`. If an automatic summary fails, Wisp preserves the
completed prompt and leaves replay unchanged. Disable with `WISP_AUTO_COMPACTION=0` or
`"auto_compaction_enabled": false`.

### Overflow recovery

When a provider explicitly rejects an input for context overflow, Wisp can compact and retry the same
prompt once. Recovery is skipped after mutating or command tools, or after deltas have already
reached an interface, because side effects and partial responses cannot be safely repeated.

On providers with a cataloged compaction limit (currently `openai-codex`), Wisp also checks the
budget proactively before and after each tool round, since those providers report overflow as a
generic error rather than a distinguishable one. Compaction can only replace turns before the one
currently in progress, so if the active turn's own tool results are what's driving the overage, Wisp
truncates the oldest of them — preserving each result's tail, where diagnostic output usually is —
before falling back to a terminal error.
