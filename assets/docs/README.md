# Documentation TUI captures

These are static captures of Wisp's **production TUI draw path**, rendered from deterministic,
synthetic fixtures, not a live user's terminal or a claim that an MCP connection ran. No credentials,
private project content, real tool execution, or external service access is involved. The visible
`fake` provider and `demo-docs` server are deliberate. Captions on the site disclose the demo data.

Captured for HY-277 from the renderer at base revision `61a79b30`, plus the MCP fixture in the same
change. The full provenance is the Git history of `rust/wisp-tui/src/visual_tests.rs` and
`tests/fixtures/tui_visual_conversation.json`. These illustrate layout, not release-specific output.
Refresh them after a visible TUI interaction or rendering change.

| Master | Fixture | What it shows |
| --- | --- | --- |
| `tui-diff-previews.png` | `diff-previews` | Inline edit/write cards, added/removed lines, bounded previews |
| `tui-approval.png` | `approval` | A command approval gate with no default selection |
| `tui-mcp.png` | `mcp` | A synthetic connected server and its two tool names in `/mcp` |
| `tui-new-turn.png` | `new-turn` | Working status before reply text, with the composer still available |

Each master is 1200 × 720 pixels: 100 × 30 terminal cells in the default dark TUI theme, rendered
at 12 × 24 pixels per cell. The rasterizer uses Chromium's local monospace font at 20px; font glyphs
can vary by OS. RGB colors, reverse video, bold, italic, dim, underline, and strike come from the
production buffer. Dim text is drawn at half opacity. These are buffer captures, not emulator chrome.

## Regenerate

From the repository root, with the Rust toolchain and `site/` npm dependencies installed:

```bash
WISP_VISUAL_OUTPUT=/tmp/wisp-docs-captures \
  cargo test -p wisp-tui capture_conversation_screens -- --ignored
cd site
# If Chromium is not already installed: npm exec playwright install chromium
node --import tsx scripts/render-tui-captures.ts /tmp/wisp-docs-captures
npm run build
npm run test:browser -- tests/browser/entry-pages.spec.ts
```

The capture test emits a larger review matrix outside the repository. The rasterizer selects only
the four documented frames. Inspect the selected buffer text and resulting images before publishing;
never substitute a personal session dump. Keep each master here once. Astro generates 480/800/1200px
WebP variants during the build; do not commit those derived files or copies in Markdown source directories or
`site/public/`. Full-size image links, descriptive alt text, and captions keep the content usable
without JavaScript or animation. No recordings or autoplay are used.

`assets/media_assets.json` is not used or modified by this workflow.
