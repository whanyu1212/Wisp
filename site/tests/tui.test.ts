import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { createMarkdownProcessor } from "@astrojs/markdown-remark";
import { load } from "cheerio";
import { loadMetadata } from "../src/content/metadata.ts";
import { bookRoot, baselinePath } from "../src/content/paths.ts";
import { finishHtml, type LegacyPage } from "../src/content/render.ts";
import {
  prepareTui,
  splitTui,
  tuiChildren,
  tuiSource,
} from "../src/content/tui.ts";

const body = readFileSync(join(bookRoot, tuiSource), "utf8");
const pages = loadMetadata();
const renderer = await createMarkdownProcessor({ syntaxHighlight: false });
const rendered = await renderer.render(prepareTui(body));
const baseline = JSON.parse(readFileSync(baselinePath, "utf8")) as {
  pages: LegacyPage[];
};
const legacy = baseline.pages.find(
  (page) => page.source === `book/src/${tuiSource}`,
)!;
const original = finishHtml(
  rendered.code,
  pages.find((page) => page.source === tuiSource)!,
  pages,
  legacy,
);
const split = splitTui(original.html);
const sources = [tuiSource, ...tuiChildren.map((page) => page.source)];
const outputs = sources.map((source) => load(split(source).html, null, false));

test("TUI split preserves every original content block exactly once", () => {
  const before = load(original.html, null, false);
  // Headings are renamed and compatibility links are added; all prose, code,
  // and tables must survive without duplication or changes to their text.
  const blocks = (root: ReturnType<typeof load>) =>
    root("p, pre, table")
      .toArray()
      .map((node) => root(node).text());
  const remaining = outputs.flatMap(blocks);
  for (const text of blocks(before)) {
    const index = remaining.indexOf(text);
    assert.notEqual(index, -1, `Lost content: ${text.slice(0, 100)}`);
    remaining.splice(index, 1);
  }
  assert.deepEqual(remaining, [
    "Choose a focused guide for common terminal tasks:",
    "Previously linked sections now live on focused pages:",
  ]);
});

test("every old heading has content or a visible exact compatibility destination", () => {
  const basics = outputs[0]!;
  for (const heading of original.headings) {
    assert.equal(basics(`[id="${heading.slug}"]`).length, 1);
    const link = basics(`[id="${heading.slug}"] a[data-tui-destination]`);
    if (link.length) {
      const destination = link.attr("href")!;
      const index = tuiChildren.findIndex((page) =>
        destination.startsWith(`/Wisp/${page.route}#`),
      );
      assert.ok(index >= 0);
      assert.equal(outputs[index + 1]!(`[id="${heading.slug}"]`).length, 1);
    }
  }
  for (const output of outputs) assert.equal(output("h1").length, 1);
  assert.match(
    outputs[1]!("#keybinding-preferences").text(),
    /^keybinding preferences$/i,
  );
  assert.ok(outputs[2]!("#managing-mcp-servers").length);
  assert.ok(outputs[3]!("#prompt-history").length);
});

test("all planned TUI fragment destinations exist and moved links use them", () => {
  const basics = outputs[0]!;
  for (const heading of legacy.anchors) {
    const [route, id] = heading.destination.split("#");
    const index = sources.findIndex(
      (source) => `/Wisp/${source.replace(/\.md$/, ".html")}` === route,
    );
    assert.ok(index >= 0, heading.destination);
    assert.equal(
      outputs[index]!(`[id="${id}"]`).length,
      1,
      heading.destination,
    );
    if (index > 0)
      assert.equal(
        basics(`[id="${heading.id}"] a`).attr("href"),
        heading.destination,
      );
  }
});

test("source drift fails clearly rather than silently dropping a TUI section", () => {
  assert.throws(
    () =>
      prepareTui(
        body.replace(
          "Ctrl+G lists every resolved binding.",
          "Changed introduction.",
        ),
      ),
    /boundary/,
  );
  assert.throws(
    () => splitTui(original.html.replace('id="keybindings"', 'id="changed"')),
    /Missing TUI split boundary/,
  );
});
