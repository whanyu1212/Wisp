# Astro cutover (HY-316)

## What changes

There is one active docs workflow: `.github/workflows/docs.yml`, building `site/dist` with Astro and
Pagefind. GitHub Pages still uses Actions and the same public `/Wisp/` base path. Markdown and
frontmatter live in `site/src/content/pages/`; runnable examples and root `CHANGELOG.md` retain their
single sources. All release records and pinned audit evidence remain. mdBook configuration, SUMMARY,
theme, vendored Mermaid scripts, and the duplicate Astro-only workflow are removed—not backed up.

The historical HY-312 plan and layout study in this directory are retained as design evidence.
Their old paths and migration status describe that snapshot, not the current build.

The final published print page has 377 IDs, including all 358 original IDs. mdBook had already
reassigned 50 numbered duplicate-heading IDs when chapters were reordered or added. For these
ambiguous names, the combined Astro view preserves the final published destination, not an earlier
chapter that once used the same number. No printed ID is dropped.

## Before merging

1. Run site unit/type/format/build/browser checks and Python documentation/repository tests.
   Inspect all 358 original anchors, the 377 heading IDs captured from the live pre-cutover site,
   canonical and compatibility routes, directory aliases, print anchors, images, and code includes.
2. Require terminal CI success and inspect automated review on the exact PR head.
3. Confirm the repository Pages source remains **GitHub Actions**. Do not manually deploy a PR
   artifact. The first merged main build is the cutover; PR verification alone is not deployment.

## After merging

1. Wait for the Docs workflow's build and Pages deployment on the merge commit.
2. Require **Verify deployed Astro site** to pass. It checks the stamped revision before desktop and
   mobile smoke checks, avoiding a false pass against a cached/previous deployment. It exercises
   normal routes, old TUI/audit/release URLs, search downloads/results, themes, and Mermaid.
3. Record the merge SHA, workflow/deployment URL, and smoke result in the issue or PR. Also open the
   public home, Quickstart, an old bookmark, and print view for a visual spot check. Do not close the
   migration based only on local builds. No deployment has been performed merely by preparing this
   change.

## Recovery without a backup copy

A failed build must not deploy. If post-deploy checks fail, inspect the specific failing route or
asset and fix forward through the same workflow. If production needs an immediate rollback, a
maintainer can explicitly authorize redeploying the last successful Pages artifact while it is
retained, or reverting the cutover commit through Git and deploying that revision. These are manual
recovery options, not a second active deployment path or a maintained mdBook copy. This task does
not authorize an automatic revert, merge, or deployment.

## Remaining work outside cutover

HY-271/272/273/278/279 are content follow-ups, not migration blockers. HY-308 remains backlog; its
future documentation target should use the commands in `site/README.md`.
