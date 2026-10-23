import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { loadMetadata } from "../../src/content/metadata";
import { href, repoRoot } from "../../src/content/paths";

const records = loadMetadata().filter((page) => page.layout === "archive");

test("homepage release notes open the root changelog with working upgrade links", async ({
  page,
}) => {
  await page.goto("");
  await expect(
    page
      .locator(".home-content")
      .getByRole("link", { name: "release notes", exact: true }),
  ).toHaveAttribute("href", "/Wisp/changelog.html");
  await page
    .getByRole("contentinfo")
    .getByRole("link", { name: "Changelog", exact: true })
    .click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Changelog");
  // Comparing every release heading to the authoritative file catches stale copies.
  const headings = readFileSync(join(repoRoot, "CHANGELOG.md"), "utf8")
    .split("\n")
    .filter((line) => line.startsWith("## "))
    .map((line) => line.slice(3));
  await expect(page.locator("article h2")).toHaveText(headings);
  await expect(
    page.getByRole("link", { name: "Edit this page", exact: true }),
  ).toHaveAttribute(
    "href",
    "https://github.com/whanyu1212/Wisp/edit/main/CHANGELOG.md",
  );
  await page
    .locator("article")
    .getByRole("link", { name: "0.3 upgrade guide", exact: true })
    .click();
  await expect(page).toHaveURL(/\/Wisp\/guide\/upgrading-to-0\.3.html$/);
});

test("dated changelog heading keeps its old anchor", async ({ page }) => {
  await page.goto("changelog.html#030--unreleased");
  const anchor = page.locator('article [id="030--unreleased"]');
  await expect(anchor).toHaveCount(1);
  await expect(anchor.locator("xpath=following::h2[1]")).toHaveText(
    "0.3.0 — 2026-10-22",
  );
});

test("contributing exposes every historical release through a non-indexed archive", async ({
  page,
}) => {
  await page.goto("contributing/index.html");
  await page
    .getByRole("link", { name: "release record archive", exact: true })
    .click();
  await expect(page).toHaveURL(/\/Wisp\/maintainers\/releases\/$/);
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
    "content",
    "noindex",
  );
  await expect(page.locator("[data-pagefind-body]")).toHaveCount(0);
  const urls = await page
    .locator("article li a")
    .evaluateAll((links) => links.map((link) => link.getAttribute("href")));
  expect(urls.sort()).toEqual(
    records.map((record) => href(record.route)).sort(),
  );
  for (const record of records) {
    await page.goto(href(record.route));
    await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
      "content",
      "noindex",
    );
    await expect(page.locator("[data-pagefind-body]")).toHaveCount(0);
    await expect(page.locator("article h1")).toHaveCount(1);
    await expect(
      page.locator(
        `.mobile-navigation a[href="${href(record.route)}"], .sidebar a[href="${href(record.route)}"]`,
      ),
    ).toHaveCount(0);
  }
});

test("old release URLs retain fragment redirects and an explicit no-JavaScript destination", async ({
  page,
  browser,
}) => {
  const record = records[0]!;
  await page.goto(href(record.route));
  const id = await page.locator("article h1").getAttribute("id");
  expect(record.legacySource).toMatch(/^contributing\//);
  const old = `/Wisp/${record.legacySource!.replace(/\.md$/, ".html")}#${id}`;
  await page.goto(old);
  await expect(page).toHaveURL(
    `http://127.0.0.1:4322${href(record.route)}#${id}`,
  );
  const context = await browser.newContext({ javaScriptEnabled: false });
  try {
    const fallback = await context.newPage();
    await fallback.goto(`http://127.0.0.1:4322${old}`);
    await expect(fallback.locator("#destination")).toHaveAttribute(
      "href",
      href(record.route),
    );
    await expect(
      fallback.locator(`a[href="${href(record.route)}#${id}"]`),
    ).toHaveCount(1);
    await expect(fallback.locator("[data-pagefind-body]")).toHaveCount(0);
  } finally {
    await context.close();
  }
});
