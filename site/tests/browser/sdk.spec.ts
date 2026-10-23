import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { baselinePath } from "../../src/content/paths";
import type { LegacyPage } from "../../src/content/render";

const baseline = JSON.parse(readFileSync(baselinePath, "utf8")) as {
  pages: LegacyPage[];
};
const audit = baseline.pages.find(
  (page) => page.source === "book/src/reference/sdk-capability-audit.md",
)!;

test("SDK tasks and API lookups link to each other without mixing in historical evidence", async ({
  page,
}) => {
  await page.goto("guide/sdk.html");
  await page
    .locator("article")
    .getByRole("link", { name: "Minimal offline prompt", exact: true })
    .click();
  await expect(page).toHaveURL(/guide\/sdk\.html#minimal-offline-prompt$/);
  await expect(page.locator("article")).toContainText(
    "uv run python -m examples.sdk.minimal",
  );
  await page
    .locator("article")
    .getByRole("link", { name: "Python SDK reference", exact: true })
    .click();
  await expect(page).toHaveURL(/reference\/sdk\.html$/);
  await page
    .locator("article table")
    .first()
    .getByRole("link", { name: "InProcessOptions", exact: true })
    .click();
  await expect(page).toHaveURL(/#inprocessoptions$/);
  await expect(page.locator("#inprocessoptions")).toBeVisible();
  await page
    .locator("article")
    .getByRole("link", { name: "Python SDK guide", exact: true })
    .click();
  await expect(page).toHaveURL(/guide\/sdk\.html$/);
  await page
    .locator("article")
    .getByRole("link", { name: "SDK capability audit", exact: true })
    .click();
  await expect(page).toHaveURL(/architecture\/sdk-capability-audit\.html$/);
  await expect(page.locator("article")).toContainText(
    "Historical design evidence",
  );
  await expect(page.locator("article")).toContainText(
    "914cf1472e715297caa30db4b9535d534a9eb718",
  );
  await expect(page.locator("article")).toContainText(
    "f98260a4d4cf98a4c8dbb4aaafcf813bb0bae567",
  );
  await page.goto("reference/index.html");
  await expect(
    page.locator('article a[href*="sdk-capability-audit"]'),
  ).toHaveCount(0);
  await page.goto("architecture/index.html");
  await expect(
    page.locator(
      'article a[href="/Wisp/architecture/sdk-capability-audit.html"]',
    ),
  ).toHaveCount(1);
});

test("every legacy SDK audit fragment redirects to its preserved Inside Wisp heading", async ({
  page,
}) => {
  for (const heading of audit.anchors) {
    await page.goto(`${audit.legacy_url}?from=legacy#${heading.id}`);
    await expect(page).toHaveURL(
      `http://127.0.0.1:4322${audit.destination}?from=legacy#${heading.id}`,
    );
    await expect(page.locator(`article [id="${heading.id}"]`)).toBeVisible();
  }
  await expect(
    page.getByRole("link", { name: "Edit this page", exact: true }),
  ).toHaveAttribute(
    "href",
    "https://github.com/whanyu1212/Wisp/edit/main/site/src/content/pages/architecture/sdk-capability-audit.md",
  );
});

test("legacy SDK audit has usable fragment fallbacks without JavaScript", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  try {
    const page = await context.newPage();
    await page.goto(
      `http://127.0.0.1:4322${audit.legacy_url}#pinned-reference`,
    );
    await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
      "content",
      "noindex",
    );
    await expect(page.locator("[data-pagefind-body]")).toHaveCount(0);
    for (const heading of audit.anchors) {
      await expect(page.locator(`main [id="${heading.id}"] a`)).toHaveAttribute(
        "href",
        `${audit.destination}#${heading.id}`,
      );
    }
    await page.locator("#pinned-reference a").click();
    await expect(page.locator("article #pinned-reference")).toBeVisible();
  } finally {
    await context.close();
  }
});
