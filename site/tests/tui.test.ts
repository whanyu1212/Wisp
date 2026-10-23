import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { createMarkdownProcessor } from "@astrojs/markdown-remark";
import { load } from "cheerio";
import { loadMetadata, readPage } from "../src/content/metadata.ts";
import { baselinePath } from "../src/content/paths.ts";
import { finishHtml, type LegacyPage } from "../src/content/render.ts";
const tuiSource = "guide/tui.md";
const pages = loadMetadata();
const tuiChildren = pages
  .filter((page) => page.parent === tuiSource)
  .sort((a, b) => a.order - b.order);
const renderer = await createMarkdownProcessor({ syntaxHighlight: false });
const baseline = JSON.parse(readFileSync(baselinePath, "utf8")) as {
  pages: LegacyPage[];
};
const legacy = baseline.pages.find(
  (page) => page.source === `book/src/${tuiSource}`,
)!;
const sources = [tuiSource, ...tuiChildren.map((page) => page.source)];
const outputs = await Promise.all(
  sources.map(async (source) => {
    const rendered = await renderer.render(readPage(source).body);
    return load(
      finishHtml(
        rendered.code,
        pages.find((page) => page.source === source)!,
        pages,
        source === tuiSource ? legacy : undefined,
      ).html,
      null,
      false,
    );
  }),
);

test("every old heading has content or a visible exact compatibility destination", () => {
  const basics = outputs[0]!;
  for (const heading of legacy.anchors) {
    assert.equal(basics(`[id="${heading.id}"]`).length, 1);
    const link = basics(`[id="${heading.id}"] a[data-tui-destination]`);
    if (link.length) {
      const destination = link.attr("href")!;
      const index = tuiChildren.findIndex((page) =>
        destination.startsWith(`/Wisp/${page.route}#`),
      );
      assert.ok(index >= 0);
      assert.equal(outputs[index + 1]!(`[id="${heading.id}"]`).length, 1);
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
