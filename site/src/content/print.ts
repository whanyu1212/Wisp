import { load } from "cheerio";
import type { PageMetadata } from "./metadata.ts";
import { base, href } from "./paths.ts";

export interface PrintEntry {
  page: PageMetadata;
  html: string;
}

/** Namespace repeated page IDs, then restore the historical combined-page anchors. */
export function combinePrint(
  entries: PrintEntry[],
  legacy: Record<string, string>,
): string {
  const targets = new Map<string, string>();
  const documents = entries.map(({ page, html }) => {
    const $ = load(html, null, false);
    // These links redirect old standalone TUI URLs; print contains the real sections.
    $('section[aria-label="Moved TUI sections"]').remove();
    const prefix = `page-${page.route.replace(/[^a-zA-Z0-9-]/g, "-")}`;
    targets.set(href(page.route), prefix);
    targets.set(base + page.route, prefix);
    $("[id]").each((_i, node) => {
      const id = $(node).attr("id")!;
      const combined = `${prefix}--${id}`;
      targets.set(`${href(page.route)}#${id}`, combined);
      targets.set(`${base}${page.route}#${id}`, combined);
      $(node).attr("id", combined);
    });
    return { $, page, prefix };
  });
  const aliases = new Map<string, string[]>();
  for (const [id, destination] of Object.entries(legacy)) {
    const target = targets.get(destination);
    if (!target)
      throw new Error(`Missing print destination: ${id} -> ${destination}`);
    aliases.set(target, [...(aliases.get(target) ?? []), id]);
  }
  return documents
    .map(({ $, page, prefix }) => {
      // Preserve local label/control relationships as well as links in rendered Markdown.
      $("[aria-labelledby], [aria-describedby]").each((_i, node) => {
        for (const attribute of ["aria-labelledby", "aria-describedby"]) {
          const value = $(node).attr(attribute);
          if (value)
            $(node).attr(
              attribute,
              value
                .split(/\s+/)
                .map((id) => `${prefix}--${id}`)
                .join(" "),
            );
        }
      });
      $("a[href]").each((_i, node) => {
        const url = $(node).attr("href")!;
        const target = targets.get(
          url.startsWith("#") ? href(page.route) + url : url,
        );
        if (target) $(node).attr("href", `#${target}`);
      });
      $("[id]").each((_i, node) => {
        for (const id of aliases.get($(node).attr("id")!) ?? []) {
          $(node).before($("<span></span>").attr("id", id));
        }
      });
      return `<section class="print-chapter" id="${prefix}">${$.html()}</section>`;
    })
    .join("\n");
}
