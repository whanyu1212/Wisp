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

## Protected paths

Read and mutation tools reject configured protected paths. Authentication storage remains protected
on every configuration construction path. Extension code must use runtime tool and process APIs
rather than creating a parallel path that skips these checks.

## Process and frontend boundaries

Use the runtime-owned process supervisor for managed subprocesses. Cleanup must be bounded and
truthful. Never execute project Python in the TUI subprocess, and never send arbitrary Python or
Textual objects over RPC. Untrusted strings must be rendered as literal escaped text.
