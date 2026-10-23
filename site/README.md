# Wisp documentation

Astro builds the static documentation at `/Wisp/`, including Pagefind search. This is the only
active documentation build/deploy path; mdBook and its vendored theme/tooling have been removed.

## Develop and verify

Use Node **22.22.1** (`.nvmrc`) and npm **10.9.4**. From `site/`:

```sh
npm ci
npm run dev
# Open the printed URL including /Wisp/.
npm test
npm run check
npm run format:check
npm run build
npm exec playwright install chromium
npm run test:browser
npm run preview -- --port 4321
```

On Linux, install browser dependencies with `npm exec playwright install -- --with-deps chromium`.
Browser tests use built output on port 4322 and cover desktop/tablet/mobile, themes, keyboard focus,
no-JavaScript reading, redirects, images, diagrams, and the real search index. Failure traces go in
ignored `test-results/`. The existing Mermaid lazy-chunk size warning does not prevent a build.

From the repository root, also run `uv run pytest tests/docs tests/repository/test_images.py`.
These tests retain curriculum checkpoints, executable SDK examples, API/reference drift checks,
TUI ownership, and the pinned historical SDK audit. Source relocation does not change the examples.

## Authoring

- Markdown lives in `src/content/pages/`. Every file has strict YAML frontmatter,
  keeping metadata and prose together. The file path supplies its source identity. Required fields
  are `title`, `description`, `route`, `section`, `order`, `layout`, `listed`, and `searchable`.
  Optional fields include `navTitle`, `parent` (another source path), `chapter`, `checkpoint`, and
  `legacySource` (the former source identity for a moved page). See nearby pages for examples.
- `metadata.ts` validates complete source coverage, unique output paths and compatibility aliases,
  sibling ordering, and parent chains. `navigation.ts` supplies all navigation surfaces and indexes.
  There is no parallel content map or SUMMARY file.
- TUI Basics, Keys, Commands, and Sessions are separate Markdown files. The basics page retains
  visible links for moved fragments and a small progressive redirect; no content-splitting adapter
  remains. Edit links point to each actual source file.
- Release records live under `pages/maintainers/releases/`, excluded from search and normal
  navigation but discoverable through the archive. The SDK capability audit lives in
  `pages/architecture/`. Their old URLs and fragments still resolve.
- Root `CHANGELOG.md` is the only changelog source. The loader supplies its metadata and watches it;
  `changelog.md` is a collection identity, not another file. Historical changelog links using former
  source directories still resolve to current pages.
- `loader.ts` expands anchored `{{#include path:anchor}}` directives before rendering; full files and
  inclusive line ranges also work. Paths are relative to the physical Markdown file. Missing or
  ambiguous anchors and repository escapes fail. Edit runnable code in `examples/crafting_agents/`,
  never copy snippets into Markdown.
- `render.ts` preserves explicit `{#heading-id}` attributes and published IDs, resolves source links,
  and supports alerts, tables, highlighted code, and readable Mermaid fallbacks. Scripts enhance
  static reading; search loads only after a query and Mermaid only where diagrams occur.
- The homepage uses `Home.astro`. Static TUI masters are stored once in `assets/docs/`; Astro emits
  responsive WebP variants. See `assets/docs/README.md` for provenance and regeneration.
- `/print.html` combines the current corpus, namespaces repeated IDs, and restores the 377 final published
  combined-page anchors (including all 358 original IDs). It is excluded from search. Historical release records are clearly marked.

## Compatibility and deployment

`npm run build` runs the exact output checker after Astro and Pagefind. It verifies real canonical
and legacy files, fragments, base-prefixed links/assets, and print anchors—no extension fallback.
`migration/routes.json` retains the original HY-312 contract; `migration/cutover-routes.json` records
56 live mdBook pages and 377 headings observed immediately before cutover. Historical planning
artifacts in `migration/` are evidence, not current build instructions.

`.github/workflows/docs.yml` pins Node and Playwright, runs all site checks plus Python documentation
contracts and offline checkpoints, then uploads `site/dist` on main. Production build/deploy/smoke
runs are serialized; PR checks can cancel older PR runs. The post-deploy browser smoke waits for the
exact `GITHUB_SHA` stamped into the HTML before checking the public `/Wisp/` routes, legacy redirects,
themes, diagrams, and search. It can also be run manually:

```sh
WISP_DOCS_URL=https://whanyu1212.github.io/Wisp/ \
WISP_DOCS_REVISION=<deployed-commit-sha> npm run test:deployed
```

See [cutover procedure](migration/CUTOVER.md) for acceptance and recovery. No mdBook backup is kept.
HY-308 is still backlog; any future `just docs` target should call these Astro commands, not mdBook.
