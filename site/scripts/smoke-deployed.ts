// Run against the deployed artifact, not an Astro development server.
import { chromium, expect } from "@playwright/test";
import { load } from "cheerio";

const base = new URL(
  process.env.WISP_DOCS_URL ?? "https://whanyu1212.github.io/Wisp/",
);
const revision = process.env.WISP_DOCS_REVISION;
if (
  !revision ||
  base.pathname !== "/Wisp/" ||
  !["https:", "http:"].includes(base.protocol)
)
  throw new Error(
    "Set WISP_DOCS_REVISION and a WISP_DOCS_URL ending in /Wisp/",
  );

// Pages/CDN propagation is asynchronous. Never mistake the previous deployment for success.
const deadline = Date.now() + 300_000;
let observed = "unavailable";
while (true) {
  try {
    const response = await fetch(
      new URL(`?revision=${encodeURIComponent(revision)}`, base),
      { signal: AbortSignal.timeout(15_000), cache: "no-store" },
    );
    const $ = load(await response.text());
    observed =
      $('meta[name="wisp-docs-revision"]').attr("content") ??
      `HTTP ${response.status}, no Astro revision`;
    if (response.ok && observed === revision) break;
  } catch (error) {
    observed = String(error);
  }
  if (Date.now() >= deadline)
    throw new Error(`Expected deployed ${revision}; observed ${observed}`);
  await new Promise((resolve) => setTimeout(resolve, 5000));
}

const browser = await chromium.launch();
try {
  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    const page = await browser.newPage({ viewport });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    for (const route of [
      "",
      "guide/",
      "reference/",
      "contributing/",
      "print.html",
      "guide/quickstart.html",
      "architecture/sdk-capability-audit.html",
      "maintainers/releases/",
    ]) {
      const response = await page.goto(new URL(route, base).href);
      expect(response?.status(), route).toBe(200);
      expect(await page.locator("main h1").count(), route).toBeGreaterThan(0);
    }
    for (const [old, destination, id] of [
      [
        "guide/tui.html#rust-keybinding-preferences",
        "guide/tui/keybindings.html",
        "keybinding-preferences",
      ],
      [
        "reference/sdk-capability-audit.html#sdk-capability-audit",
        "architecture/sdk-capability-audit.html",
        "sdk-capability-audit",
      ],
      [
        "contributing/0.2.4-release.html",
        "maintainers/releases/0.2.4-release.html",
        "",
      ],
    ]) {
      await page.goto(new URL(old!, base).href);
      await expect(page).toHaveURL(
        new URL(destination! + (id ? `#${id}` : ""), base).href,
      );
      if (id) await expect(page.locator(`[id="${id}"]`)).toHaveCount(1);
    }
    await page.goto(new URL("crafting-agents/01-core-loop.html", base).href);
    for (const theme of ["light", "dark"]) {
      await page.getByRole("combobox", { name: "Theme" }).selectOption(theme);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(page.locator(".mermaid svg").first()).toBeVisible();
    }
    await page
      .getByRole("button", { name: "Search documentation", exact: true })
      .click();
    const dialog = page.getByRole("dialog", { name: "Search documentation" });
    await dialog.getByRole("searchbox").fill("InProcessOptions");
    await dialog
      .getByRole("link", { name: "Python SDK reference", exact: true })
      .click();
    await expect(page).toHaveURL(new URL("reference/sdk.html", base).href);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(errors).toEqual([]);
    await page.close();
  }
  console.log(`Deployed smoke passed: ${base.href} revision ${revision}`);
} finally {
  await browser.close();
}
