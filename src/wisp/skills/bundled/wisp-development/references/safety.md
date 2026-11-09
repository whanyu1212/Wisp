# Safety Boundaries

Wisp enforces independent boundaries that extension work must preserve.

## Project trust

Project trust is stored globally by canonical absolute project path. Undecided fails closed.
Project-local settings, context, and skills are not inspected until trust is established. Any future
project executable extension discovery must apply the same pre-inspection boundary; a project must
never supply its own trust decision or redirect a security-critical path.

## Tool safety and approvals

Tools declare `read`, `mutating`, or `command` safety. Exposure and approval policy are runtime
choices outside model or extension control. Extension metadata cannot grant tool access, mark an
unsafe operation approved, or bypass print-mode restrictions.

`mutating` and `command` tools need approval. The permission mode is `ask` or `yolo`; YOLO
approves every safety class, is saved per project by `wisp.trust.permissions`, and lasts one
session at most in a broad folder such as the home folder. A tool that sets `confirm_every_call`
(the MCP proposal tools) is never approved by YOLO, `--yes`, or a session grant.

The agent mode is `build` or `plan`. Plan mode offers only `read` tools that build mode would offer
and narrows the tool policy to those names, so a plan-mode run cannot call a mutating tool even if
the model names one.

## Protected paths

Read and mutation tools reject configured protected paths. Wisp's own credential and settings files
(`~/.wisp/auth.json` or the configured auth path, `~/.wisp/mcp-auth.json`, the user
`~/.wisp/settings.json`, and the permissions directory) remain protected on every configuration
construction path, even when `protected_paths` is empty. Shell commands are not path-checked, which
is why command tools need approval. Extension code must use runtime tool and process APIs rather
than creating a parallel path that skips these checks.

## Stale writes

`write` and `edit` refuse with `stale_input` when an existing file was never read in the
conversation or changed since the model last read, wrote, or edited it. New files need no read.
`ToolContext.from_config` turns the check on for CLI, TUI, RPC, and SDK sessions; a bare
`ToolContext` leaves it off, so tools an embedder calls directly keep their old behavior.

## Process and frontend boundaries

Use the runtime-owned process supervisor for managed subprocesses. Cleanup must be bounded and
truthful. Never execute project Python in the TUI subprocess, and never send arbitrary Python or
frontend objects over RPC. Untrusted strings must be rendered as literal escaped text.
