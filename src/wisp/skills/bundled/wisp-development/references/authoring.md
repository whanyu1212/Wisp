# Authoring Wisp Changes

## Source checkout

For changes to Wisp itself, work in a source checkout. Read `AGENTS.md` when present, `CLAUDE.md`,
the relevant README sections, implementation, and tests before editing. Register built-in
capabilities through `wisp.extensions.builtin.activate()` and keep changes in the narrowest owning
layer.

A Python embedder may pass ordered static factories to the runtime activation helper. Such factories
can register capabilities through `ExtensionAPI`; this is an embedding API, not filesystem plugin
discovery. The deterministic [`examples/extensions`](../../../../../../examples/extensions/) source
checkout example demonstrates a read-only tool, command metadata, an event handler, and synchronous
and asynchronous activation. Installed wheels do not include repository examples.

## Installed package

Do not modify files inside an installed `wisp-ai` environment. Installed Wisp includes this
read-only development skill for accurate architecture guidance, but it does not currently load
user/project Python extension files. To change Wisp, clone the repository and use its locked
development environment.

## Implementation approach

- Prefer a focused function or typed value over a speculative framework.
- Add deterministic fake/scripted-provider tests for provider-facing behavior.
- Keep provider behavior behind provider interfaces and tool execution separate.
- Bump event schemas with named breadcrumbs for serialized contract changes.
- Update README documentation when user-visible behavior changes.
