import assert from "node:assert/strict";
import { test } from "node:test";
import { ancestors, navigation } from "../src/content/navigation.ts";
import { loadMetadata, type PageMetadata } from "../src/content/metadata.ts";

const root: PageMetadata = {
  source: "guide/index.md",
  title: "Guide",
  description: "Guide overview",
  route: "guide/index.html",
  section: "using",
  order: 0,
  layout: "docs",
  listed: true,
  searchable: true,
};

test("navigation projects sibling hierarchy once, without sorting children into roots", () => {
  const parent = {
    ...root,
    source: "guide/tui.md",
    route: "guide/tui.html",
    order: 1,
  };
  const child = {
    ...root,
    source: "guide/keys.md",
    route: "guide/keys.html",
    parent: parent.source,
  };
  const tail = {
    ...root,
    source: "guide/sdk.md",
    route: "guide/sdk.html",
    order: 2,
  };
  const pages = [child, tail, parent, root];
  const projected = navigation(pages)[0]!.pages;
  assert.deepEqual(
    projected.map((page) => [page.source, page.depth]),
    [
      [root.source, 0],
      [parent.source, 0],
      [child.source, 1],
      [tail.source, 0],
    ],
  );
  assert.deepEqual(ancestors(child, pages), [parent]);
  assert.deepEqual(ancestors(root, pages), []);
  // A hidden grouping entry promotes children without inventing a navigation link.
  assert.deepEqual(
    navigation([root, { ...parent, listed: false }, child])[0]!.pages.map(
      (page) => [page.source, page.depth],
    ),
    [
      [root.source, 0],
      [child.source, 0],
    ],
  );
});

test("the real collections expose the same section order, course sequence, and no archive links", () => {
  const metadata = loadMetadata();
  const groups = navigation(metadata);
  assert.deepEqual(
    groups.map((group) => group.key),
    ["using", "reference", "learn", "inside", "contributing"],
  );
  const pages = groups.flatMap((group) => group.pages);
  assert.equal(
    pages.length,
    metadata.filter((page) => page.listed && page.section !== "maintainers")
      .length,
  );
  assert.equal(new Set(pages.map((page) => page.source)).size, pages.length);
  const course = groups.find((group) => group.key === "learn")!.pages;
  assert.deepEqual(
    course.filter((page) => page.chapter).map((page) => page.chapter),
    [1, 2, 3, 4, 5],
  );
  assert.ok(course.slice(6).every((page) => !page.chapter));
});
