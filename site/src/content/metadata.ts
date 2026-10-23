import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import { z } from "zod";
import { parseFrontmatter } from "@astrojs/markdown-remark";
import { contentRoot } from "./paths.ts";

export const sections = {
  using: "Using Wisp",
  reference: "Reference",
  learn: "Learn",
  inside: "Inside Wisp",
  contributing: "Contributing",
  maintainers: "Maintainer archive",
} as const;

export const pageSchema = z
  .object({
    source: z
      .string()
      .regex(/^[a-zA-Z0-9_-][a-zA-Z0-9_./-]*\.md$/)
      .refine((s) => !s.split("/").includes("..")),
    legacySource: z
      .string()
      .regex(/^[a-zA-Z0-9_-][a-zA-Z0-9_./-]*\.md$/)
      .refine((s) => !s.split("/").includes(".."))
      .optional(),
    title: z.string().trim().min(1),
    description: z.string().trim().min(1),
    route: z
      .string()
      .regex(
        /^[a-zA-Z0-9_-][a-zA-Z0-9_.-]*(?:\/[a-zA-Z0-9_-][a-zA-Z0-9_.-]*)*\.html$/,
      ),
    section: z.enum([
      "using",
      "reference",
      "learn",
      "inside",
      "contributing",
      "maintainers",
    ]),
    order: z.number().int().nonnegative(),
    layout: z.enum(["home", "docs", "course", "archive"]),
    listed: z.boolean(),
    searchable: z.boolean(),
    navTitle: z.string().trim().min(1).optional(),
    parent: z.string().optional(),
    chapter: z.number().int().positive().optional(),
    checkpoint: z.string().optional(),
  })
  .strict();
export type PageMetadata = z.infer<typeof pageSchema>;

// A collection identity, not a second Markdown file: the loader reads the root changelog.
export const changelog: PageMetadata = {
  source: "changelog.md",
  title: "Changelog",
  description:
    "Wisp release notes, compatibility changes, and upgrade guidance.",
  route: "changelog.html",
  section: "using",
  order: 0,
  layout: "docs",
  listed: false,
  searchable: true,
};

export function markdownSources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true })
    .flatMap((entry) => {
      const path = join(directory, entry.name);
      return entry.isDirectory()
        ? markdownSources(path)
        : entry.name.endsWith(".md")
          ? [path]
          : [];
    })
    .sort();
}

export function validateMetadata(
  input: unknown,
  sources: string[],
): PageMetadata[] {
  const pages = z.array(pageSchema).parse(input);
  const ids = new Set<string>(),
    routes = new Set<string>(),
    orders = new Set<string>();
  for (const page of pages) {
    if (ids.has(page.source))
      throw new Error(`Duplicate source: ${page.source}`);
    if (routes.has(page.route))
      throw new Error(`Duplicate route: ${page.route}`);
    const order = `${page.section}/${page.parent ?? ""}/${page.order}`;
    if (page.listed && orders.has(order))
      throw new Error(`Duplicate navigation order: ${order}`);
    if (page.listed) orders.add(order);
    ids.add(page.source);
    routes.add(page.route);
  }
  const outputs = new Map<string, string>();
  for (const page of pages) {
    for (const output of new Set([
      page.route,
      (page.legacySource ?? page.source).replace(/\.md$/, ".html"),
    ])) {
      if (output === "404.html" || output === "print.html")
        throw new Error(`Reserved route: ${output}`);
      const owner = outputs.get(output);
      if (owner && owner !== page.source)
        throw new Error(`Compatibility route collision: ${output}`);
      outputs.set(output, page.source);
    }
  }
  const expected = new Set(sources);
  for (const source of sources)
    if (!ids.has(source)) throw new Error(`Missing metadata: ${source}`);
  for (const page of pages) {
    if (!expected.has(page.source))
      throw new Error(`Missing source: ${page.source}`);
    const visited = new Set([page.source]);
    let current = page;
    while (current.parent) {
      const parent = pages.find((p) => p.source === current.parent);
      if (!parent || parent.section !== page.section)
        throw new Error(`Invalid parent: ${current.parent}`);
      if (visited.has(parent.source))
        throw new Error(`Navigation cycle: ${page.source}`);
      visited.add(parent.source);
      current = parent;
    }
  }
  return pages;
}

/** Read strict per-page frontmatter; the physical path owns the source identity. */
export function readPage(source: string, root = contentRoot) {
  const parsed = parseFrontmatter(readFileSync(join(root, source), "utf8"));
  const metadata = pageSchema.omit({ source: true }).parse(parsed.frontmatter);
  return { metadata: { ...metadata, source }, body: parsed.content };
}

export function loadMetadata(): PageMetadata[] {
  const sources = markdownSources(contentRoot).map((p) =>
    relative(contentRoot, p).replaceAll("\\", "/"),
  );
  return validateMetadata(
    [...sources.map((source) => readPage(source).metadata), changelog],
    [...sources, changelog.source],
  );
}
