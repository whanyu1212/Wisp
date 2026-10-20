# Wisp Astro migration plan

Plan for [HY-312](https://linear.app/queryverse/issue/HY-312), prepared on 20 October 2026.
The migration uses a custom static Astro site on GitHub Pages at `/Wisp/`.
The visual direction is editorial and expressive, following Hanyu's preference:
large serif headlines, warm surfaces, an indigo accent, and real product imagery.

This package defines the proposed implementation contract for HY-313 through HY-316.
It does not migrate content or change production deployment. Hanyu approved the planning package and layout proposal on 20 October 2026 and
authorized HY-313 implementation. Final homepage copy and real product imagery
remain HY-277 deliverables.

## Review materials

- [Interactive layout study](layouts.html): homepage, documentation, and course views,
  with light/dark and 390px mobile-width controls. Open directly in a browser, or run
  `python3 -m http.server 8766 --bind 127.0.0.1 --directory book/migration/astro`
  and visit `http://127.0.0.1:8766/layouts.html`.
- [Route and anchor inventory](routes.json): every current Markdown page, rendered
  heading ID, legacy path, proposed destination, index alias, and README public URL.
  This is a dated compatibility baseline, not a second content/navigation database.
- The layout study contains illustrative excerpts and a labeled image placement.
  Actual screenshots, final homepage copy, and section introductions belong to HY-277.

## Directory and transition decisions

Use `site/` for the Astro project. Keep ignored `/docs/` private and unchanged.

```text
site/
  astro.config.mjs          static output, /Wisp/ base, file-format HTML
  package.json             dev, check, build; Pagefind runs after the static build
  package-lock.json        one pinned package manager and reproducible install
  src/
    content.config.ts      validated build-time collections
    pages/                 homepage, content route, archive and compatibility routes
    layouts/               Home, Docs, Course, Archive
    components/            navigation, TOC, code, notices, search entry, theme control
    content/               authored Markdown after cutover
    content/maintainers/    historical release records, excluded from public navigation
    assets/                site-only screenshots and diagrams after cutover
    navigation.ts          section labels/order and metadata-driven navigation
    routes.ts              base-aware URL creation and source-link resolution
  public/                  favicon and other assets requiring stable names
  migration/               this plan and the frozen compatibility inventory after cutover
book/src/                  sole Markdown source until the cutover
examples/crafting_agents/  sole source of runnable tutorial code throughout
assets/                    shared repository branding; imported, not duplicated
CHANGELOG.md               sole changelog source throughout
```

During HY-313, load `book/src/` into Astro and keep mdBook deployable. Maintain the new
navigation/route metadata in one validated `site/content-map.json`, keyed by source
path; do not add frontmatter that mdBook might display as article content. Render
both systems from the same files. Exclude `SUMMARY.md` from the Astro collection.
The metadata registry must cover every published source exactly once.

At HY-316, move public Markdown once into `site/src/content/`, historical release
records into `site/src/content/maintainers/releases/`, and the SDK audit into
`site/src/content/architecture/`. Transfer the registry fields into frontmatter
and remove the transitional registry in the same change. Rebase include paths,
source links, edit links, and tests together. Move this planning package into
`site/migration/`; remove obsolete mdBook files only after Astro verification.
Build the changelog directly from the root file, with adapter-supplied metadata.
No second maintained copy of Markdown, changelog text, or shared branding.

## Content and navigation contract

Use a build-time `pages` collection for normal content and a `maintainerRecords`
collection for historical releases. The homepage is an Astro page using the same
navigation model; the changelog is a build-time entry loaded from the root file.
Plain Markdown is the default. MDX is allowed only when a page needs embedded components.

| Field | Contract |
| --- | --- |
| `title` | Nonempty title; do not derive navigation labels from filenames |
| `description` | Nonempty summary for metadata and section introductions |
| `route` | Unique base-relative output path, e.g. `guide/quickstart.html` or `guide/index.html`; no leading slash, traversal, query, fragment, or `/Wisp/` prefix |
| `section` | `using`, `reference`, `learn`, `inside`, `contributing`, or `maintainers` |
| `order` | Nonnegative integer, unique among visible siblings |
| `parent` | Optional content entry ID; must exist, be in the same section, and form an acyclic hierarchy |
| `layout` | `docs`, `course`, or `archive`; the homepage has its own template |
| `navTitle` | Optional shorter label; defaults to `title` |
| `listed` | Boolean; false for maintainer records and compatibility pages |
| `searchable` | Boolean; false for archive and compatibility pages |
| `chapter` | Positive integer for numbered course chapters only; cases have none |
| `checkpoint` | Optional repository-relative example path for a course chapter; validate existence |

One navigation projection supplies desktop/mobile navigation, index links, breadcrumbs,
and previous/next links. Previous/next stays inside a section; numbered chapters use
chapter order, with cases listed separately. Do not make independent link arrays in
components. Fail builds on duplicate output paths, duplicate sibling orders, missing
sources, unknown parents, missing include anchors, or unresolved internal links.

Use Astro's current supported build-time content APIs and static route generation;
choose and pin the toolchain in HY-313. This plan deliberately avoids locking the work
to an unverified package version. See the official
[collection documentation](https://docs.astro.build/en/guides/content-collections/)
and [routing documentation](https://docs.astro.build/en/guides/routing/).

## Information architecture

Primary order: **Using Wisp → Reference → Learn → Inside Wisp → Contributing**.
Changelog and GitHub are secondary links. Keep established path prefixes (`guide`,
`reference`, `crafting-agents`, `architecture`, `contributing`) even when navigation
labels differ; changing the generator does not require renaming every public route.

| Section | Content order and responsibility |
| --- | --- |
| Using Wisp | Introduction, Installation, Quickstart, Providers and auth; TUI basics with Keys, Commands, Sessions children; Staying in sync, Interfaces, Tools and safety, Sessions, Context and compaction, Skills, Python SDK, Upgrading |
| Reference | Overview, CLI, Python SDK, Configuration, Environment, Project files, Compatibility |
| Learn | Overview; existing chapters 1–5 in order; the two existing case studies. Future chapters stay outside migration scope |
| Inside Wisp | Architecture overview, Agent runtime, Rust TUI boundary, pinned SDK capability audit |
| Contributing | Overview, Development, Building, Testing; optional concise Release process linking to the archive |
| Secondary | Changelog, GitHub, archive index linked from contributing/release documentation |

HY-269 implements this ordering. HY-277 supplies useful section introductions and
homepage content. HY-276 keeps the SDK guide task-oriented and the reference contractual;
keep both routes and their existing anchors. Retain Interfaces and Staying in sync as
separate pages for this migration; any later merge needs a separate compatibility map.

### TUI split ownership

Keep `/Wisp/guide/tui.html` as the TUI entry and basics page. Add these child routes:

| Destination | Existing material |
| --- | --- |
| `guide/tui.html` | Startup/composer, rendering behavior, large pastes, updates, steering, completions/file picker, modes, flags, provider-native search |
| `guide/tui/keybindings.html` | Keybinding preferences, keybinding table, configuration examples |
| `guide/tui/commands.html` | Slash commands, MCP management, context/skills/MCP browsers |
| `guide/tui/sessions.html` | Resuming long sessions, prompt history; session/scrollback material extracted from the long introductory section |

HY-274 must account for paragraphs inside the long `Rust TUI` section, not just move
headings. HY-271's corrections and HY-273's MCP guide are independent: preserve current
MCP instructions in Commands until the dedicated guide is ready. Retain old IDs even
where visible headings lose the historical “Rust” prefix.

## URL and fragment compatibility

Choose static `.html` routes for articles and `index.html` for section landings. Both
`/Wisp/guide/` and `/Wisp/guide/index.html` must serve the same landing. Emit files at
those exact paths and use `/Wisp/` exactly once in links/assets. No server redirects,
request middleware, or GitHub Pages extension fallback is part of the design.

`routes.json` records each content page and heading. Unmoved pages keep their paths
and IDs. New visible headings may use new IDs only if old IDs remain as aliases.
Do not assume Astro's heading slugger matches mdBook for punctuation, inline code,
Unicode, or repeated headings. Compare rendered IDs against the baseline.

| Legacy route class | Destination and behavior |
| --- | --- |
| Normal content | Same `.html` file, same heading IDs |
| Section/root `index.html` and `/` aliases | Preserve both entry forms |
| `reference/sdk-capability-audit.html` | `architecture/sdk-capability-audit.html`; hidden compatibility document preserves every old fragment |
| `contributing/*-release.html` | `maintainers/releases/*-release.html`; historical content remains available with its original heading IDs, excluded from search and primary navigation |
| `guide/tui.html` without fragment | TUI basics page, not an automatic redirect |
| `guide/tui.html` with moved fragment | Exact fragment mapping to Keys, Commands, or Sessions; ordinary visible fallback links exist at the old IDs |
| `print.html` | Retain a static combined printable view at cutover, preserving existing print heading IDs; inventory separately from content pages |
| `404.html` | Custom base-aware not-found page with home and section links |
| Search internals/theme scripts | Replace implementation assets; do not promise compatibility for private mdBook search indexes or generated theme hashes |

For wholly moved pages, generate a small HTML compatibility page with a canonical
link and a heading/link for every known old fragment. Its script reads the fragment,
looks up a fixed local mapping, and uses `location.replace` to the new base-prefixed
URL. Preserve query parameters. Unknown fragments go to the new page with the original
fragment and an on-page fallback link. With JavaScript disabled, visitors can still
follow visible destination links; a generic meta-refresh must not bypass fragment
mapping. Do not claim HTTP 301 responses on GitHub Pages.

For the retained TUI page, run the mapping only for moved fragments on initial load
and `hashchange`; keep existing in-page navigation for everything else. Old moved
IDs become labeled fallback links. All destination anchors and fallback IDs must
exist in built HTML. Compatibility pages are excluded from Pagefind and the sitemap;
canonical content remains searchable. `routes.json` is the fixed baseline; later
implementation adds executable validation and a maintained redirect manifest.

## Editorial design and layouts

The approved preference is **more editorial and expressive**. The preview explores a
warm paper/ink palette with violet accents, large serif display text, italic wordmark,
numbered entry points, and thin rules. Body text and navigation remain system sans;
code uses the platform monospace stack. No web font download is required for the
initial implementation. Refine the typeface later only with a licensed, self-hosted choice.

| Element | Light | Dark |
| --- | --- | --- |
| Page | `#faf7ef` | `#1e2027` |
| Text | `#25251f` | `#ececf1` |
| Secondary text | `#605e57` | `#b2b4c0` |
| Accent | `#5542a0` | `#b6b3ff` |
| Rules | `#d5d0c5` | `#3b3d48` |

The prototype uses these values to make the proposal tangible. Production must verify
text/control contrast, focus visibility, highlighted code, and Mermaid in both themes.
Use system preference by default and persist an explicit theme override. Respect
reduced motion. No entrance animations or animated background is necessary.

- **Homepage:** asymmetric two-column opening; headline and immediate install/start
  path on the left, a real current TUI capture on the right. A numbered Use / Reference /
  Learn row follows. Secondary architecture and contributor links sit below. HY-277 owns
  final copy, truthful screenshots/captions, alt text, and static fallbacks for recordings.
- **Documentation:** shared top navigation, 230px section sidebar, article up to 70–75ch,
  and a 190px headings TOC when space permits. Clear breadcrumbs, callouts, code copy
  controls, and previous/next within the section. Reference tables may use additional
  article width with localized horizontal scrolling, never whole-page overflow.
- **Course:** same shell, stronger chapter typography, an objective and prerequisites,
  ordered chapter navigation, runnable checkpoint/source links, and diagrams alongside
  the explanation. Case studies do not masquerade as numbered chapters.
- **Mobile:** one column, stacked homepage, native disclosure navigation, compact
  in-flow “On this page” disclosure before article text, horizontal scroll only inside
  code/tables. Keep all section and chapter links available without JS. At medium widths,
  drop the right TOC first; collapse sidebar/navigation below 700px.
- **Accessibility:** semantic landmarks, skip link, visible keyboard focus, 44px touch
  targets, labeled controls and captions. A search dialog must manage/restore focus and
  close with Escape. Theme/search scripts enhance static reading rather than hydrate it.

The prototype demonstrates page hierarchy, themes, and a narrow layout. It does not
implement search, production code highlighting, actual Mermaid, theme persistence,
full content navigation, or the mobile article TOC. Those remain HY-314/HY-315 acceptance
checks, not functionality claimed by this layout study.

## Rendering and dependency inventory

The source audit found 53 content pages, 14 code includes, seven Mermaid fences across
five pages, and 15 offline tutorial checkpoint invocations in docs CI.

| Boundary | Required migration behavior |
| --- | --- |
| `book/src/SUMMARY.md`, `tests/docs/test_book.py` | Replace the one-page-once navigation check with collection/source coverage; do not weaken it |
| `{{#include path:anchor}}` in chapters 1–5 | Resolve anchored code from `examples/crafting_agents/`; missing files/anchors fail build. Preserve code language, dedentation and readable source attribution; no copied snippets |
| Mermaid fences | Render diagrams with legible light/dark styles; prefer build-time SVG after a HY-313 feasibility check. If client rendering is needed, load only on pages containing diagrams and provide a readable static fallback |
| Alerts, tables, links, code | Preserve GitHub-style NOTE/TIP/WARNING/Caution semantics and GFM tables; inventory all actual alert types before implementation. Resolve `.md` and extensionless source links through the route map; validate fragments |
| `book/theme/head.hbs`, CSS, Mermaid scripts, icons | Replace mdBook-specific shell; move only still-used assets. Do not copy generated theme JS |
| `assets/wisp-banner-v2.png` | Shared branding remains authoritative in `assets/`; import or derive build output without duplicating tracked bytes. Coordinate changed URLs with `tests/repository/test_images.py` |
| `tests/support/paths.py`, `tests/docs/*` | Update source roots and moved-page expectations at cutover; preserve SDK, compatibility, TUI ownership, pinned audit and runnable-example contracts |
| `.github/workflows/docs.yml` | Keep checkpoint execution; add reproducible Node install/build, Pagefind, exact output link checks, legacy/fragment checks and browser smoke checks; extend path filters to site, assets, changelog and relevant tests/helpers |
| `README.md`, `CONTRIBUTING.md`, contributor Development page | Keep public links valid; replace mdBook install/build/serve instructions at cutover |
| `pyproject.toml` build-guide reference | Update its source path when content moves |
| Root `CHANGELOG.md` | Load at build time; add it to workflow path filters; no copied changelog |

Retain all historical release pages in the archive. Preserve the SDK audit's immutable
reference revisions and linked history. Asset manifest `assets/media_assets.json` is
outside scope. HY-308 may introduce a justfile later; recheck its status before HY-316.

## Implementation handoff and acceptance

1. **HY-313:** create the scaffold and metadata adapter; render Quickstart, SDK reference,
   and chapter 1 with its include and diagram, then the full corpus. Build exact output
   paths and validate source links and every legacy anchor. Keep mdBook deployment intact.
2. **HY-314 / HY-269:** implement the approved visual direction and shared navigation,
   section order, responsive TOC, themes, and keyboard behavior. HY-315 adds Pagefind
   after the static build, excluding archive and compatibility output.
3. **HY-270 / HY-274 / HY-276 / HY-277:** implement the agreed destinations, TUI split,
   changelog/archive, SDK responsibilities, real imagery and entry-page content.
4. **HY-316:** refresh the published baseline for changes since this snapshot; move
   sources/metadata and update tests/callers atomically; verify output and switch Pages.
   Document rollback to the last successful mdBook deployment before cutting over.

Cutover requires all source pages accounted for, all mapped destination files/anchors
present, base-path correctness, offline checkpoints passing, SDK/content tests passing,
and browser checks for desktop/mobile navigation, search, themes, diagrams, and keyboard
use. A clean build alone does not prove URL compatibility. No application runtime changes
are required. New MCP/troubleshooting material, missing settings, and chapters 6–10 are
independent follow-ups rather than migration gates.

## Implementation decisions

- The editorial layouts, archive URLs and final `site/src/content/` ownership are approved
  as the HY-313 foundation. Final product imagery is HY-277 work.
- Decide Mermaid build-time SVG versus conditional client rendering in HY-313 using the
  seven existing diagrams, including an accessible static fallback and both themes.

## Verification evidence

The baseline is commit `aed7ddda` (the full commit is recorded in `routes.json`).
`mdbook build book --dest-dir /tmp/wisp-hy312-book` completed with mdBook 0.5.4.
All 53 content-page URLs returned successfully from the public site and their 358
heading IDs, levels and labels matched the fresh local rendering. The inventory
contains 17 relocations: 16 release records and one SDK audit. Source coverage,
route uniqueness, and heading-ID uniqueness within each page were checked.
The combined print-page heading inventory comes from the fresh local build;
it was not independently compared with the public print page. Directory aliases
are planned from index files and existing README links; their HTTP behavior must
be checked at cutover.

The layout controls and Home, Documentation, and Course views were inspected in the
browser, including light/dark and the 390px simulated mobile content width. The
available preview pane was 657px wide; full desktop layout and a real mobile viewport
still require verification during implementation. Production search, Mermaid,
redirect behavior, and deployment have not been implemented or tested in this issue.
No Python or Rust application source changed, so application test suites were not run.
