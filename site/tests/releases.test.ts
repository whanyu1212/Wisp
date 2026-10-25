import assert from "node:assert/strict";
import { test } from "node:test";
import { changelog, loadMetadata } from "../src/content/metadata.ts";
import { resolveLink } from "../src/content/render.ts";

const pages = loadMetadata();

test("changelog metadata is supplied once without a duplicate Markdown source", () => {
  assert.deepEqual(
    pages.filter((page) => page.source === changelog.source),
    [changelog],
  );
  assert.equal(changelog.searchable, true);
  assert.equal(changelog.listed, false);
  const archive = pages
    .filter((page) => page.layout === "archive")
    .sort((a, b) => a.order - b.order);
  assert.equal(archive[0]?.source, "maintainers/releases/0.4.0-release.md");
  assert.equal(archive.at(-1)?.source, "maintainers/releases/rc2-release.md");
  assert.ok(
    archive.findIndex((p) => p.source.endsWith("/rc13-release.md")) <
      archive.findIndex((p) => p.source.endsWith("/rc6-release.md")),
  );
});

test("root changelog links resolve both current and historical source paths", () => {
  for (const prefix of ["book/src/", "site/", "./book/src/"]) {
    assert.equal(
      resolveLink(
        `${prefix}guide/upgrading.md#compatibility`,
        changelog.source,
        pages,
      ),
      "/Wisp/guide/upgrading.html#compatibility",
    );
  }
  assert.equal(
    resolveLink(
      "https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md",
      "index.md",
      pages,
    ),
    "/Wisp/changelog.html",
  );
  assert.equal(
    resolveLink(
      "https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md#030--unreleased",
      "index.md",
      pages,
    ),
    "/Wisp/changelog.html#030--unreleased",
  );
  // Do not accidentally rewrite links to unrelated repositories or historical tags.
  const historical =
    "https://github.com/whanyu1212/Wisp/blob/v0.2.0/CHANGELOG.md";
  assert.equal(resolveLink(historical, "index.md", pages), historical);
});
