import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "cheerio";
import { combinePrint } from "../src/content/print.ts";
import type { PageMetadata } from "../src/content/metadata.ts";

const page: PageMetadata = {
  source: "guide/a.md",
  route: "guide/a.html",
  title: "A",
  description: "A guide",
  section: "using",
  order: 0,
  layout: "docs",
  listed: true,
  searchable: true,
};

test("combined print namespaces repeated headings and links while retaining legacy targets", () => {
  const html = combinePrint(
    [
      {
        page,
        html: '<h1 id="intro">A</h1><a href="#intro">Here</a><a href="/Wisp/guide/b.html#intro">Next</a><section aria-labelledby="intro">Body</section>',
      },
      {
        page: { ...page, source: "guide/b.md", route: "guide/b.html" },
        html: '<h1 id="intro">B</h1>',
      },
    ],
    {
      intro: "/Wisp/guide/a.html#intro",
      "intro-1": "/Wisp/guide/b.html#intro",
    },
  );
  const $ = load(html);
  const ids = $("[id]")
    .toArray()
    .map((node) => $(node).attr("id"));
  assert.equal(ids.length, new Set(ids).size);
  assert.equal($("#intro").next("h1").text(), "A");
  assert.equal($("#intro-1").next("h1").text(), "B");
  for (const link of $("a").toArray())
    assert.equal($(`[id="${$(link).attr("href")!.slice(1)}"]`).length, 1);
  assert.equal(
    $("[aria-labelledby]").attr("aria-labelledby"),
    "page-guide-a-html--intro",
  );
});

test("combined print refuses to lose a historical anchor's destination", () => {
  assert.throws(
    () =>
      combinePrint([{ page, html: '<h1 id="intro">A</h1>' }], {
        lost: "/Wisp/guide/a.html#missing",
      }),
    /Missing print destination/,
  );
});

test("print preserves explicit index.html anchors as well as directory aliases", () => {
  const html = combinePrint(
    [
      {
        page: { ...page, source: "index.md", route: "index.html" },
        html: '<h1 id="intro">Home</h1><a href="/Wisp/#intro">Directory</a>',
      },
    ],
    { intro: "/Wisp/index.html#intro" },
  );
  const $ = load(html);
  assert.equal($("#intro").next("h1").text(), "Home");
  assert.equal($("a").attr("href"), "#page-index-html--intro");
});
