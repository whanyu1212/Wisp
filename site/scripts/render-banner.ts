// Compose the README banner from the synthetic TUI captures, overwriting it in place.
// Usage: node --import tsx scripts/render-banner.ts /tmp/wisp-docs-captures
// Run scripts/render-tui-captures.ts on the same directory first.
import { readdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { chromium } from "@playwright/test";
import { repoRoot } from "../src/content/paths.ts";

const input = process.argv[2];
if (!input)
  throw new Error("Pass the directory from capture_conversation_screens");

// The welcome master is drawn at 12 × 24 pixels per cell, doubled.
const cellWidth = 24,
  cellHeight = 48;
const welcome = JSON.parse(
  readFileSync(join(input, "rust-welcome-ember-150x34.json"), "utf8"),
) as { width: number; cells: { text: string }[] };
const mascotCells = welcome.cells.flatMap((cell, index) => {
  const code = cell.text.codePointAt(0) ?? 0;
  return code > 0x2800 && code <= 0x28ff
    ? [
        {
          column: index % welcome.width,
          row: Math.floor(index / welcome.width),
        },
      ]
    : [];
});
if (mascotCells.length === 0)
  throw new Error("The welcome capture has no Braille mascot");
const columns = mascotCells.map((cell) => cell.column);
const rows = mascotCells.map((cell) => cell.row);
const mascot = {
  x: Math.min(...columns) * cellWidth,
  y: Math.min(...rows) * cellHeight,
  width: (Math.max(...columns) - Math.min(...columns) + 1) * cellWidth,
  height: (Math.max(...rows) - Math.min(...rows) + 1) * cellHeight,
};

const dataUrl = (path: string, type: string) =>
  `data:${type};base64,${readFileSync(path).toString("base64")}`;
const fontFiles = join(
  dirname(createRequire(import.meta.url).resolve("@fontsource/jetbrains-mono")),
  "files",
);
const mono = dataUrl(
  join(fontFiles, "jetbrains-mono-latin-400-normal.woff2"),
  "font/woff2",
);
const welcomeImage = dataUrl(
  join(repoRoot, "assets/docs/tui-welcome.png"),
  "image/png",
);
// A real session screenshot. macOS names it with a narrow no-break space before
// "PM", so it is found by prefix rather than spelled out.
const screenshotName = readdirSync(join(repoRoot, "assets")).find((name) =>
  name.startsWith("Screenshot 2026-10-25 at 12.58.53"),
);
if (!screenshotName) throw new Error("The session screenshot is missing");
const screenshotImage = dataUrl(
  join(repoRoot, "assets", screenshotName),
  "image/png",
);
// The terminal's own pixels inside the screenshot, without the macOS chrome,
// which the banner redraws as its own title bar.
const screenshot = { width: 2254, height: 1236 };
const terminal = { x: 12, y: 66, width: 2226, height: 1152 };
const screenWidth = 412;
const screenScale = screenWidth / terminal.width;
const screenHeight = Math.round(terminal.height * screenScale);

// Ember palette from rust/wisp-tui/src/theme_catalog.json.
const html = `<!doctype html>
<style>
  @font-face { font-family: "Banner Mono"; src: url(${mono}); }
  html, body { margin: 0; }
  body {
    width: 1200px; height: 300px; overflow: hidden; position: relative;
    background:
      radial-gradient(420px 260px at 120px 150px, #fab28322, transparent 70%),
      radial-gradient(520px 320px at 1060px 40px, #f58b6b1c, transparent 70%),
      #100f0f;
    color: #eeeeee;
  }
  /* Lighten drops the capture's dark cell background into the gradient. */
  .mascot {
    position: absolute; left: 44px; top: 40px; height: 220px;
    mix-blend-mode: lighten;
  }
  .copy { position: absolute; left: 300px; top: 44px; width: 440px; }
  h1 {
    margin: 0; font: 400 88px/1 Georgia, "Times New Roman", serif;
    letter-spacing: -0.045em; color: #fab283;
  }
  p {
    margin: 18px 0 0; font: 21px/1.45 "Banner Mono", monospace; color: #eeeeee;
  }
  code {
    display: inline-block; margin-top: 20px; padding: 9px 16px;
    border: 1px solid #fab28366; border-radius: 8px; background: #1c1b1a;
    font: 17px/1.2 "Banner Mono", monospace; color: #eeeeee;
  }
  code span, p em { color: #fab283; }
  p em { font-style: normal; }
  /* The terminal floats in a gentle perspective tilt, as in the promo video. */
  .stage { position: absolute; left: 758px; top: 0; width: 442px; height: 300px; perspective: 1300px; }
  .orbit {
    position: absolute; left: -12px; top: 92px; width: 470px; height: 140px;
    border: 1px solid #fab28326; border-radius: 50%; transform: rotate(-9deg);
  }
  .orbit::after {
    content: ""; position: absolute; left: 34px; top: 104px; width: 6px; height: 6px;
    border-radius: 50%; background: #fab283; box-shadow: 0 0 10px 3px #fab28388;
  }
  .window {
    position: absolute; left: 14px; top: 34px; width: ${screenWidth}px;
    border: 1px solid #4a4440; border-radius: 10px; overflow: hidden;
    background: #100f0f; transform-origin: 50% 50%;
    transform: rotateY(-14deg) rotateX(5deg) rotateZ(-1.5deg);
    box-shadow: 0 0 0 1px #00000088, 0 24px 60px -12px #000000ee,
      0 0 70px -18px #fab28355;
  }
  .bar {
    display: flex; align-items: center; gap: 6px; height: 22px; padding: 0 10px;
    background: #1c1b1a; border-bottom: 1px solid #2c2a28;
    font: 10px/1 "Banner Mono", monospace; color: #8f8a85;
  }
  .bar i { width: 8px; height: 8px; border-radius: 50%; }
  .bar b { margin-left: 8px; font-weight: 400; }
  .screen { position: relative; height: ${screenHeight}px; overflow: hidden; }
  .screen img {
    position: absolute; width: ${screenshot.width * screenScale}px;
    left: ${-terminal.x * screenScale}px; top: ${-terminal.y * screenScale}px;
  }
  /* A soft sheen across the glass keeps the tilt from reading as flat. */
  .screen::after {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(115deg, #ffffff0d, transparent 40%);
  }
</style>
<canvas class="mascot"></canvas>
<div class="copy">
  <h1>Wisp</h1>
  <p>your code, your <em>direction</em>.</p>
  <code><span>$</span> uv tool install --python 3.12 wisp-ai</code>
</div>
<div class="stage">
  <div class="orbit"></div>
  <div class="window">
    <div class="bar">
      <i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i>
      <b>wisp — ~/Wisp</b>
    </div>
    <div class="screen"><img src="${screenshotImage}" alt=""></div>
  </div>
</div>`;

const browser = await chromium.launch();
try {
  const page = await browser.newPage({
    viewport: { width: 1200, height: 300 },
    deviceScaleFactor: 2,
  });
  await page.setContent(html, { waitUntil: "load" });
  await page.evaluate(
    async ({ source, crop }) => {
      const image = new Image();
      image.src = source;
      await image.decode();
      const canvas = document.querySelector("canvas")!;
      canvas.width = crop.width;
      canvas.height = crop.height;
      canvas
        .getContext("2d")!
        .drawImage(
          image,
          crop.x,
          crop.y,
          crop.width,
          crop.height,
          0,
          0,
          crop.width,
          crop.height,
        );
      await document.fonts.ready;
    },
    { source: welcomeImage, crop: mascot },
  );
  await page.screenshot({ path: join(repoRoot, "assets/wisp-banner-v2.png") });
} finally {
  await browser.close();
}
