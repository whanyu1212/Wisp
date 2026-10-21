import { expect, test, type Page } from "@playwright/test";

const routes = [
  "",
  "guide/quickstart.html",
  "reference/sdk.html",
  "crafting-agents/01-core-loop.html",
  "crafting-agents/case-studies/tool-boundary.html",
];
const scheme = (page: Page) =>
  page.locator("html").evaluate((html) => getComputedStyle(html).colorScheme);
const noOverflow = async (page: Page) => {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
};

for (const route of routes) {
  test(`shared layout stays readable: ${route || "home"}`, async ({
    page,
  }, info) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    expect((await page.goto(route))?.status()).toBe(200);
    await expect(page.getByRole("combobox", { name: "Theme" })).toBeEnabled();
    await expect(page.getByRole("main")).toHaveCount(1);
    await expect(page.getByRole("banner")).toHaveCount(1);
    await expect(page.getByRole("contentinfo")).toHaveCount(1);
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
    for (const theme of ["light", "dark"]) {
      await page.getByRole("combobox", { name: "Theme" }).selectOption(theme);
      expect(await scheme(page)).toBe(theme);
      await noOverflow(page);
      if (info.project.name === "mobile") {
        await page.setViewportSize({ width: 320, height: 740 });
        await noOverflow(page);
      }
    }
    if (route) {
      await expect(
        page.getByRole("navigation", { name: "Breadcrumb", exact: true }),
      ).toBeVisible();
      if (info.project.name === "mobile") {
        await expect(page.locator(".sidebar")).toBeHidden();
        await expect(page.locator(".mobile-navigation")).toBeVisible();
      } else {
        await expect(
          page.locator('.sidebar a[aria-current="page"]'),
        ).toHaveCount(1);
      }
      if (route.endsWith("01-core-loop.html")) {
        await expect(page.locator(".page.course")).toHaveCount(1);
        await expect(
          page.getByRole("complementary", { name: "Runnable checkpoint" }),
        ).toContainText("checkpoint_01.py");
      }
    } else {
      await expect(page.locator(".sidebar")).toHaveCount(0);
    }
    expect(errors).toEqual([]);
  });
}

test("skip link, outline destination focus, and disclosure keyboard dismissal", async ({
  page,
}, info) => {
  await page.goto("guide/quickstart.html");
  await expect(page.getByRole("combobox", { name: "Theme" })).toBeEnabled();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "Skip to content" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();

  const mobile = info.project.name === "mobile";
  if (mobile) {
    const summary = page.locator(".mobile-navigation > summary");
    await summary.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".mobile-navigation")).toHaveAttribute(
      "open",
      "",
    );
    await page
      .getByRole("navigation", { name: "Mobile documentation" })
      .getByRole("link")
      .first()
      .focus();
    await page.keyboard.press("Escape");
    await expect(summary).toBeFocused();
    await expect(page.locator(".mobile-navigation")).not.toHaveAttribute(
      "open",
      "",
    );
    await summary.click();
    await page.locator('.mobile-navigation a[aria-current="page"]').click();
    await expect(page.locator(".mobile-navigation")).not.toHaveAttribute(
      "open",
      "",
    );
  }
  const outline = page.locator(
    info.project.name === "desktop" ? ".toc" : ".mobile-toc",
  );
  if (info.project.name !== "desktop") await outline.locator("summary").click();
  const link = outline.getByRole("link").first();
  const hash = await link.getAttribute("href");
  await link.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(`[id="${hash!.slice(1)}"]`)).toBeFocused();
  if (info.project.name !== "desktop")
    await expect(outline).not.toHaveAttribute("open", "");
});

test("previous and next stay within the section and resolve to real files", async ({
  page,
}) => {
  await page.goto("crafting-agents/01-core-loop.html");
  const navigation = page.getByRole("navigation", { name: "Adjacent pages" });
  await expect(navigation.locator('[rel="prev"]')).toHaveAttribute(
    "href",
    "/Wisp/crafting-agents/",
  );
  await expect(navigation.locator('[rel="next"]')).toHaveAttribute(
    "href",
    "/Wisp/crafting-agents/02-tools.html",
  );
  await navigation.locator('[rel="next"]').click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("2.");
  await expect(page.locator('.previous-next [rel="prev"]')).toHaveAttribute(
    "href",
    "/Wisp/crafting-agents/01-core-loop.html",
  );
  await page.goto("guide/index.html");
  await expect(page.locator('.previous-next [rel="prev"]')).toHaveCount(0);
});

test("system theme follows live changes; explicit overrides persist and can be reset", async ({
  page,
}) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("reference/sdk.html");
  const select = page.getByRole("combobox", { name: "Theme" });
  await expect(select).toBeEnabled();
  await expect(select).toHaveValue("system");
  expect(await scheme(page)).toBe("dark");
  await page.emulateMedia({ colorScheme: "light" });
  expect(await scheme(page)).toBe("light");
  await select.selectOption("dark");
  await page.reload();
  await expect(select).toHaveValue("dark");
  expect(await scheme(page)).toBe("dark");
  await page.goto("guide/quickstart.html");
  await expect(select).toHaveValue("dark");
  await select.selectOption("system");
  expect(
    await page.evaluate(() => localStorage.getItem("wisp-theme")),
  ).toBeNull();
  expect(await scheme(page)).toBe("light");
  await page.emulateMedia({ colorScheme: "dark" });
  expect(await scheme(page)).toBe("dark");
});

test("invalid storage and denied persistence do not break theme controls", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("wisp-theme", "invalid"));
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("guide/quickstart.html");
  const select = page.getByRole("combobox", { name: "Theme" });
  await expect(select).toHaveValue("system");
  expect(await scheme(page)).toBe("dark");
  await page.evaluate(() => {
    Storage.prototype.setItem = () => {
      throw new Error("Storage unavailable");
    };
    Storage.prototype.getItem = () => {
      throw new Error("Storage unavailable");
    };
    Storage.prototype.removeItem = () => {
      throw new Error("Storage unavailable");
    };
  });
  await select.selectOption("light");
  expect(await scheme(page)).toBe("light");
  await select.selectOption("system");
  expect(await scheme(page)).toBe("dark");
});

test("unavailable storage at startup falls back to the system theme", async ({
  page,
}) => {
  await page.addInitScript(() => {
    for (const name of ["getItem", "setItem", "removeItem"] as const) {
      Storage.prototype[name] = () => {
        throw new Error("Storage unavailable");
      };
    }
  });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("guide/quickstart.html");
  const select = page.getByRole("combobox", { name: "Theme" });
  await expect(select).toBeEnabled();
  await expect(select).toHaveValue("system");
  expect(await scheme(page)).toBe("dark");
  await select.selectOption("light");
  expect(await scheme(page)).toBe("light");
  await page.reload();
  expect(await scheme(page)).toBe("dark");
});

test("reading and native navigation work without JavaScript in system dark mode", async ({
  browser,
}, info) => {
  const context = await browser.newContext({
    javaScriptEnabled: false,
    colorScheme: "dark",
    viewport: info.project.use.viewport,
  });
  const page = await context.newPage();
  try {
    await page.goto(
      "http://127.0.0.1:4322/Wisp/crafting-agents/01-core-loop.html",
    );
    expect(await scheme(page)).toBe("dark");
    await expect(page.locator("article pre").first()).toBeVisible();
    await expect(page.locator(".diagram-source").first()).toHaveAttribute(
      "open",
      "",
    );
    await expect(page.getByRole("button", { name: "Copy code" })).toHaveCount(
      0,
    );
    if (info.project.name === "mobile") {
      await page.locator(".mobile-navigation > summary").click();
      const nav = page.getByRole("navigation", {
        name: "Mobile documentation",
      });
      await expect(
        nav.getByRole("link", { name: "Quickstart", exact: true }),
      ).toBeVisible();
      await nav.getByRole("link", { name: "Quickstart", exact: true }).click();
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(
        "Quickstart",
      );
    }
    await noOverflow(page);
  } finally {
    await context.close();
  }
});

test("wide code and tables scroll locally and copy reports success or failure", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("reference/sdk.html");
  const pre = page.locator("article pre").first();
  await expect(
    page.getByRole("button", { name: "Copy code" }).first(),
  ).toBeVisible();
  await pre.locator("code").evaluate((code) => {
    code.textContent = "long_example ".repeat(120);
  });
  await expect(pre).toHaveAttribute("tabindex", "0");
  expect(
    await pre.evaluate((element) => element.scrollWidth > element.clientWidth),
  ).toBe(true);
  await pre.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(() => pre.evaluate((element) => element.scrollLeft))
    .toBeGreaterThan(0);
  await page.getByRole("button", { name: "Copy code" }).first().click();
  await expect(page.getByRole("status").first()).toHaveText("Copied");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    "long_example ".repeat(120),
  );
  await page.evaluate(() => {
    navigator.clipboard.writeText = async () => {
      throw new Error("Clipboard denied");
    };
  });
  await page.getByRole("button", { name: "Copy code" }).first().click();
  await expect(page.getByRole("status").first()).toContainText(
    "Could not copy",
  );
  const table = page.locator(".table-scroll").first();
  await table.locator("table").evaluate((element) => {
    element.style.minWidth = "1800px";
  });
  await expect(table).toHaveAttribute("tabindex", "0");
  expect(
    await table.evaluate(
      (element) => element.scrollWidth > element.clientWidth,
    ),
  ).toBe(true);
  await noOverflow(page);
});

test("diagrams redraw in both themes and preserve their readable fallback", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "light" });
  await page.goto("crafting-agents/01-core-loop.html");
  const diagram = page.locator(".mermaid svg").first();
  await expect(diagram).toBeVisible();
  const nodeFill = () =>
    diagram
      .locator(".node rect")
      .first()
      .evaluate((rect) => getComputedStyle(rect).fill);
  const lightFill = await nodeFill();
  await page.getByRole("combobox", { name: "Theme" }).selectOption("dark");
  await expect.poll(nodeFill).not.toBe(lightFill);
  const darkFill = await nodeFill();
  await expect(page.locator(".diagram-source").first()).not.toHaveAttribute(
    "open",
    "",
  );
  // Rapid changes must not interleave Mermaid's shared renderer state.
  await page.getByRole("combobox", { name: "Theme" }).selectOption("light");
  await page.getByRole("combobox", { name: "Theme" }).selectOption("dark");
  await expect.poll(nodeFill).toBe(darkFill);
  await page.getByRole("combobox", { name: "Theme" }).selectOption("system");
  await expect.poll(nodeFill).toBe(lightFill);
  await page.emulateMedia({ colorScheme: "dark" });
  await expect.poll(nodeFill).toBe(darkFill);
  await page.locator(".diagram-source > summary").first().click();
  await expect(page.locator(".diagram-source pre").first()).toContainText(
    "flowchart TD",
  );
  await noOverflow(page);
});

test("all current diagrams retain legible dimensions in both themes", async ({
  page,
}) => {
  for (const [route, count] of [
    ["architecture/agent-runtime.html", 3],
    ["architecture/index.html", 1],
    ["architecture/rust-tui-boundary.html", 1],
    ["crafting-agents/01-core-loop.html", 1],
    ["crafting-agents/04-providers.html", 1],
  ] as const) {
    await page.goto(route);
    for (const theme of ["light", "dark"]) {
      await page.getByRole("combobox", { name: "Theme" }).selectOption(theme);
      await expect(page.locator(".mermaid svg")).toHaveCount(count);
      await expect
        .poll(() => page.locator(".diagram-source[open]").count())
        .toBe(0);
      expect(
        await page
          .locator(".mermaid svg")
          .evaluateAll((svgs) =>
            svgs.every(
              (svg) =>
                svg.getBoundingClientRect().width >=
                (svg as SVGSVGElement).viewBox.baseVal.width - 1,
            ),
          ),
      ).toBe(true);
      await noOverflow(page);
    }
  }
});

test("a failed diagram download leaves source and navigation usable", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const warning = page.waitForEvent("console", {
    predicate: (message) =>
      message.type() === "warning" &&
      message.text().includes("Could not load documentation diagrams"),
  });
  await page.route("**/mermaid.core.*.js", (route) => route.abort());
  await page.goto("crafting-agents/01-core-loop.html");
  await warning;
  await expect(page.locator(".diagram-source").first()).toHaveAttribute(
    "open",
    "",
  );
  await expect(page.locator(".diagram-source pre").first()).toContainText(
    "flowchart TD",
  );
  await expect(page.getByRole("combobox", { name: "Theme" })).toBeEnabled();
  expect(errors).toEqual([]);
});
