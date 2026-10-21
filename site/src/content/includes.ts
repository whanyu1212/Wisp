import { readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { repoRoot } from "./paths.ts";

/** Expand mdBook includes before Markdown parsing, keeping runnable examples authoritative. */
export function expandIncludes(
  markdown: string,
  sourcePath: string,
  root = repoRoot,
): string {
  const expanded = markdown.replace(
    /\{\{#include\s+([^}\s]+)\s*\}\}/g,
    (_match, spec: string) => {
      const [file, selector, end, ...extra] = spec.split(":");
      if (!file || extra.length) throw new Error(`Invalid include: ${spec}`);
      const target = realpathSync(resolve(dirname(sourcePath), file));
      const local = relative(realpathSync(root), target);
      if (isAbsolute(local) || local === ".." || local.startsWith("../"))
        throw new Error(`Include outside repository: ${spec}`);
      const lines = readFileSync(target, "utf8")
        .replaceAll("\r\n", "\n")
        .split("\n");
      let selected: string[];
      if (!selector) selected = lines;
      else if (/^\d+$/.test(selector)) {
        const start = Number(selector),
          last = end ? Number(end) : start;
        if (
          !Number.isInteger(last) ||
          start < 1 ||
          last < start ||
          last > lines.length
        )
          throw new Error(`Invalid include range: ${spec}`);
        selected = lines.slice(start - 1, last);
      } else {
        if (end) throw new Error(`Invalid include anchor: ${spec}`);
        const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const startPattern = new RegExp(`ANCHOR:\\s*${escaped}\\s*$`);
        const endPattern = new RegExp(`ANCHOR_END:\\s*${escaped}\\s*$`);
        const starts = lines.flatMap((line, i) =>
          startPattern.test(line) ? [i] : [],
        );
        const ends = lines.flatMap((line, i) =>
          endPattern.test(line) ? [i] : [],
        );
        if (starts.length !== 1 || ends.length !== 1 || ends[0]! <= starts[0]!)
          throw new Error(`Missing or ambiguous include anchor: ${spec}`);
        selected = lines.slice(starts[0]! + 1, ends[0]);
      }
      selected = selected.filter((line) => !/ANCHOR(?:_END)?:\s*/.test(line));
      const indents = selected
        .filter((line) => line.trim())
        .map((line) => /^\s*/.exec(line)![0].length);
      const indent = indents.length ? Math.min(...indents) : 0;
      return selected
        .map((line) => line.slice(indent))
        .join("\n")
        .replace(/\n$/, "");
    },
  );
  if (expanded.includes("{{#include"))
    throw new Error(`Malformed or nested include in ${sourcePath}`);
  return expanded;
}
