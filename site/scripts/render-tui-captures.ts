// Render only synthetic production-buffer captures, never a user's terminal/session.
// Usage: node --import tsx scripts/render-tui-captures.ts /tmp/wisp-hy277-captures
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "@playwright/test";
import { repoRoot } from "../src/content/paths.ts";

const input = process.argv[2];
if (!input)
  throw new Error("Pass the directory from capture_conversation_screens");
const output = join(repoRoot, "assets/docs");
mkdirSync(output, { recursive: true });
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ deviceScaleFactor: 1 });
  for (const scenario of ["diff-previews", "approval", "mcp", "new-turn"]) {
    const capture = JSON.parse(
      readFileSync(join(input, `rust-${scenario}-dark-100x30.json`), "utf8"),
    );
    await page.setContent('<canvas aria-label="TUI capture"></canvas>');
    const image = await page.evaluate(
      (capture: {
        width: number;
        height: number;
        cells: {
          text: string;
          fg: string;
          bg: string;
          bold: boolean;
          dim: boolean;
          italic: boolean;
          underline: boolean;
          underline_color: string | null;
          reverse: boolean;
          strike: boolean;
        }[];
      }) => {
        const canvas = document.querySelector("canvas")!;
        const width = 12,
          height = 24;
        canvas.width = capture.width * width;
        canvas.height = capture.height * height;
        const ctx = canvas.getContext("2d")!;
        // Paint backgrounds first so wide glyphs are not erased by their next cell.
        for (const [index, cell] of capture.cells.entries()) {
          ctx.fillStyle = cell.reverse ? cell.fg : cell.bg;
          ctx.fillRect(
            (index % capture.width) * width,
            Math.floor(index / capture.width) * height,
            width,
            height,
          );
        }
        for (const [index, cell] of capture.cells.entries()) {
          const x = (index % capture.width) * width,
            y = Math.floor(index / capture.width) * height;
          ctx.fillStyle = cell.reverse ? cell.bg : cell.fg;
          ctx.globalAlpha = cell.dim ? 0.5 : 1;
          ctx.font = `${cell.italic ? "italic " : ""}${cell.bold ? "bold " : ""}20px monospace`;
          ctx.fillText(cell.text, x, y + 19);
          if (cell.strike) ctx.fillRect(x, y + 12, width, 1);
          if (cell.underline) {
            ctx.fillStyle = cell.underline_color ?? ctx.fillStyle;
            ctx.fillRect(x, y + 22, width, 1);
          }
        }
        return canvas.toDataURL("image/png").split(",")[1]!;
      },
      capture,
    );
    writeFileSync(
      join(output, `tui-${scenario}.png`),
      Buffer.from(image, "base64"),
    );
  }
} finally {
  await browser.close();
}
