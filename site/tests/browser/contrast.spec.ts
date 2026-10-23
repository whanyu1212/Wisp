import { expect, test } from "@playwright/test";

// Use rendered colors, not just palette constants: syntax themes and inherited
// backgrounds can otherwise silently lose contrast on the editorial paper.
test("navigation, highlighted code, and controls retain readable contrast", async ({
  page,
}) => {
  for (const route of [
    "",
    "guide/index.html",
    "reference/sdk.html",
    "crafting-agents/01-core-loop.html",
  ]) {
    await page.goto(route);
    for (const theme of ["light", "dark"]) {
      await page.getByRole("combobox", { name: "Theme" }).selectOption(theme);
      const failures = await page.evaluate(() => {
        const luminance = (color: string) => {
          const [r, g, b] = color
            .match(/[\d.]+/g)!
            .slice(0, 3)
            .map(Number)
            .map((value) => {
              const channel = value / 255;
              return channel <= 0.04045
                ? channel / 12.92
                : ((channel + 0.055) / 1.055) ** 2.4;
            });
          return r! * 0.2126 + g! * 0.7152 + b! * 0.0722;
        };
        const contrast = (a: string, b: string) => {
          const first = luminance(a),
            second = luminance(b);
          return (
            (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05)
          );
        };
        const background = (element: Element): string => {
          const color = getComputedStyle(element).backgroundColor;
          if (color !== "rgba(0, 0, 0, 0)") return color;
          return element.parentElement
            ? background(element.parentElement)
            : "rgb(255, 255, 255)";
        };
        const failures = new Set<string>();
        document
          .querySelectorAll<HTMLElement>(
            ".site-header a, .eyebrow, .breadcrumbs a, article p, .lede, .install-note, .tui-capture figcaption, .primary, .section-index p, .sidebar a, .toc a, .astro-code span, button, select",
          )
          .forEach((element) => {
            if (
              !element.getClientRects().length ||
              !element.textContent?.trim()
            )
              return;
            const style = getComputedStyle(element);
            const large = parseFloat(style.fontSize) >= 24;
            const ratio = contrast(style.color, background(element));
            if (ratio < (large ? 3 : 4.5))
              failures.add(
                `Text ${style.color} on ${background(element)}: ${ratio.toFixed(2)}`,
              );
          });
        document.querySelectorAll("button, select").forEach((element) => {
          const style = getComputedStyle(element);
          const ratio = contrast(style.borderTopColor, background(element));
          if (ratio < 3) failures.add(`Control border: ${ratio.toFixed(2)}`);
        });
        return [...failures];
      });
      expect(failures, `${route} in ${theme}`).toEqual([]);
    }
  }
});
