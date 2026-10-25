# Documentation TUI captures

These are static captures of Wisp's **production TUI draw path**, rendered from deterministic,
synthetic fixtures, not a live user's terminal or a claim that an MCP connection ran. No credentials,
private project content, real tool execution, or external service access is involved. The visible
`fake` and `demo` providers, `demo-model`, `/projects/demo-app`, and `demo-*` servers are deliberate.
Captions on the site disclose the demo data.

First captured for HY-277; the current set adds the Ember theme, the showcase and welcome scenes,
and the bundled font. The full provenance is the Git history of `rust/wisp-tui/src/visual_tests.rs`
and `tests/fixtures/tui_visual_conversation.json`. These illustrate layout, not release-specific output.
Refresh them after a visible TUI interaction or rendering change.

| Master | Fixture | What it shows |
| --- | --- | --- |
| `tui-showcase.png` | `showcase` | A finished turn with tool rows and an inline diff beside the docked side panel; the homepage hero |
| `tui-welcome.png` | `welcome` | The welcome screen and its Braille mascot; source for the README banner, not shown on the site |
| `tui-diff-previews.png` | `diff-previews` | Inline edit/write cards, added/removed lines, bounded previews |
| `tui-approval.png` | `approval` | A command approval gate with no default selection |
| `tui-mcp.png` | `mcp` | A synthetic connected server and its two tool names in `/mcp` |
| `tui-new-turn.png` | `new-turn` | Working status before reply text, with the composer still available |

All masters use the Ember TUI theme, the palette the site and banner share. `tui-showcase.png` and
`tui-welcome.png` are 150 × 34 cells, wide enough for the side panel to dock (3600 × 1632 pixels);
the others are 100 × 30 cells (2400 × 1440 pixels). Each cell is 12 × 24 pixels, drawn at twice that
size so large displays stay sharp. Text uses JetBrains Mono at 20px (SIL OFL 1.1, bundled through
the `@fontsource/jetbrains-mono` dev dependency); symbols outside its Latin subsets fall back to the
local monospace font. Box-drawing borders and Braille dots are drawn geometrically so they join
across cells on every OS. RGB colors, reverse video, bold, italic, dim, underline, and strike come
from the production buffer. Dim text is drawn at half opacity. These are buffer captures, not
emulator chrome.

## Regenerate

From the repository root, with the Rust toolchain and `site/` npm dependencies installed:

```bash
WISP_VISUAL_OUTPUT=/tmp/wisp-docs-captures \
  cargo test -p wisp-tui capture_conversation_screens -- --ignored
cd site
# If Chromium is not already installed: npm exec playwright install chromium
node --import tsx scripts/render-tui-captures.ts /tmp/wisp-docs-captures
node --import tsx scripts/render-banner.ts /tmp/wisp-docs-captures
npm run build
npm run test:browser -- tests/browser/entry-pages.spec.ts
```

The capture test emits a larger review matrix outside the repository. The rasterizer selects only
the six documented frames, and the banner script rebuilds `assets/wisp-banner-v2.png` from two of
them. Inspect the selected buffer text and resulting images before publishing; never substitute a
personal session dump. Keep each master here once. Astro generates 480/800/1200/1600/2400px
WebP variants during the build; do not commit those derived files or copies in Markdown source directories or
`site/public/`. Full-size image links, descriptive alt text, and captions keep the content usable
without JavaScript or animation. No recordings or autoplay are used.

`assets/media_assets.json` is not used or modified by this workflow.
