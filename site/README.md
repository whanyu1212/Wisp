# Wisp documentation in Astro

This implements the HY-313 foundation, HY-314 shared editorial layouts, and HY-315
static search. It builds a static site under `/Wisp/` while the production workflow
continues to deploy mdBook.
Markdown stays authoritative in `../book/src/`, with release notes loaded directly
from the root `../CHANGELOG.md`; tutorial code stays in
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
Mermaid redraws, and theme persistence/system changes. Search checks exercise the
real built index, result destinations, pagination, focus, both themes, lazy loading,
stale requests, and failed runtime/index/fragment downloads. Chromium is a development-only
test dependency; on Linux use `npm exec playwright install -- --with-deps chromium`.
Failure traces are saved in ignored `test-results/`.

`build` renders the complete corpus, indexes it with the pinned Pagefind CLI, and
then checks the actual files in `dist/`:
all canonical/legacy routes, the 358 captured heading IDs, directory index aliases,
local link fragments, and base-prefixed assets. It fails on invalid output rather
than relying on a server's extension fallback. The frozen baseline lives in
`../book/migration/astro/routes.json` and should change only after an intentional
compatibility decision. Old TUI anchors retain visible fallback links and redirect
only moved fragments to their focused pages, on initial load and hash changes.

`.github/workflows/astro.yml` runs these checks on pull requests and main-branch
changes to the site, root changelog, Markdown sources, migration baseline, tutorial examples, and
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
- `src/content/tui.ts` projects the authoritative `book/src/guide/tui.md` into
  TUI basics plus Keys, Commands, and Sessions children. It partitions existing
  content (including paragraphs in the long introduction), not copied Markdown.
  Exact boundaries fail loudly on source drift; conservation tests check that prose,
  code, and tables survive once. MCP instructions remain in Commands until HY-273.
  Child edit links still target the original source. HY-316 must replace this temporary
  adapter with physical Markdown pages while retaining the routes and fragment fallbacks.
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
- `src/components/Search.astro` and its adjacent `search.ts` provide a native modal
  search dialog with labeled controls, live loading/empty/failure feedback, and paged
  results. Search resources load only after a nonempty query; closing the dialog
  restores focus, and stale requests cannot replace newer results. The entry point
  stays hidden without JavaScript; reading and native navigation remain available.
  The generated `/Wisp/pagefind/` bundle and explicit base URL also work from nested
  pages. Search uses the existing `searchable` metadata to index article bodies and
  title/section metadata, not navigation, compatibility pages, or maintainer records.
  `search-client.ts` lazily starts `search-worker.ts`, keeping indexing queries off
  the main thread. The worker observes its own downloads because Pagefind can
  swallow failed index chunks; failures must not appear as an empty result set.
  Requests time out after 15 seconds rather than leaving a permanent loading state.
  Use `build` and `preview` to try search locally: the Astro development server does
  not serve the generated index.
- `src/components/Diagrams.astro` imports Mermaid only on pages with diagrams.
  All seven existing diagrams retain readable source without JavaScript; successful
  rendering collapses that fallback into a disclosure. Failures leave it open. Wide
  diagrams keep their natural text size in a keyboard-scrollable region.
  This avoids a browser-download prerequisite for static builds. The Mermaid runtime
  has a large lazy chunk; future optimization must preserve the conditional loading.
- `src/content/check-output.ts` validates output, including the compatibility pages
  for the release records and the SDK audit. Those records still read their original
  source files; they are hidden from normal navigation and marked `noindex`.
- HY-270 loads the root `CHANGELOG.md` into the `pages` collection with adapter-supplied
  metadata (`changelog.md` is an identity, not a copied source file). The loader watches
  root changelog edits, and the renderer resolves both current `book/src/` links and
  historical `site/` links to current routes. The footer and homepage release-notes
  link lead to `/Wisp/changelog.html`; its edit link targets the root source.
- `/Wisp/maintainers/releases/` lists every release record from `maintainerRecords`.
  Contributing links to this archive, which is excluded from navigation and search.
  Canonical archive pages and old contributing URLs preserve historical headings;
  redirects retain fragments and provide links without JavaScript. Per HY-312's
  migration plan, physical source relocation waits for HY-316 so mdBook remains
  deployable from the same files. No records or ticket history are deleted.
- `tests/content.test.ts` exercises invalid metadata, include failures, actual Astro
  rendering, source-link resolution and compatibility-validation failures.

The two SDK audit fragment corrections in `book/src/reference/sdk-capability-audit.md`
fix existing links for both renderers. Shared branding stays in `assets/`; static
asset endpoints emit it into the build without adding duplicate tracked images.
The agent-runtime sequence diagram uses `AgentLoop` as its participant identifier
because `Loop` is a reserved token in the pinned Mermaid parser; its visible label
remains `run_agent_loop`.

## Follow-up scope

HY-314 provides the shared responsive layouts, navigation, light/dark/system themes,
focus handling, and built-site browser coverage; HY-315 adds Pagefind static search.
HY-269 aligns homepage section destinations with the shared user-first navigation:
Using Wisp → Reference → Learn → Inside Wisp → Contributing. Homepage cards use the
first three groups from that same model; the primary call to action still goes
straight to Quickstart. Built-site tests check desktop/mobile section parity and
every listed page's sidebar and previous/next destinations.
HY-274 projects the TUI split through the adapter described above. HY-276 keeps the SDK guide
focused on runnable tasks and the reference on API contracts. The pinned SDK audit is listed under
Inside Wisp at `architecture/sdk-capability-audit.html`; its old reference URL is a hidden,
non-indexed compatibility page with fragment redirects and no-JavaScript links. Source files and
edit links remain under `book/src/` until cutover, and the immutable audit evidence is unchanged.
Interfaces and Staying in sync remain separate: interface selection versus live-control semantics.
HY-277 supplies the Astro homepage in `src/components/Home.astro`: a direct install/start path,
production-rendered TUI captures, the shared Use / Reference / Learn destinations, and secondary
architecture/contributor links. It replaces the repeated mdBook home article in Astro while retaining
its public heading IDs. `book/src/index.md` still supplies the mdBook landing page until cutover.
`SectionIndex.astro` derives guide, reference, and contributing listings from the same navigation
metadata as the sidebar. Markdown introductions remain authoritative; there is no second page list.

`TuiCapture.astro` uses Astro's image pipeline for responsive WebP output and links to full-size PNG
masters stored once in `assets/docs/`. Captures also appear in Quickstart and relevant TUI pages.
All four are static production-buffer captures with explicitly labeled synthetic data, not live
service screenshots. See `assets/docs/README.md` at the repository root for provenance and
regeneration. The capture script uses the existing Playwright dependency, only during development.
No capture, browser, or Rust build is required to build the site from the checked-in masters.

HY-316 moves content to `site/src/content/`, transfers metadata into frontmatter,
updates Python test paths, switches deployment to Astro and adds the combined print
view, then retires mdBook. The existing `.github/workflows/docs.yml` is unchanged.
The generated search bundle ships with the Astro build; production remains on mdBook
until the cutover.
