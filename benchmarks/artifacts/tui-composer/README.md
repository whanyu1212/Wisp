# Composer and footer refinement

These captures use the production Rust draw path and the shared synthetic
conversation fixture. They show the user panel's padded bottom edge, the rounded
composer with the transcript background, and the footer's separate shortcut and status areas.

| Capture | State |
| --- | --- |
| [Dark](dark.png) | Completed reply and a draft, 100 × 30 |
| [Light](light.png) | New turn with an empty steering composer, 80 × 24 |
| [Monochrome](mono.png) | Completed reply and a draft, 100 × 30 |
| [Streaming reply](streaming.png) | Spinner beside the active reply, 100 × 30 |
| [Tool pulse](tool-pulse.gif) | Running dot pulses; success and failure stay steady |
| [Permission gate](permission-gate.png) | Four choices, 100 × 30 |
| [Compact gate](permission-gate-compact.png) | All choices at 30 × 8 |
| [Project permissions](permissions.png) | Saved default picker, 100 × 30 |
| [Compact](compact.png) | New turn at the minimum 30 × 8 size |

The compact layout drops the composer frame and optional spacing to keep the
prompt and input visible. These are terminal-buffer captures; font rendering
depends on the installed monospace fonts.

To reproduce the full matrix of nine states, four sizes, and three color variants:

```sh
WISP_VISUAL_OUTPUT=/tmp/wisp-composer cargo test -p wisp-tui capture_conversation_screens -- --ignored
uv run python -m benchmarks.tui_visual_capture /tmp/wisp-composer
rsvg-convert /tmp/wisp-composer/rust-conversation-dark-100x30.svg -o /tmp/wisp-composer/dark.png
```

The [previous conversation captures](../tui-conversation/README.md) show the
earlier composer and footer for comparison.
