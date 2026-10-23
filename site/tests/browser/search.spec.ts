import { expect, test } from "@playwright/test";
import { loadMetadata, sections } from "../../src/content/metadata";
import { href } from "../../src/content/paths";

for (const [query, title, route, section] of [
  ["Quickstart", "Quickstart", "guide/quickstart.html", "Using Wisp"],
  [
    "InProcessOptions",
    "Python SDK reference",
    "reference/sdk.html",
    "Reference",
  ],
  [
    "smallest coding agent",
    "1. The smallest coding agent",
    "crafting-agents/01-core-loop.html",
    "Learn",
  ],
]) {
  test(`search finds and opens ${route} under the base path`, async ({
    page,
  }) => {
    const resources: string[] = [];
    const errors: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/pagefind/")) resources.push(request.url());
    });
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("crafting-agents/case-studies/tool-boundary.html");
    const opener = page.getByRole("button", {
      name: "Search documentation",
      exact: true,
    });
    await expect(opener).toBeVisible();
    expect(resources).toEqual([]);
    await opener.click();
    const dialog = page.getByRole("dialog", { name: "Search documentation" });
    const input = dialog.getByRole("searchbox");
    await expect(input).toBeFocused();
    expect(resources).toEqual([]);
    await input.fill(query!);
    const result = dialog.getByRole("link", { name: title!, exact: true });
    await expect(result).toBeVisible();
    await expect(result).toHaveAttribute(
      "href",
      `http://127.0.0.1:4322/Wisp/${route}`,
    );
    await expect(result.locator("..")).toContainText(section!);
    expect(resources.length).toBeGreaterThan(0);
    expect(
      resources.every((url) =>
        new URL(url).pathname.startsWith("/Wisp/pagefind/"),
      ),
    ).toBe(true);
    await result.focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(
      new RegExp(`/Wisp/${route!.replaceAll(".", "\\.")}$`),
    );
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(title!);
    expect(errors).toEqual([]);
  });
}

test("the built index contains exactly searchable canonical pages and excludes navigation", async ({
  page,
  request,
}) => {
  await page.goto("reference/sdk.html");
  const indexed = await page.evaluate(async () => {
    const bundle = "/Wisp/pagefind/pagefind.js";
    const api = await import(/* @vite-ignore */ bundle);
    await api.options({ baseUrl: "/Wisp/" });
    const response: {
      results: {
        data(): Promise<{
          url: string;
          content: string;
          meta: { title: string; section: string };
        }>;
      }[];
    } = await api.search(null);
    return await Promise.all(response.results.map((result) => result.data()));
  });
  const expected = loadMetadata().filter((page) => page.searchable);
  expect(
    indexed
      .map((result) => new URL(result.url, "http://localhost").pathname)
      .sort(),
  ).toEqual(expected.map((page) => href(page.route)).sort());
  for (const result of indexed) {
    const pathname = new URL(result.url, "http://localhost").pathname;
    const metadata = expected.find((page) => href(page.route) === pathname)!;
    expect(result.meta.title).toBe(metadata.title);
    expect(result.meta.section).toBe(sections[metadata.section]);
    expect(result.content).not.toMatch(
      /Skip to content|Browse documentation|On this page/,
    );
    expect((await request.get(result.url)).status()).toBe(200);
  }
});

test("keyboard dismissal, focus containment, pagination, empty state, and both themes", async ({
  page,
}, info) => {
  await page.goto("guide/quickstart.html");
  const opener = page.getByRole("button", {
    name: "Search documentation",
    exact: true,
  });
  const dialog = page.getByRole("dialog", { name: "Search documentation" });
  for (const theme of ["light", "dark"]) {
    await page.getByRole("combobox", { name: "Theme" }).selectOption(theme);
    await opener.focus();
    await page.keyboard.press("Enter");
    const input = dialog.getByRole("searchbox");
    await expect(input).toBeFocused();
    await input.fill("wisp");
    await expect(dialog.getByRole("status")).toContainText("Showing 8 of");
    await expect(dialog.getByRole("listitem")).toHaveCount(8);
    const more = dialog.getByRole("button", { name: "Load more results" });
    await more.click();
    await expect(dialog.getByRole("listitem")).toHaveCount(16);
    await expect(dialog.getByRole("status")).toContainText("Showing 16 of");
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press("Tab");
      expect(
        await dialog.evaluate((element) =>
          element.contains(document.activeElement),
        ),
      ).toBe(true);
    }
    await input.fill("zzzznoresultwispzzzz");
    await expect(dialog.getByRole("status")).toContainText("No results");
    await expect(dialog.getByRole("listitem")).toHaveCount(0);
    await expect(more).toBeHidden();
    await input.fill("");
    await expect(dialog.getByRole("status")).toHaveText(
      "Type to search the documentation.",
    );
    if (info.project.name === "mobile")
      await page.setViewportSize({ width: 320, height: 740 });
    expect(
      await dialog.evaluate(
        (element) => element.scrollWidth <= element.clientWidth,
      ),
    ).toBe(true);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(opener).toBeFocused();
    await opener.click();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect(opener).toBeFocused();
  }
});

test("search reports loading and ignores a stale query after clearing, closing, and reopening", async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let reached!: () => void;
  const requested = new Promise<void>((resolve) => {
    reached = resolve;
  });
  await page.route("**/pagefind/pagefind.js", async (route) => {
    reached();
    await gate;
    await route.continue();
  });
  await page.goto("reference/sdk.html");
  const opener = page.getByRole("button", {
    name: "Search documentation",
    exact: true,
  });
  await opener.click();
  const dialog = page.getByRole("dialog");
  const input = dialog.getByRole("searchbox");
  await input.fill("Quickstart");
  await requested;
  await expect(dialog.getByRole("status")).toHaveText(
    "Loading search results…",
  );
  await input.fill("");
  await page.keyboard.press("Escape");
  await opener.click();
  await input.fill("InProcessOptions");
  release();
  await expect(
    dialog.getByRole("link", { name: "Python SDK reference", exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("link", { name: "Quickstart", exact: true }),
  ).toHaveCount(0);
  await expect(dialog.getByRole("status")).toContainText("InProcessOptions");
});

for (const failure of [
  "pagefind.js",
  "pagefind-entry.json*",
  "*.pf_meta",
  "wasm.*.pagefind",
  "index/*.pf_index",
  "fragment/*.pf_fragment",
]) {
  test(`search failure (${failure}) leaves reading and navigation usable`, async ({
    page,
  }) => {
    await page.route(`**/pagefind/${failure}`, (route) => route.abort());
    await page.goto("guide/quickstart.html");
    const opener = page.getByRole("button", {
      name: "Search documentation",
      exact: true,
    });
    await opener.click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("searchbox").fill("Quickstart");
    await expect(dialog.getByRole("status")).toContainText(
      "Search is unavailable",
    );
    await page.keyboard.press("Escape");
    await expect(opener).toBeFocused();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(
      "Quickstart",
    );
    await page.locator('.previous-next [rel="next"]').click();
    await expect(page.getByRole("heading", { level: 1 })).not.toHaveText(
      "Quickstart",
    );
  });
}

test("search is not a dead control when JavaScript is disabled", async ({
  browser,
}, info) => {
  const context = await browser.newContext({
    javaScriptEnabled: false,
    viewport: info.project.use.viewport,
  });
  try {
    const page = await context.newPage();
    await page.goto("http://127.0.0.1:4322/Wisp/reference/sdk.html");
    await expect(
      page.getByRole("button", { name: "Search documentation", exact: true }),
    ).toBeHidden();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(
      "Python SDK reference",
    );
    await page.locator('.previous-next [rel="next"]').click();
    await expect(page.getByRole("heading", { level: 1 })).not.toHaveText(
      "Python SDK reference",
    );
  } finally {
    await context.close();
  }
});
