// Render only synthetic production-buffer captures, never a user's terminal/session.
// Usage: node --import tsx scripts/render-tui-captures.ts /tmp/wisp-hy277-captures
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { chromium } from "@playwright/test";
import { repoRoot } from "../src/content/paths.ts";

const captures = [
  { output: "tui-showcase", frame: "rust-showcase-ember-150x34" },
  { output: "tui-welcome", frame: "rust-welcome-ember-150x34" },
  { output: "tui-diff-previews", frame: "rust-diff-previews-ember-100x30" },
  { output: "tui-approval", frame: "rust-approval-ember-100x30" },
  { output: "tui-mcp", frame: "rust-mcp-ember-100x30" },
  { output: "tui-new-turn", frame: "rust-new-turn-ember-100x30" },
];
// Masters are drawn at twice the cell size so the 1200px+ variants stay sharp.
const scale = 2;

// The bundled face draws text the same way on every OS; symbols outside its Latin
// subsets fall back to the local monospace font, and Braille is drawn as dots.
const fontFiles = join(
  dirname(createRequire(import.meta.url).resolve("@fontsource/jetbrains-mono")),
  "files",
);
const fonts = ["latin", "latin-ext"].flatMap((subset) =>
  [
    { weight: "400", style: "normal" },
    { weight: "700", style: "normal" },
    { weight: "400", style: "italic" },
    { weight: "700", style: "italic" },
  ].map((face) => ({
    ...face,
    data: readFileSync(
      join(
        fontFiles,
        `jetbrains-mono-${subset}-${face.weight}-${face.style}.woff2`,
      ),
    ).toString("base64"),
  })),
);

const input = process.argv[2];
if (!input)
  throw new Error("Pass the directory from capture_conversation_screens");
const output = join(repoRoot, "assets/docs");
mkdirSync(output, { recursive: true });
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.setContent('<canvas aria-label="TUI capture"></canvas>');
  await page.evaluate(async (fonts) => {
    for (const font of fonts) {
      const face = new FontFace(
        "Capture Mono",
        `url(data:font/woff2;base64,${font.data})`,
        { weight: font.weight, style: font.style },
      );
      document.fonts.add(await face.load());
    }
  }, fonts);
  for (const capture of captures) {
    const buffer = JSON.parse(
      readFileSync(join(input, `${capture.frame}.json`), "utf8"),
    );
    const image = await page.evaluate(
      ({
        capture,
        scale,
      }: {
        capture: {
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
        };
        scale: number;
      }) => {
        const canvas = document.querySelector("canvas")!;
        const width = 12,
          height = 24;
        canvas.width = capture.width * width * scale;
        canvas.height = capture.height * height * scale;
        const ctx = canvas.getContext("2d")!;
        ctx.scale(scale, scale);
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
        // Braille dots fill a 2 × 4 grid; bit order follows the Unicode pattern.
        const braille = [
          [0, 0],
          [0, 1],
          [0, 2],
          [1, 0],
          [1, 1],
          [1, 2],
          [0, 3],
          [1, 3],
        ];
        // Box-drawing glyphs are stroked from the cell centre so borders join across
        // cells; fallback fonts rarely fill the full 12 × 24 cell. Arms: up, right,
        // down, left; corners marked round curve instead of meeting square.
        const boxes: Record<
          string,
          { arms: string; heavy?: true; round?: true }
        > = {
          "─": { arms: "-r-l" },
          "━": { arms: "-r-l", heavy: true },
          "│": { arms: "u-d-" },
          "┃": { arms: "u-d-", heavy: true },
          "┌": { arms: "-rd-" },
          "┐": { arms: "--dl" },
          "└": { arms: "ur--" },
          "┘": { arms: "u--l" },
          "╭": { arms: "-rd-", round: true },
          "╮": { arms: "--dl", round: true },
          "╰": { arms: "ur--", round: true },
          "╯": { arms: "u--l", round: true },
          "├": { arms: "urd-" },
          "┤": { arms: "u-dl" },
          "┬": { arms: "-rdl" },
          "┴": { arms: "ur-l" },
          "┼": { arms: "urdl" },
        };
        for (const [index, cell] of capture.cells.entries()) {
          const x = (index % capture.width) * width,
            y = Math.floor(index / capture.width) * height;
          ctx.fillStyle = cell.reverse ? cell.bg : cell.fg;
          ctx.globalAlpha = cell.dim ? 0.5 : 1;
          const code = cell.text.codePointAt(0) ?? 0;
          const box = boxes[cell.text];
          if (code > 0x2800 && code <= 0x28ff) {
            for (const [bit, [column, row]] of braille.entries()) {
              if (!((code - 0x2800) & (1 << bit))) continue;
              ctx.beginPath();
              ctx.arc(
                x + 3.5 + column * 5,
                y + 3 + row * 6,
                1.6,
                0,
                2 * Math.PI,
              );
              ctx.fill();
            }
          } else if (box) {
            const centreX = x + width / 2,
              centreY = y + height / 2;
            const ends = {
              u: [centreX, y],
              r: [x + width, centreY],
              d: [centreX, y + height],
              l: [x, centreY],
            };
            const arms = [...box.arms].filter(
              (arm): arm is keyof typeof ends => arm in ends,
            );
            ctx.strokeStyle = ctx.fillStyle;
            ctx.lineWidth = box.heavy ? 3 : 1.5;
            ctx.beginPath();
            if (box.round) {
              const [from, to] = arms.map((arm) => ends[arm]);
              ctx.moveTo(from![0]!, from![1]!);
              ctx.arcTo(centreX, centreY, to![0]!, to![1]!, width / 2);
              ctx.lineTo(to![0]!, to![1]!);
            } else {
              for (const arm of arms) {
                ctx.moveTo(centreX, centreY);
                ctx.lineTo(ends[arm][0]!, ends[arm][1]!);
              }
            }
            ctx.stroke();
          } else {
            ctx.font = `${cell.italic ? "italic " : ""}${cell.bold ? "bold " : ""}20px "Capture Mono", monospace`;
            ctx.fillText(cell.text, x, y + 18);
          }
          if (cell.strike) ctx.fillRect(x, y + 12, width, 1);
          if (cell.underline) {
            ctx.fillStyle = cell.underline_color ?? ctx.fillStyle;
            ctx.fillRect(x, y + 22, width, 1);
          }
        }
        return canvas.toDataURL("image/png").split(",")[1]!;
      },
      { capture: buffer, scale },
    );
    writeFileSync(
      join(output, `${capture.output}.png`),
      Buffer.from(image, "base64"),
    );
  }
} finally {
  await browser.close();
}
