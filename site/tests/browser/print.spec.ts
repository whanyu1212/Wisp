import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { baselinePath, cutoverPath } from "../../src/content/paths";
import { loadMetadata } from "../../src/content/metadata";

const baseline = JSON.parse(readFileSync(baselinePath, "utf8"));
const cutover = JSON.parse(readFileSync(cutoverPath, "utf8"));
const anchors: string[] = [
  ...new Set<string>([
    ...baseline.utility_routes.find(
      (route: { legacy_url: string }) =>
        route.legacy_url === "/Wisp/print.html",
    ).heading_ids,
    ...cutover.print.heading_ids,
  ]),
];

test("combined print retains every original anchor and all current content without duplicate IDs", async ({
  page,
}) => {
  expect((await page.goto("print.html"))?.status()).toBe(200);
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
    "content",
    "noindex",
  );
  await expect(page.locator("[data-pagefind-body]")).toHaveCount(0);
  await expect(page.locator(".print-chapter")).toHaveCount(
    loadMetadata().length,
  );
  const ids = await page
    .locator("[id]")
    .evaluateAll((nodes) => nodes.map((node) => node.id));
  expect(new Set(ids).size).toBe(ids.length);
  for (const id of anchors) expect(ids).toContain(id);
  await page.getByRole("combobox", { name: "Theme" }).selectOption("dark");
  await page.emulateMedia({ media: "print" });
  await expect(page.locator(".print-document")).toHaveCSS(
    "color",
    "rgb(17, 17, 17)",
  );
  await expect(page.locator(".astro-code span").first()).toHaveCSS(
    "color",
    "rgb(17, 17, 17)",
  );
  await expect(page.locator(".site-header")).toBeHidden();
  await expect(page.locator(".sidebar")).toBeHidden();
  await expect(page.locator(".print-chapter").first()).toBeVisible();
});

test("combined print is readable with JavaScript disabled", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  try {
    const page = await context.newPage();
    await page.goto(
      "http://127.0.0.1:4322/Wisp/print.html#follow-the-implementation-1",
    );
    await expect(page.locator("#follow-the-implementation-1")).toHaveCount(1);
    await expect(page.locator(".diagram-source").first()).toBeVisible();
    await expect(
      page.getByText("Historical release records are included at the end", {
        exact: false,
      }),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});
