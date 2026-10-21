# Wisp documentation in Astro

This implements the HY-313 foundation and HY-314 shared editorial layouts. It builds
a static site under `/Wisp/` while the production workflow continues to deploy mdBook.
Markdown stays authoritative in `../book/src/`; tutorial code stays in
`../examples/crafting_agents/`. No content copies or new frontend framework are needed.

## Develop and verify

Use Node **22.22.1** (`.nvmrc`) and npm **10.9.4**. From this directory:

```sh
npm ci
npm run dev
```

Open the URL printed by Astro, including `/Wisp/`. These commands verify the site:

```sh
npm run check
npm test
npm run build
npm run format:check
npm exec playwright install chromium
npm run test:browser
```

Browser tests use the built `dist/` output (run `build` first), served by an isolated
Astro preview on port 4322. They check desktop, tablet, and mobile layouts, keyboard
and no-JavaScript navigation, local code/table scrolling, clipboard success/failure,
Mermaid redraws, and theme persistence/system changes. Chromium is a development-only
test dependency; on Linux use `npm exec playwright install -- --with-deps chromium`.
Failure traces are saved in ignored `test-results/`.

`build` renders the complete corpus and then checks the actual files in `dist/`:
all canonical/legacy routes, the 358 captured heading IDs, directory index aliases,
local link fragments, and base-prefixed assets. It fails on invalid output rather
than relying on a server's extension fallback. The frozen baseline lives in
`../book/migration/astro/routes.json` and should change only after an intentional
compatibility decision. The old TUI anchors continue pointing to the intact TUI
page until HY-274 splits it.

`.github/workflows/astro.yml` runs these checks on pull requests and main-branch
changes to the site, Markdown sources, migration baseline, tutorial examples, and
shared assets, including built-site Chromium checks. It does not deploy; the separate
docs workflow still publishes mdBook.

To inspect production output:

```sh
npm run preview -- --port 4321
# Stop with Ctrl+C (or `npm exec astro preview stop` if started with --background).
```

From the repository root, retain the existing content and example checks:

```sh
uv run pytest tests/docs tests/repository/test_images.py
mdbook build book
```

## Where to work

- `content-map.json` supplies validated metadata while mdBook is still active.
  The `pages` and `maintainerRecords` collections read directly from the same Markdown
  sources. Metadata must cover every source exactly once; duplicate output paths,
  missing titles, conflicting sibling order, bad parents, and missing checkpoints fail.
- `src/content/loader.ts` orchestrates source loading, include expansion, Markdown
  rendering and compatibility projection. It watches Markdown, metadata and example
  changes during development. Invalid edits are reported and do not partially replace
  the current collection. Restart development after changing the loader itself.
- `src/content/includes.ts` supports the existing anchored mdBook includes, complete
  files, and explicit inclusive line ranges. Missing/ambiguous anchors, invalid ranges,
  malformed directives and paths escaping the repository fail the build.
- `src/content/render.ts` preserves mdBook heading IDs (including `{#explicit-id}`),
  rewrites source links and shared branding, renders alerts/tables, and prepares Mermaid.
  Highlighting uses GitHub's high-contrast light theme on the paper surface so even
  comments and variable names meet normal-text contrast; browser checks pin this.
- `src/layouts/` holds the editorial shell and page/compatibility rendering. Home,
  documentation, and course pages share the header/footer; course pages add chapter
  typography, case-study grouping, and checkpoint cards without duplicating prose.
  `src/content/navigation.ts` projects sibling hierarchy, breadcrumbs, and section-local
  previous/next links from collection metadata. Native mobile navigation/TOC disclosures
  work without JavaScript; Escape dismisses an open disclosure and restores focus.
  The section `index.astro` route and Astro's `preserve` build format produce actual
  `section/index.html` files alongside normal `article.html` files.
- `src/components/Theme.astro` resolves an explicit light/dark override before paint;
  otherwise CSS follows the system, even without JavaScript or storage access. The
  labeled System/Light/Dark select persists overrides and can reset to system mode.
  Theme changes redraw diagrams serially, including live system preference changes.
- `src/components/CodeBlocks.astro` progressively adds copy buttons when the Clipboard
  API is available, with live success/failure feedback. Code/table scroll areas are
  keyboard-focusable in static output. Static articles are not hydrated.
- `src/components/Diagrams.astro` imports Mermaid only on pages with diagrams.
  All seven existing diagrams retain readable source without JavaScript; successful
  rendering collapses that fallback into a disclosure. Failures leave it open. Wide
  diagrams keep their natural text size in a keyboard-scrollable region.
  This avoids a browser-download prerequisite for static builds. The Mermaid runtime
  has a large lazy chunk; future optimization must preserve the conditional loading.
- `src/content/check-output.ts` validates output, including the compatibility pages
  for the 17 release records and the SDK audit. Those records still read their original
  source files; they are hidden from normal navigation and marked `noindex`.
- `tests/content.test.ts` exercises invalid metadata, include failures, actual Astro
  rendering, source-link resolution and compatibility-validation failures.

The two SDK audit fragment corrections in `book/src/reference/sdk-capability-audit.md`
fix existing links for both renderers. Shared branding stays in `assets/`; static
asset endpoints emit it into the build without adding duplicate tracked images.
The agent-runtime sequence diagram uses `AgentLoop` as its participant identifier
because `Loop` is a reserved token in the pinned Mermaid parser; its visible label
remains `run_agent_loop`.

## Follow-up scope

HY-315 owns Pagefind. HY-314 provides the shared responsive layouts, navigation,
light/dark/system themes, focus handling, and built-site browser coverage.
HY-269/HY-270/HY-274/HY-276/HY-277 own final navigation/content restructuring, the
changelog, TUI splits, SDK prose and real homepage imagery. The foundation preserves
the complete homepage article below the new editorial opening; final content editing
will remove repetition in HY-277.

HY-316 moves content to `site/src/content/`, transfers metadata into frontmatter,
updates Python test paths, switches deployment to Astro and adds the combined print
view, then retires mdBook. The existing `.github/workflows/docs.yml` is unchanged.
No search UI is displayed until search actually works.
