import { expect, test } from "@playwright/test";
import { loadMetadata } from "../../src/content/metadata";
import { navigation } from "../../src/content/navigation";
import { href } from "../../src/content/paths";

const groups = navigation(loadMetadata());

test("homepage offers install, product context, and preserved entry anchors without repeated banner content", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("");
  const brandLogo = page.locator(
    '.site-header .brand[aria-label="Wisp home"] img',
  );
  await expect(brandLogo).toBeVisible();
  await expect
    .poll(() =>
      brandLogo.evaluate(
        (img: HTMLImageElement) => img.complete && img.naturalWidth > 0,
      ),
    )
    .toBe(true);
  await expect(page.locator("h1")).toHaveCount(1);
  await expect(page.locator(".home-intro")).toContainText("coding agent");
  const installCommand =
    "uv tool install --python 3.12 wisp-ai\ncd path/to/project\nwisp";
  await expect(page.locator(".home-intro pre")).toHaveText(installCommand);
  await expect(page.locator(".install-note")).toContainText(
    "Python 3.11+ supported; 3.12 recommended for installation.",
  );
  await expect(page.locator(".home-intro .primary")).toHaveAttribute(
    "href",
    "/Wisp/guide/quickstart.html",
  );
  const copyButton = page
    .locator(".home-install")
    .getByRole("button", { name: "Copy code" });
  await expect(copyButton).toBeVisible();
  await copyButton.click();
  await expect(page.locator(".home-install [role='status']")).toHaveText(
    "Copied",
  );
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    installCommand,
  );
  await expect(page.locator('main img[src*="wisp-banner"]')).toHaveCount(0);
  for (const id of [
    "wisp",
    "choose-a-starting-point",
    "install-wisp",
    "start-reading",
  ]) {
    await expect(page.locator(`[id="${id}"]`)).toHaveCount(1);
  }
  await expect(page.locator(".secondary-starts a")).toHaveText([
    "Inside Wisp →",
    "Development →",
  ]);
});

test("homepage centers a reader-friendly hero showcase and renders uncropped workflow captures", async ({
  page,
}) => {
  await page.goto("");
  const hero = page.locator(".home-showcase img");
  await expect(hero).toHaveAttribute("width", "2592");
  await expect(hero).toHaveAttribute("height", "1502");
  await expect(hero).toHaveAttribute("alt", /prompt composer, and side panel/);
  const heroBox = (await hero.boundingBox())!;
  const mainBox = (await page.locator("main").boundingBox())!;
  expect(heroBox.width).toBeLessThanOrEqual(880);
  if (page.viewportSize()!.width > 936) {
    expect(heroBox.width).toBeLessThan(mainBox.width);
    expect(heroBox.x + heroBox.width / 2).toBeCloseTo(
      mainBox.x + mainBox.width / 2,
      0,
    );
  }
  // Ignore the one-pixel image border and allow responsive image rounding.
  expect(
    Math.abs(heroBox.height - 1 - (heroBox.width * 1502) / 2592),
  ).toBeLessThan(1);

  const workflowImages = page.locator(".workflow-rows .tui-capture img");
  await expect(workflowImages).toHaveCount(2);
  const approval = workflowImages.nth(1);
  await approval.scrollIntoViewIfNeeded();
  await expect(approval).toHaveAttribute("width", "2718");
  await expect(approval).toHaveAttribute("height", "1528");
  const approvalBox = (await approval.boundingBox())!;
  expect(
    Math.abs(approvalBox.height - 1 - (approvalBox.width * 1528) / 2718),
  ).toBeLessThan(1);
  await page.goto("guide/installation.html");
  await expect(page.locator("article")).toContainText(
    "Python 3.11+ is supported; Python 3.12 is recommended for installation.",
  );
});

test("section indexes list exactly the shared navigation destinations", async ({
  page,
}) => {
  for (const key of ["using", "reference", "contributing"]) {
    const group = groups.find((group) => group.key === key)!;
    const root = group.pages[0]!;
    await page.goto(href(root.route));
    const links = page.locator(".section-index li > a");
    expect(
      await links.evaluateAll((links) =>
        links.map((link) => link.getAttribute("href")),
      ),
    ).toEqual(group.pages.slice(1).map((entry) => href(entry.route)));
    await expect(page.locator(".section-index li p")).toHaveCount(
      group.pages.length - 1,
    );
    await expect(
      page.locator('.section-index a[href*="maintainers/"]'),
    ).toHaveCount(0);
  }
});

test("TUI captures load responsively with accurate provenance in both themes", async ({
  page,
}) => {
  for (const route of [
    "",
    "guide/quickstart.html",
    "guide/tui.html",
    "guide/tui/sessions.html",
    "guide/tui/commands.html",
  ]) {
    await page.goto(route);
    const figures = page.locator(".tui-capture");
    expect(await figures.count()).toBeGreaterThan(0);
    for (const figure of await figures.all()) {
      await figure.scrollIntoViewIfNeeded();
      const image = figure.locator("img");
      await expect(image).toHaveAttribute("alt", /Wisp/);
      // Real-session crops and synthetic masters retain their source dimensions.
      const sourceWidth = Number(await image.getAttribute("width"));
      expect(sourceWidth).toBeGreaterThanOrEqual(1780);
      // Astro caps responsive derivatives at the source width rather than upscaling.
      const srcset = (await image.getAttribute("srcset")) ?? "";
      expect(
        [...srcset.matchAll(/ (\d+)w/g)].map((match) => Number(match[1])),
      ).toEqual([480, 800, 1200, 1600, Math.min(2400, sourceWidth)]);
      await expect
        .poll(() =>
          image.evaluate(
            (img: HTMLImageElement) => img.complete && img.naturalWidth > 0,
          ),
        )
        .toBe(true);
      await expect(figure.locator("figcaption")).toContainText(
        route === "" ? "Real session screenshot" : "synthetic demo data",
      );
      const fullSize = await figure.locator("a").getAttribute("href");
      expect(fullSize).toMatch(/^\/Wisp\/_astro\//);
      const response = await page.request.get(fullSize!);
      expect(response.ok()).toBe(true);
      expect(response.headers()["content-type"]).toContain("image/png");
    }
    if (route === "") {
      await expect(figures).toHaveCount(3);
      await expect(page.locator(".home-showcase img")).toHaveAttribute(
        "alt",
        /welcome screen with the Braille mascot/,
      );
      await expect(
        page.getByRole("heading", { name: "Follow every edit" }),
      ).toBeVisible();
      await expect(page.getByText("Steer without starting over")).toHaveCount(
        0,
      );
    }
    for (const theme of ["light", "dark"]) {
      await page.getByRole("combobox", { name: "Theme" }).selectOption(theme);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
    }
  }
});

test("homepage and section navigation remain useful with JavaScript disabled", async ({
  browser,
}) => {
  const context = await browser.newContext({
    javaScriptEnabled: false,
    viewport: { width: 320, height: 740 },
    colorScheme: "dark",
    reducedMotion: "reduce",
  });
  try {
    const page = await context.newPage();
    await page.goto("http://127.0.0.1:4322/Wisp/");
    await expect(page.locator(".home-intro .primary")).toBeInViewport();
    await expect(page.locator(".home-intro pre")).toBeVisible();
    await expect(page.locator(".home-showcase .tui-capture img")).toBeVisible();
    await page.locator('.starting-points a[href="/Wisp/guide/"]').click();
    await page
      .locator('.section-index a[href="/Wisp/guide/quickstart.html"]')
      .click();
    await expect(page.locator("h1")).toHaveText("Quickstart");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  } finally {
    await context.close();
  }
});
