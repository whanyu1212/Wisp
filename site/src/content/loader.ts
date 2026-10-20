import { readFileSync, existsSync } from "node:fs";
import { join, relative } from "node:path";
import { pathToFileURL } from "node:url";
import type { Loader } from "astro/loaders";
import { loadMetadata } from "./metadata.ts";
import { baselinePath, bookRoot, metadataPath, repoRoot } from "./paths.ts";
import { expandIncludes } from "./includes.ts";
import { finishHtml, type LegacyPage } from "./render.ts";

export function bookLoader(archive = false): Loader {
  return {
    name: archive ? "wisp-maintainer-records" : "wisp-book",
    async load(context) {
      const refresh = async () => {
        const pages = loadMetadata();
        const baseline = JSON.parse(readFileSync(baselinePath, "utf8")) as {
          pages: LegacyPage[];
        };
        // Build all entries before clearing the old store: invalid edits must not publish partial content.
        const entries = [];
        for (const page of pages.filter(
          (page) => (page.layout === "archive") === archive,
        )) {
          if (page.checkpoint && !existsSync(join(repoRoot, page.checkpoint)))
            throw new Error(`Missing checkpoint: ${page.checkpoint}`);
          const sourcePath = join(bookRoot, page.source);
          const body = expandIncludes(
            readFileSync(sourcePath, "utf8"),
            sourcePath,
          );
          const rendered = await context.renderMarkdown(body, {
            fileURL: pathToFileURL(sourcePath),
          });
          const legacy = baseline.pages.find(
            (p) => p.source === `book/src/${page.source}`,
          );
          const finished = finishHtml(rendered.html, page, pages, legacy);
          const data = await context.parseData({ id: page.source, data: page });
          entries.push({
            id: page.source,
            data,
            body,
            filePath: relative(context.config.root.pathname, sourcePath),
            rendered: {
              ...rendered,
              html: finished.html,
              metadata: { ...rendered.metadata, headings: finished.headings },
            },
          });
        }
        context.store.clear();
        for (const entry of entries) context.store.set(entry);
      };
      await refresh();
      if (context.watcher) {
        context.watcher.add([
          bookRoot,
          metadataPath,
          baselinePath,
          join(repoRoot, "examples/crafting_agents"),
        ]);
        let pending = Promise.resolve();
        const changed = (path: string) => {
          if (
            path.startsWith(bookRoot) ||
            path === metadataPath ||
            path === baselinePath ||
            path.startsWith(join(repoRoot, "examples/crafting_agents"))
          ) {
            pending = pending
              .then(refresh)
              .catch((error) => context.logger.error(String(error)));
          }
        };
        context.watcher
          .on("change", changed)
          .on("add", changed)
          .on("unlink", changed);
      }
    },
  };
}
