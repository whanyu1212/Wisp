import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { load } from "cheerio";
import type { PageMetadata } from "./metadata.ts";
import type { LegacyPage } from "./render.ts";
import { base } from "./paths.ts";

function files(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  });
}

/** Check real output files and fragments, without extension-fallback heuristics. */
export function checkOutput(
  directory: string,
  pages: PageMetadata[],
  baseline: LegacyPage[],
): string[] {
  const errors: string[] = [];
  const documents = new Map<string, ReturnType<typeof load>>();
  for (const path of files(directory).filter((path) =>
    path.endsWith(".html"),
  )) {
    const local = relative(directory, path).split(sep).join("/");
    documents.set(local, load(readFileSync(path, "utf8")));
  }
  for (const [local, $] of documents) {
    const ids = new Set<string>();
    $("[id]").each((_i, element) => {
      const id = $(element).attr("id")!;
      if (ids.has(id)) errors.push(`${local}: duplicate ID #${id}`);
      ids.add(id);
    });
    $("a[href],img[src],script[src],link[href]").each((_i, element) => {
      const url = $(element).attr("href") ?? $(element).attr("src") ?? "";
      if (!url || /^(?:[a-z][\w+.-]*:|\/\/)/i.test(url)) return;
      if (!url.startsWith("#") && !url.startsWith(base)) {
        errors.push(`${local}: link is not base-prefixed: ${url}`);
        return;
      }
      const parsed = new URL(url, `https://example.test${base}${local}`);
      let target = decodeURIComponent(parsed.pathname.slice(base.length));
      if (!target || target.endsWith("/")) target += "index.html";
      const file = resolve(directory, target);
      if (!file.startsWith(resolve(directory) + sep) || !existsSync(file)) {
        errors.push(`${local}: missing output ${url}`);
        return;
      }
      if (parsed.hash && documents.has(target)) {
        const id = decodeURIComponent(parsed.hash.slice(1));
        const found = documents.get(target)!("[id]")
          .toArray()
          .some((el) => documents.get(target)!(el).attr("id") === id);
        if (!found) errors.push(`${local}: missing fragment ${url}`);
      }
    });
    if ($('pre[data-language="mermaid"], pre code.language-mermaid').length)
      errors.push(`${local}: unrendered Mermaid fence`);
    if (
      $("code")
        .toArray()
        .some((el) => $(el).text().includes("{{#include"))
    )
      errors.push(`${local}: unexpanded include`);
  }
  for (const page of pages)
    if (!documents.has(page.route))
      errors.push(`Missing canonical output: ${page.route}`);
  for (const old of baseline) {
    const current = pages.find(
      (page) => `book/src/${page.source}` === old.source,
    );
    if (!current) {
      errors.push(`Unaccounted legacy source: ${old.source}`);
      continue;
    }
    if (current.route !== old.destination.slice(base.length)) {
      errors.push(
        `Changed planned destination: ${old.source} -> ${current.route}`,
      );
    }
    for (const alias of old.aliases) {
      const output = alias.slice(base.length) + "index.html";
      if (
        output !== old.legacy_url.slice(base.length) ||
        !documents.has(output)
      )
        errors.push(`Missing legacy index alias: ${alias}`);
    }
    for (const route of new Set([
      old.legacy_url.slice(base.length),
      current.route,
    ])) {
      const doc = documents.get(route);
      if (!doc) {
        errors.push(`Missing legacy/canonical route: ${route}`);
        continue;
      }
      const ids = new Set(
        doc("[id]")
          .toArray()
          .map((el) => doc(el).attr("id")),
      );
      for (const heading of old.anchors)
        if (!ids.has(heading.id))
          errors.push(`${route}: lost legacy #${heading.id}`);
    }
  }
  return errors;
}
