---
title: Agent Skills
---

# Agent Skills

Wisp discovers metadata from directories that follow the
[Agent Skills specification](https://agentskills.io/specification). Inspect the current catalog and
isolated validation diagnostics with:

```bash
wisp skills [project-path]
```

## Discovery and precedence

| Precedence | Location |
|---|---|
| 1 (highest) | `<project>/.wisp/skills/<name>/SKILL.md` |
| 2 | `<project>/.agents/skills/<name>/SKILL.md` |
| 3 | `~/.wisp/skills/<name>/SKILL.md` |
| 4 | `~/.agents/skills/<name>/SKILL.md` |
| 5 (lowest) | Wisp package-owned skills |

Each `SKILL.md` must begin with bounded YAML frontmatter containing a specification-valid `name` and
`description`; the declared name must match its parent directory. Invalid skills are skipped
individually and reported without hiding valid entries. Symlinked, protected, out-of-root, and
oversized metadata is rejected.

Project locations are not scanned until project trust is granted; user locations remain available in
untrusted projects.

Wisp ships a read-only `wisp-development` skill with current architecture, extension API, safety,
authoring, and verification guidance. It is available from source checkouts and installed wheels,
including in untrusted projects. Higher-precedence project or user skills may shadow it using the
same deterministic conflict rules.

## The `skill` tool

When the read-only `skill` tool is exposed, Wisp adds a separately bounded index of escaped skill
names and descriptions to model context. The model can call `skill` with `name` to load the selected
`SKILL.md` instructions, or add a forward-slash `resource` path to read a supporting file inside the
same skill directory.

Loaded content is delimited and labeled as subordinate task guidance; it cannot override Wisp's core
policy, the user's request, or runtime controls. Enable the tool with `--allow-read-tools`,
`--allow-tool skill`, or `--all-tools`, following the same exposure rules as other tools. Print mode
continues to expose no tools unless one of those options is selected.

Instruction and resource reads are UTF-8, bounded, protected-path aware, and reject absolute paths,
traversal, symlinks, junctions, non-regular files, and targets outside the selected skill. Absolute
skill paths are not shown to the model. Bundled scripts are returned only as text and never execute
automatically; execution still requires the normal command tool and approval policy. The optional
`allowed-tools` metadata field is descriptive only and cannot grant tool access or approval.

## Explicit invocation

The active operation keeps one immutable catalog snapshot. First-time project trust refreshes that
snapshot before the pending provider request begins. Invoke a cataloged skill explicitly from any
CLI, JSON/RPC, SDK, or TUI prompt flow with:

```text
/skill:<name> [additional instructions]
```

The directive must begin at the first character; names are case-sensitive, and the optional request
may span multiple lines. Wisp securely loads the bounded `SKILL.md` body, expands it into the
provider-visible user message, and applies the same policy to initial prompts, steering, and
follow-ups. Explicit invocation does not require exposing the `skill` tool and does not grant tool
access or approval.

## In the TUI

The TUI fetches the active immutable snapshot at startup. Type `/skill:` to see deterministic prefix
completions for the available names, or run `/skills` to inspect the cached catalog and its discovery
diagnostics without rescanning the filesystem. Package and user skills are always available; project
skills refresh after first-time project trust is applied. Both surfaces remain available while a
prompt is running. Skill descriptions, diagnostics, paths, and requests are displayed as literal text
rather than terminal markup.

## Persistence

Sessions retain the exact submitted directive, additional request, instruction-content SHA-256,
truncation state, and provider-visible expansion as typed data. Replay uses that persisted expansion
even if the source skill later changes or disappears; a new invocation reads the current resource and
records a new hash. Live and restored TUI transcripts show a compact invocation row from that typed
metadata instead of exposing the provider-visible expansion.

Skill installation, hot reload, bundled-script execution, fuzzy completion, and skill-management UI
remain unsupported.

## Examples

- [`examples/extensions`](https://github.com/whanyu1212/Wisp/tree/main/examples/extensions) — a
  deterministic Python embedder example for static extension authoring. Wisp does not discover or
  import that example (or other user/project Python files) automatically.
- [`examples/skills/wisp-code-review`](https://github.com/whanyu1212/Wisp/tree/main/examples/skills/wisp-code-review)
  — a complete opt-in review skill, including installation instructions and a progressively loaded
  checklist.
