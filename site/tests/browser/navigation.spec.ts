import { expect, test } from "@playwright/test";
import { loadMetadata } from "../../src/content/metadata";
import { navigation } from "../../src/content/navigation";
import { href } from "../../src/content/paths";

const groups = navigation(loadMetadata());
const titles = [
  "Using Wisp",
  "Reference",
  "Learn",
  "Inside Wisp",
  "Contributing",
];

test("homepage, desktop, and mobile share user-first section destinations", async ({
  page,
}, info) => {
  await page.goto("");
  await expect(page.locator('meta[name="description"]')).toHaveAttribute(
    "content",
    /^Install and use Wisp/,
  );
  const primary = page.getByRole("navigation", {
    name: "Primary",
    exact: true,
    includeHidden: true,
  });
  await expect(primary.getByRole("link", { includeHidden: true })).toHaveText(
    titles,
  );
  const mobile = page.getByRole("navigation", {
    name: "Mobile documentation",
    includeHidden: true,
  });
  await expect(mobile.locator("section > strong")).toHaveText(titles);
  for (const [index, group] of groups.entries()) {
    const destination = href(group.pages[0]!.route);
    await expect(primary.locator("a").nth(index)).toHaveAttribute(
      "href",
      destination,
    );
    await expect(
      mobile.locator("section").nth(index).locator("a").first(),
    ).toHaveAttribute("href", destination);
    if (index < 3) {
      const card = page.locator(".starting-points a").nth(index);
      await expect(card).toHaveAttribute("href", destination);
      await expect(card).toContainText(group.title);
    }
  }
  await expect(page.locator(".starting-points a")).toHaveCount(3);
  if (info.project.name === "mobile") {
    await page.locator(".mobile-navigation > summary").click();
    await mobile.getByRole("link", { name: "Using Wisp", exact: true }).click();
  } else {
    await primary
      .getByRole("link", { name: "Using Wisp", exact: true })
      .click();
  }
  await expect(page).toHaveURL(/\/Wisp\/guide\/$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Introduction",
  );
});

test("every listed page has matching sidebar/mobile order and section-local previous/next links", async ({
  page,
}) => {
  for (const group of groups) {
    for (const [index, entry] of group.pages.entries()) {
      const response = await page.goto(href(entry.route));
      expect(response?.status()).toBe(200);
      const sidebar = page.locator(".sidebar nav a");
      const mobile = page
        .locator(".mobile-navigation section")
        .nth(groups.indexOf(group))
        .locator("a");
      const expected = group.pages.map((page) => href(page.route));
      expect(
        await sidebar.evaluateAll((links) =>
          links.map((link) => link.getAttribute("href")),
        ),
      ).toEqual(expected);
      expect(
        await mobile.evaluateAll((links) =>
          links.map((link) => link.getAttribute("href")),
        ),
      ).toEqual(expected);
      await expect(
        page.locator('.sidebar a[aria-current="page"]'),
      ).toHaveAttribute("href", href(entry.route));
      for (const [relation, adjacent] of [
        ["prev", group.pages[index - 1]],
        ["next", group.pages[index + 1]],
      ] as const) {
        const link = page.locator(`.previous-next [rel="${relation}"]`);
        if (adjacent)
          await expect(link).toHaveAttribute("href", href(adjacent.route));
        else await expect(link).toHaveCount(0);
      }
    }
  }
});
