# Wisp mdBook migration plan

Date: 2026-09-16
POC: Wisp maintainers
Status: Implemented

## TL;DR

Replace VitePress with mdBook 0.5.4 while keeping the existing `site/` Markdown tree and public URL paths. Remove the Node, npm, Vue, VitePress, and TypeScript configuration. Use mdBook's default theme with light branding and `mdbook-mermaid` 0.17.1 for the six existing diagrams. No Wisp-authored TypeScript or JavaScript remains.

Keep the renderer migration separate from the educational rewrite. Once the migrated site is stable, add a book section called "Build a coding agent" that teaches the general design and links each idea to Wisp's implementation.

## Goals

- Make the published documentation read as one ordered book.
- Remove the frontend application toolchain and its package lock.
- Preserve existing documentation content, links, and public routes during the renderer change.
- Keep Mermaid diagrams, search, edit links, dark mode, and GitHub Pages deployment.
- Make local authoring and CI use pinned Rust documentation tools.
- Create a clear place for educational material about coding agents and Wisp's design.

## Non-goals

- Do not rewrite the existing guide, reference, architecture, or contributor content during the renderer migration.
- Do not create a custom mdBook theme in the first migration.
- Do not add documentation versioning, localization, analytics, or interactive components.
- Do not publish the implementation plans and audits currently under `docs/`.
- Do not change application code or product behavior.

## Confirmed baseline

The current site has:

- 29 Markdown pages and about 4,000 lines of content.
- 28 files with VitePress front matter.
- 8 VitePress callouts, represented by 16 opening and closing markers.
- 6 Mermaid diagrams.
- 4 public image/icon assets.
- 82 relative internal links.
- A production VitePress build that passes in about 6.8 seconds.
- Two tests that inspect VitePress navigation configuration directly.

Both extensionless and `.html` forms of an existing page currently resolve on GitHub Pages. mdBook will generate the same underlying `guide/installation.html` style of file, so keeping the current source paths should preserve published routes.

## Target layout

```text
book.toml
site/
  SUMMARY.md
  index.md
  logo.png
  hero.png
  favicon.ico
  apple-touch-icon.png
  guide/
  reference/
  architecture/
  contributing/
mermaid.min.js            # generated vendor asset
mermaid-init.js           # generated vendor asset
theme/
  favicon.png             # Wisp favicon override
book/                     # generated and ignored
```

`book.toml` will set `site/` as the source directory and `book/` as the output directory. Keeping the Markdown files where they are avoids a broad move, preserves repository links, and retains the current URL layout.

`mermaid.min.js` and `mermaid-init.js` are generated upstream assets installed by `mdbook-mermaid`. Wisp will not contain hand-written TypeScript or JavaScript. The two files are committed so local and deployed rendering use the same Mermaid runtime; updating them means rerunning the pinned plugin installer rather than editing them.

## Proposed book structure

The initial `site/SUMMARY.md` will order the current material without duplicating pages:

```text
Wisp

Using Wisp
  Introduction
  Installation
  Quickstart
  Upgrading
  Staying in sync
  Interfaces
  Python SDK
  Providers and auth
  Tools and safety
  Sessions
  Context and compaction
  Agent skills
  TUI

Reference
  Overview
  CLI
  Python SDK
  SDK capability audit
  Project file discovery
  Compatibility and versioning
  Configuration
  Environment variables

Inside Wisp
  Architecture overview
  Agent runtime
  Rust terminal frontend boundary

Contributing
  Overview
  Development setup
  Testing
  RC2 release
```

The home page becomes the unnumbered introduction to the book. Part headings in `SUMMARY.md` replace the four VitePress navigation and sidebar configurations. Chapter numbering will be disabled initially because reference and contributor pages are not sequential lessons.

After migration, add a new first-class part before "Using Wisp":

```text
Build a coding agent
  What the harness must do
  A minimal provider and tool loop
  Typed messages and events
  Tool execution and approvals
  Transcripts and sessions
  Steering, queues, and cancellation
  Context budgets and compaction
  Provider lifecycle differences
  CLI, RPC, SDK, and TUI adapters
  Reliability and security testing
  How Wisp implements the design
  Build a minimal agent
```

Each educational chapter should explain the general design first and then link to the relevant Wisp architecture chapter, source entry point, and tests. It should not copy reference material or implementation plans.

## Phase 1: Add mdBook alongside VitePress

Purpose: prove that the existing content builds before deleting the working site.

Changes:

1. Add `book.toml` pinned to the intended structure and default HTML renderer behavior.
2. Add `site/SUMMARY.md` with every published Markdown page exactly once.
3. Install the Mermaid assets with `mdbook-mermaid` 0.17.1 and configure its preprocessor.
4. Convert the 8 VitePress callouts to mdBook 0.5 native admonitions:
   - `::: info` to `> [!NOTE]`
   - `::: tip` to `> [!TIP]`
   - `::: warning` to `> [!WARNING]`
5. Remove ordinary `title` front matter from the 28 content pages because each page already has a level-one heading.
6. Replace the VitePress-only home-page front matter with a normal book introduction. Preserve the current positioning, install command, release notice, and links; accept a simpler page instead of recreating the marketing layout.
7. Move the four assets from `site/public/` to `site/` with `git mv` so mdBook copies them to the site root and existing asset URLs remain valid.
8. Build with both renderers during this phase. Do not deploy mdBook yet.

Acceptance:

- `mdbook build` succeeds with mdBook 0.5.4 and mdbook-mermaid 0.17.1.
- All 29 current pages appear in `book/`.
- The output route manifest matches the current VitePress route manifest.
- All six Mermaid blocks render in a browser.
- Search returns results from guide, reference, and architecture chapters.
- Every internal link passes an offline built-site link check.
- The current VitePress build still passes, giving a direct rollback path.

## Phase 2: Switch authoring, tests, and deployment

Purpose: make mdBook authoritative after the parallel build is verified.

Changes:

1. Replace `.github/workflows/docs.yml`:
   - remove Node setup and `npm ci`;
   - install pinned mdBook 0.5.4 and mdbook-mermaid 0.17.1 binaries;
   - run `mdbook build`;
   - run an offline link check against `book/`;
   - upload `book/` to GitHub Pages.
2. Keep the workflow's existing concurrency, pull-request build, Pages permissions, and deployment behavior.
3. Update `site/contributing/development.md` to document:
   - `mdbook serve --open`
   - `mdbook build`
   - the pinned mdBook and Mermaid plugin versions
   - how to refresh Mermaid's generated assets
4. Update tests that currently parse `site/.vitepress/config.ts`:
   - `tests/test_sdk_capability_audit.py`
   - `tests/test_compatibility_documentation.py`

   They will assert that the relevant pages are present in `site/SUMMARY.md`, while retaining their existing content-link assertions.
5. Update `.gitignore` to ignore `/book/` and remove VitePress build/cache entries.
6. Add a current changelog entry for the documentation renderer migration. Keep the historical entry saying VitePress was originally added.

Acceptance:

- The focused documentation tests pass.
- The full Python test suite passes because the changed tests are part of application CI.
- `uv run ruff format --check .`, `uv run ruff check .`, and `uv run mypy` pass.
- The documentation workflow succeeds on a pull request.
- The uploaded artifact contains the expected HTML, search index, icons, and Mermaid assets.
- A browser check covers desktop and mobile navigation, search, previous/next chapter controls, dark mode, admonitions, code blocks, and diagrams.

## Phase 3: Remove VitePress

Purpose: remove the old toolchain only after mdBook is the verified deployment path.

Delete:

- `site/.vitepress/config.ts`
- `package.json`
- `package-lock.json`

Also remove the now-empty `site/public/` directory. Do not change `src/wisp/agent/prompt/project_context.py`; `package.json` and `package-lock.json` remain valid generic project-context filenames even when Wisp itself no longer contains them.

Final checks:

- Search the repository for active VitePress commands and configuration references.
- Confirm remaining mentions are historical, such as the changelog.
- Build and link-check the book from a clean checkout with no `node_modules`.
- Compare all published route paths with the pre-migration manifest.
- Confirm `git status` contains only migration files and does not include unrelated work.

## Phase 4: Add the coding-agent curriculum

This is a separate content change after the renderer migration lands.

Start with three chapters:

1. A minimal provider and tool loop.
2. Tool execution, approvals, and trust boundaries.
3. Transcripts, sessions, and recovery.

These chapters test whether the proposed curriculum works before committing to the full outline. They should use Wisp as a case study without presenting Wisp-specific choices as universal requirements.

Acceptance for the pilot:

- A new reader can follow the chapters in order without reading the API reference.
- Each chapter has a concrete example and a "How Wisp does it" section.
- Links point to stable source modules and tests rather than local planning documents.
- Existing architecture pages remain the canonical description of Wisp internals.
- Reader or maintainer feedback supports expanding the remaining curriculum.

## Dependency and maintenance policy

Pin these tools together:

- mdBook 0.5.4
- mdbook-mermaid 0.17.1

`mdbook-mermaid` 0.17 targets mdBook 0.5's preprocessor API. Upgrade both in one dedicated change and rebuild the generated Mermaid assets at the same time.

Do not add a custom preprocessor, renderer, theme, JavaScript file, or CSS framework during migration. A small CSS file is acceptable only if the default theme makes Wisp branding or accessibility materially worse; browser verification should establish that need first.

## Risks and mitigations

### Route or anchor changes

File routes should remain stable because the source tree stays in place. Heading anchors may differ between Markdown renderers.

Mitigation: compare the generated route manifest, run an offline link checker over built HTML, and add mdBook redirects only for confirmed differences.

### Weaker landing page

The VitePress hero and feature cards will not carry over to the default mdBook theme.

Mitigation: rewrite the page as a concise book introduction. Do not maintain a custom theme to reproduce the current layout unless reader feedback shows that the landing page is hurting discovery.

### Mermaid adds JavaScript assets

mdBook itself cannot render Mermaid diagrams without an extension. The selected plugin installs a browser runtime and initialization file.

Mitigation: treat both as generated vendor assets. Do not hand-edit them. Pin the plugin and document the one refresh command.

### Broken links are less visible

The current VitePress build fails on dead internal links. mdBook's core build does not provide the same complete guarantee.

Mitigation: keep an offline link check as a required CI step against the rendered `book/` directory.

### One manual table of contents

`SUMMARY.md` becomes authoritative, so adding a page without adding a chapter can leave it unpublished.

Mitigation: update the navigation tests to inspect `SUMMARY.md` and include a CI assertion that every intended published Markdown file appears once.

### Migration mixed with educational rewriting

Changing the renderer and restructuring the content together would make regressions difficult to isolate.

Mitigation: finish phases 1 through 3 before adding curriculum chapters.

## Alternatives considered

### Run mdBook and VitePress as separate sites

Rejected because it keeps both toolchains, splits search and navigation, and works against content consolidation.

### Recreate the VitePress experience with a custom mdBook theme

Rejected for the initial migration because it replaces 160 lines of TypeScript configuration with a larger set of templates, CSS, and possibly JavaScript. The default theme is the reason to choose mdBook.

### Move all Markdown into a conventional `book/src/` tree

Rejected because the move adds churn without improving the published book. Configuring mdBook to use `site/` preserves repository links and URLs.

### Rewrite the existing documentation as a course during migration

Rejected because product reference and educational narrative have different jobs. The course should be added after the renderer is stable.

## Rollback

Keep VitePress deployable through phase 1. If route parity, Mermaid rendering, search, or browser usability fails, stop before phase 2 and remove the additive mdBook files.

After phase 2, rollback is a workflow-only change while the VitePress files still exist. Delete VitePress only in phase 3 after the mdBook deployment has been inspected on GitHub Pages.

## Approved decisions

1. Keep `site/` as the mdBook source directory.
2. Use the default mdBook theme and accept a simpler home page.
3. Keep Mermaid through pinned, generated vendor assets.
4. Preserve current URLs rather than reorganizing files around the future curriculum.
5. Land the renderer migration before writing the educational chapters.
