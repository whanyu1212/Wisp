import type { PageMetadata } from "./metadata.ts";
import { sections } from "./metadata.ts";

export function navigation(pages: PageMetadata[]) {
  return Object.entries(sections)
    .filter(([key]) => key !== "maintainers")
    .map(([key, title]) => ({
      key,
      title,
      pages: pages
        .filter((page) => page.section === key && page.listed)
        .sort((a, b) => a.order - b.order),
    }));
}
