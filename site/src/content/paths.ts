import { join } from "node:path";
import { fileURLToPath } from "node:url";

declare const __WISP_REPO_ROOT__: string;

// Astro bundles endpoints into dist; preserve the build-time repository location.
export const repoRoot =
  typeof __WISP_REPO_ROOT__ !== "undefined"
    ? __WISP_REPO_ROOT__
    : fileURLToPath(new URL("../../../", import.meta.url));
export const contentRoot = join(repoRoot, "site/src/content/pages");
export const baselinePath = join(repoRoot, "site/migration/routes.json");
export const cutoverPath = join(repoRoot, "site/migration/cutover-routes.json");
export const base = "/Wisp/";

export function href(route: string): string {
  return base + route.replace(/(^|\/)index\.html$/, "$1");
}
