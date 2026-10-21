import type { PageMetadata } from "./metadata.ts";
import { sections } from "./metadata.ts";

export type NavigationPage = PageMetadata & { depth: number };
export interface NavigationGroup {
  key: string;
  title: string;
  pages: NavigationPage[];
}

/** Project validated metadata in sibling order for every navigation surface. */
export function navigation(pages: PageMetadata[]): NavigationGroup[] {
  return Object.entries(sections)
    .filter(([key]) => key !== "maintainers")
    .map(([key, title]) => {
      const members = pages.filter((page) => page.section === key);
      const visit = (parent?: string, depth = 0): NavigationPage[] =>
        members
          .filter((page) => page.parent === parent)
          .sort((a, b) => a.order - b.order)
          .flatMap((page) => [
            ...(page.listed ? [{ ...page, depth }] : []),
            ...visit(page.source, depth + (page.listed ? 1 : 0)),
          ]);
      return { key, title, pages: visit() };
    });
}

/** Return outermost-first ancestors; input has already passed cycle validation. */
export function ancestors(
  page: PageMetadata,
  pages: PageMetadata[],
): PageMetadata[] {
  const parents: PageMetadata[] = [];
  let parent = pages.find((item) => item.source === page.parent);
  while (parent) {
    parents.unshift(parent);
    const source = parent.parent;
    parent = pages.find((item) => item.source === source);
  }
  return parents;
}
