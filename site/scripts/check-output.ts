import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { baselinePath, cutoverPath } from "../src/content/paths.ts";
import { loadMetadata } from "../src/content/metadata.ts";
import { checkOutput } from "../src/content/check-output.ts";
import type { LegacyPage } from "../src/content/render.ts";

const baseline = JSON.parse(readFileSync(baselinePath, "utf8")) as {
  pages: LegacyPage[];
  utility_routes: { legacy_url: string; heading_ids: string[] }[];
};
const cutover = JSON.parse(readFileSync(cutoverPath, "utf8")) as {
  pages: LegacyPage[];
  print: { heading_ids: string[] };
};
const pages = loadMetadata();
const errors = checkOutput(
  fileURLToPath(new URL("../dist/", import.meta.url)),
  pages,
  [...baseline.pages, ...cutover.pages],
  [
    ...new Set([
      ...baseline.utility_routes.find(
        (route) => route.legacy_url === "/Wisp/print.html",
      )!.heading_ids,
      ...cutover.print.heading_ids,
    ]),
  ],
);
if (errors.length)
  throw new Error(`Output validation failed:\n${errors.join("\n")}`);
console.log(
  `Validated ${pages.length} content pages, ${baseline.pages.reduce((n, p) => n + p.anchors.length, 0)} original anchors, ${cutover.pages.reduce((n, p) => n + p.anchors.length, 0)} cutover anchors, print anchors, and all local output links/assets.`,
);
