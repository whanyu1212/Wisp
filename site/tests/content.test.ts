import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expandIncludes } from "../src/content/includes.ts";
import {
  validateMetadata,
  type PageMetadata,
} from "../src/content/metadata.ts";
import { finishHtml, resolveLink } from "../src/content/render.ts";
import { checkOutput } from "../src/content/check-output.ts";

const page: PageMetadata = {
  source: "guide/start.md",
  title: "Start",
  description: "Start using Wisp",
  route: "guide/start.html",
  section: "using",
  order: 0,
  layout: "docs",
  listed: true,
  searchable: true,
};
const index: PageMetadata = {
  ...page,
  source: "guide/index.md",
  route: "guide/index.html",
  order: 1,
};

test("metadata rejects missing/extra sources, duplicate routes and invalid navigation", () => {
  assert.throws(() => validateMetadata([], [page.source]), /Missing metadata/);
  assert.throws(() => validateMetadata([page], []), /Missing source/);
  assert.throws(
    () =>
      validateMetadata(
        [page, { ...index, route: page.route }],
        [page.source, index.source],
      ),
    /Duplicate route/,
  );
  assert.throws(() =>
    validateMetadata([{ ...page, route: "../escape.html" }], [page.source]),
  );
  assert.throws(() =>
    validateMetadata([{ ...page, title: "" }], [page.source]),
  );
  assert.throws(
    () => validateMetadata([{ ...page, parent: page.source }], [page.source]),
    /cycle/,
  );
  assert.throws(
    () => validateMetadata([{ ...page, parent: "unknown" }], [page.source]),
    /Invalid parent/,
  );
  assert.throws(
    () =>
      validateMetadata(
        [page, { ...index, order: 0 }],
        [page.source, index.source],
      ),
    /Duplicate navigation/,
  );
});

test("includes expand live anchored source and fail on missing anchors or escaped paths", () => {
  const root = mkdtempSync(join(tmpdir(), "wisp-includes-"));
  try {
    mkdirSync(join(root, "book"));
    writeFileSync(
      join(root, "example.py"),
      '# ANCHOR: loop\n    print("live")\n# ANCHOR_END: loop\n',
    );
    const source = join(root, "book/page.md");
    assert.equal(
      expandIncludes("{{#include ../example.py:loop}}", source, root),
      'print("live")',
    );
    assert.throws(
      () => expandIncludes("{{#include ../example.py:missing}}", source, root),
      /anchor/,
    );
    assert.throws(
      () => expandIncludes("{{#include ../example.py:9:2}}", source, root),
      /range/,
    );
    assert.throws(() =>
      expandIncludes("{{#include ../../does-not-exist}}", source, root),
    );
  } finally {
    rmSync(root, { recursive: true });
  }
});

test("source links preserve fragments and queries through route relocations and index aliases", () => {
  const moved = {
    ...page,
    source: "reference/audit.md",
    route: "architecture/audit.html",
  };
  assert.equal(
    resolveLink("../reference/audit#pinned", page.source, [page, index, moved]),
    "/Wisp/architecture/audit.html#pinned",
  );
  assert.equal(
    resolveLink("./index.md?from=test#start", page.source, [index]),
    "/Wisp/guide/?from=test#start",
  );
  assert.equal(
    resolveLink(
      "https://whanyu1212.github.io/Wisp/guide/start.html",
      page.source,
      [page],
    ),
    "/Wisp/guide/start.html",
  );
  assert.equal(
    resolveLink("https://example.com/docs", page.source, []),
    "https://example.com/docs",
  );
  assert.throws(
    () => resolveLink("./missing.md", page.source, [page]),
    /Unresolved/,
  );
});

test("rendering retains published heading IDs and readable diagram fallback", () => {
  const result = finishHtml(
    '<h1 id="new-slug">Start</h1><pre><code class="language-mermaid">graph TD\nA--&gt;B</code></pre><blockquote><p>[!TIP]\nTry this.</p></blockquote>',
    page,
    [page],
    {
      source: "book/src/guide/start.md",
      legacy_url: "/Wisp/guide/start.html",
      destination: "/Wisp/guide/start.html",
      aliases: [],
      anchors: [
        {
          id: "published-slug",
          level: 1,
          text: "Start",
          destination: "/Wisp/guide/start.html#published-slug",
        },
      ],
    },
  );
  assert.equal(result.headings[0]?.slug, "published-slug");
  assert.match(result.html, /class="diagram-source" open/);
  assert.match(result.html, /notice-tip/);
});

test("output checker rejects missing fragments/assets and extensionless fallback links", () => {
  const root = mkdtempSync(join(tmpdir(), "wisp-output-"));
  try {
    mkdirSync(join(root, "guide"));
    writeFileSync(
      join(root, page.route),
      '<h1 id="start">Start</h1><a href="/Wisp/guide/start.html#lost">Lost</a><img src="/Wisp/missing.png"><a href="/Wisp/guide/start">Fallback</a>',
    );
    const errors = checkOutput(root, [page], []);
    assert.equal(errors.length, 3);
    assert.ok(errors.some((error) => error.includes("missing fragment")));
    assert.ok(errors.some((error) => error.includes("missing.png")));
    assert.ok(
      errors.some((error) =>
        error.includes("missing output /Wisp/guide/start"),
      ),
    );
  } finally {
    rmSync(root, { recursive: true });
  }
});

test("mdBook explicit heading attributes become IDs, not visible text", () => {
  const result = finishHtml(
    '<h2 id="wrong">Rust TUI {#rust-tui-scaffold}</h2>',
    page,
    [page],
  );
  assert.equal(result.headings[0]?.slug, "rust-tui-scaffold");
  assert.equal(result.headings[0]?.text, "Rust TUI");
  assert.ok(!result.html.includes("{#"));
});

test("code and table scroll areas are keyboard accessible without JavaScript", () => {
  const result = finishHtml(
    '<h1 id="start">Start</h1><pre><code>long code</code></pre><table><caption>Options</caption><tr><td>value</td></tr></table><table><tr><td>other</td></tr></table>',
    page,
    [page],
  );
  assert.match(result.html, /<pre tabindex="0" aria-label="Code listing">/);
  assert.match(
    result.html,
    /class="table-scroll" tabindex="0" role="region" aria-label="Options"/,
  );
  assert.match(result.html, /aria-label="Table 2"/);
});

test("malformed includes fail even outside code fences", () => {
  assert.throws(
    () => expandIncludes("{{#include }}", "/tmp/page.md"),
    /Malformed/,
  );
  assert.throws(
    () => expandIncludes("{{#include missing file}}", "/tmp/page.md"),
    /Malformed/,
  );
});

test("metadata rejects reserved output and canonical/compatibility collisions", () => {
  assert.throws(
    () => validateMetadata([{ ...page, route: "404.html" }], [page.source]),
    /Reserved route/,
  );
  assert.throws(
    () =>
      validateMetadata(
        [
          { ...page, route: "guide/renamed.html" },
          { ...index, route: "guide/start.html" },
        ],
        [page.source, index.source],
      ),
    /Compatibility route collision/,
  );
});

test("output validation pins approved destinations and index aliases", () => {
  const root = mkdtempSync(join(tmpdir(), "wisp-compatibility-"));
  try {
    mkdirSync(join(root, "guide"));
    writeFileSync(join(root, page.route), '<h1 id="start">Start</h1>');
    const errors = checkOutput(
      root,
      [page],
      [
        {
          source: "book/src/" + page.source,
          legacy_url: "/Wisp/guide/start.html",
          destination: "/Wisp/guide/approved.html",
          aliases: ["/Wisp/guide/"],
          anchors: [
            {
              id: "start",
              level: 1,
              text: "Start",
              destination: "/Wisp/guide/approved.html#start",
            },
          ],
        },
      ],
    );
    assert.ok(
      errors.some((error) => error.includes("Changed planned destination")),
    );
    assert.ok(
      errors.some((error) => error.includes("Missing legacy index alias")),
    );
  } finally {
    rmSync(root, { recursive: true });
  }
});

test("actual Astro Markdown output supports Mermaid and dual-theme highlighting", async () => {
  const { createMarkdownProcessor } = await import("@astrojs/markdown-remark");
  const renderer = await createMarkdownProcessor({
    syntaxHighlight: { type: "shiki", excludeLangs: ["mermaid"] },
    shikiConfig: {
      themes: { light: "github-light-high-contrast", dark: "github-dark" },
    },
  });
  const result = await renderer.render(
    '# Start\n\n```mermaid\ngraph TD\nA-->B\n```\n\n```python\nprint("hello")\n```',
  );
  const finished = finishHtml(result.code, page, [page]);
  assert.match(finished.html, /class="mermaid"/);
  assert.match(finished.html, /Diagram source/);
  assert.match(finished.html, /--shiki-dark/);
});

test("frontmatter owns metadata and rejects missing, unknown, or spoofed source fields", async () => {
  const { readPage } = await import("../src/content/metadata.ts");
  const directory = mkdtempSync(join(tmpdir(), "wisp-frontmatter-"));
  try {
    const { source: _source, ...metadata } = page;
    const write = (data: unknown) =>
      writeFileSync(
        join(directory, "page.md"),
        `---\n${JSON.stringify(data)}\n---\n# Body\n`,
      );
    write(metadata);
    const parsed = readPage("page.md", directory);
    assert.equal(parsed.metadata.source, "page.md");
    assert.equal(parsed.body.trim(), "# Body");
    write({ ...metadata, source: "other.md" });
    assert.throws(() => readPage("page.md", directory));
    write({ ...metadata, title: undefined });
    assert.throws(() => readPage("page.md", directory));
    write({ ...metadata, legacySource: "../escape.md" });
    assert.throws(() => readPage("page.md", directory));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
