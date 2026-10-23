import { expect, test } from "@playwright/test";

const moved = [
  ["rust-keybinding-preferences", "keybindings"],
  ["keybindings", "keybindings"],
  ["managing-mcp-servers", "commands"],
  ["slash-commands", "commands"],
  ["context-skills-and-mcp-browsers", "commands"],
  ["resuming-long-sessions", "sessions"],
  ["prompt-history", "sessions"],
];

test("TUI basics links to focused, searchable children with correct source editing", async ({
  page,
}) => {
  await page.goto("guide/tui.html");
  await expect(page.locator("article h1")).toHaveText("TUI basics");
  for (const part of ["keybindings", "commands", "sessions"]) {
    await page.goto("guide/tui.html");
    await page
      .locator(`article a[href="/Wisp/guide/tui/${part}.html"]`)
      .first()
      .click();
    await expect(page).toHaveURL(new RegExp(`/guide/tui/${part}\\.html$`));
    await expect(page.locator("article h1")).toHaveCount(1);
    await expect(page.locator("[data-pagefind-body]")).toHaveCount(1);
    await expect(
      page.getByRole("link", { name: "Edit this page", exact: true }),
    ).toHaveAttribute(
      "href",
      "https://github.com/whanyu1212/Wisp/edit/main/book/src/guide/tui.md",
    );
  }
});

test("moved fragments redirect exactly on load and hashchange, retaining queries", async ({
  page,
}) => {
  for (const [id, part] of moved) {
    await page.goto(`guide/tui.html?from=legacy#${id}`);
    await expect(page).toHaveURL(
      new RegExp(`/guide/tui/${part}\\.html\\?from=legacy#${id}$`),
    );
    await expect(page.locator(`article #${id}`)).toBeVisible();
  }
  await page.goto("guide/tui.html");
  await page.evaluate(() => {
    location.hash = "prompt-history";
  });
  await expect(page).toHaveURL(/\/tui\/sessions\.html#prompt-history$/);
  for (const id of [
    "rust-large-pastes",
    "steering-and-follow-ups",
    "unknown",
  ]) {
    await page.goto(`guide/tui.html#${id}`);
    await expect(page).toHaveURL(new RegExp(`/guide/tui\\.html#${id}$`));
  }
});

test("old TUI fragments have visible destinations without JavaScript", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  try {
    const page = await context.newPage();
    await page.goto("http://127.0.0.1:4322/Wisp/guide/tui.html#keybindings");
    for (const [id, part] of moved) {
      await expect(
        page.locator(`[id="${id}"] a[data-tui-destination]`),
      ).toHaveAttribute("href", `/Wisp/guide/tui/${part}.html#${id}`);
    }
    await page.locator("#keybindings a").click();
    await expect(page.locator("article #keybindings")).toBeVisible();
  } finally {
    await context.close();
  }
});
