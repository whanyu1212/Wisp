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
    "Reference →",
    "Development →",
  ]);
});

test("homepage renders the v0.16.12 workspace stage with uncropped tabs and pause control", async ({
  page,
}) => {
  await page.goto("");
  const editingPanel = page.locator('.stage-panel[data-stage="editing"]');
  const hero = editingPanel.locator("img");
  await expect(hero).toHaveAttribute("width", "3404");
  await expect(hero).toHaveAttribute("height", "1946");
  await expect(hero).toHaveAttribute("alt", /live editing turn/);
  await expect(editingPanel).toBeVisible();
  const heroBox = (await hero.boundingBox())!;
  const mainBox = (await page.locator("main").boundingBox())!;
  expect(heroBox.width).toBeLessThanOrEqual(1080);
  if (page.viewportSize()!.width > 936) {
    expect(heroBox.x + heroBox.width / 2).toBeCloseTo(
      mainBox.x + mainBox.width / 2,
      0,
    );
  }
  // Ignore the one-pixel image border and allow responsive image rounding.
  expect(
    Math.abs(heroBox.height - 1 - (heroBox.width * 1946) / 3404),
  ).toBeLessThan(1.5);

  const toggle = page.locator(".stage-toggle");
  await expect(toggle).toBeVisible();
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await expect(toggle).toHaveText("Play");

  for (const [id, altPattern] of [
    ["files", /Files side panel/],
    ["browser", /Browser side panel/],
    ["permissions", /Project permissions panel/],
  ] as const) {
    await page.locator(`.stage-tab[data-stage="${id}"]`).click();
    await expect(page.locator(`#stage-${id}`)).toBeChecked();
    const panel = page.locator(`.stage-panel[data-stage="${id}"]`);
    await expect(panel).toBeVisible();
    await expect(panel.locator("img")).toHaveAttribute("width", "3404");
    await expect(panel.locator("img")).toHaveAttribute("height", "1946");
    await expect(panel.locator("img")).toHaveAttribute("alt", altPattern);
  }

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
      await expect(figures).toHaveCount(4);
      await expect(
        page.locator('.stage-panel[data-stage="editing"] img'),
      ).toHaveAttribute("alt", /live editing turn/);
      await expect(
        page.getByRole("heading", { name: "Inside a live session" }),
      ).toBeVisible();
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
    await expect(
      page.locator('.stage-panel[data-stage="editing"] .tui-capture img'),
    ).toBeVisible();
    await expect(page.locator(".stage-toggle")).toBeHidden();
    await page.locator('.stage-tab[data-stage="files"]').click();
    await expect(page.locator("#stage-files")).toBeChecked();
    await expect(
      page.locator('.stage-panel[data-stage="files"] .tui-capture img'),
    ).toBeVisible();
    await page
      .locator('.starting-points a[href="/Wisp/guide/tui.html"]')
      .click();
    await expect(page.locator("h1")).toHaveText("TUI basics");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  } finally {
    await context.close();
  }
});
