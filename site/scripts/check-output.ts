import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { baselinePath } from "../src/content/paths.ts";
import { loadMetadata } from "../src/content/metadata.ts";
import { checkOutput } from "../src/content/check-output.ts";
import type { LegacyPage } from "../src/content/render.ts";

const baseline = JSON.parse(readFileSync(baselinePath, "utf8")) as {
  pages: LegacyPage[];
};
const pages = loadMetadata();
const errors = checkOutput(
  fileURLToPath(new URL("../dist/", import.meta.url)),
  pages,
  baseline.pages,
);
if (errors.length)
  throw new Error(`Output validation failed:\n${errors.join("\n")}`);
console.log(
  `Validated ${pages.length} content pages, ${baseline.pages.reduce((n, p) => n + p.anchors.length, 0)} legacy anchors, and all local output links/assets.`,
);
